/*
 * bidi.c
 *
 * Distributed under terms of the GPL3 license.
 */

#include "bidi.h"

#define BIDI_N0_SCAN_PER_CHAR 16

// False for codepoints outside the blocks that contain right-to-left or Arabic
// letters, i.e. text that cannot need reordering. Ordered so that the common
// rejects take the fewest compares.
static inline bool
bidi_codepoint_may_reorder(char_type ch) {
    if (ch < 0x590) return false;
    if (ch < 0xfb1d) return ch <= 0x8ff;
    if (ch >= 0x1f000) return false;
    return ch <= 0xfdff || ch - 0xfe70 <= 0xfefe - 0xfe70 || ch - 0x10800 <= 0x10fff - 0x10800 || ch - 0x1e800 <= 0x1efff - 0x1e800;
}

void
bidi_scratch_free(BidiScratch *s) {
    free(s->text);
    free(s->types);
    free(s->brackets);
    free(s->levels);
    free(s->cell_of_char);
    free(s->row_start);
    free(s->row_bytes);
    zero_at_ptr(s);
}

bool
bidi_cells_may_reorder(const CPUCell *cells, index_type num, const TextCache *tc) {
    for (const CPUCell *c = cells, *end = cells + num; c < end; c++) {
        char_type ch = c->ch_or_idx;
        if (c->ch_is_idx) {
            if (c->is_multicell && (c->x || c->y)) continue;
            ch = tc_first_char_at_index(tc, ch);
        }
        if (bidi_codepoint_may_reorder(ch)) return true;
    }
    return false;
}

// Appends to s->text the characters a row feeds FriBidi: per cell its first char, a blank as U+0020. Returns the new
// length, or UINT32_MAX for a row with a cell spanning several rows, which cannot be reordered.
static inline uint32_t
feed_row(BidiScratch *s, const CPUCell *cells, index_type cols, const TextCache *tc, uint32_t n) {
    FriBidiChar *restrict text = s->text + n, *const text_start = text;
    uint32_t *restrict cell_of_char = s->cell_of_char + n;
    for (index_type col = 0; col < cols; col++) {
        const CPUCell *c = cells + col;
        if (c->is_multicell) {
            if (c->scale > 1) return UINT32_MAX;
            if (col && c->x) continue;
        }
        const char_type ch = cell_first_char(c, tc);
        *text++ = ch ? ch : ' ';
        *cell_of_char++ = col;
    }
    return n + (uint32_t)(text - text_start);
}

// Embeddings, overrides and isolates are never honoured: a cell can start with such a control only through OSC 66, which can
// start any number of cells with one
static void
retype_controls(BidiScratch *s, uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        if (s->types[i] & (FRIBIDI_MASK_EXPLICIT | FRIBIDI_MASK_ISOLATE)) s->types[i] = FRIBIDI_TYPE_ON;
    }
}

bool
bidi_resolve_paragraph(
    BidiScratch *s, const CPUCell *const *rows, unsigned num_rows, index_type cols, const TextCache *tc, FriBidiParType base, uint8_t *levels) {
    const size_t cap = (size_t)num_rows * cols;
    ensure_space_for(s, text, FriBidiChar, cap, text_cap, cap, false);
    ensure_space_for(s, types, FriBidiCharType, cap, types_cap, cap, false);
    ensure_space_for(s, brackets, FriBidiBracketType, cap, brackets_cap, cap, false);
    ensure_space_for(s, levels, FriBidiLevel, cap, levels_cap, cap, false);
    ensure_space_for(s, cell_of_char, uint32_t, cap, cell_of_char_cap, cap, false);
    ensure_space_for(s, row_start, uint32_t, num_rows + 1, row_start_cap, num_rows + 1, false);
    uint32_t n = 0;
    for (unsigned r = 0; r < num_rows; r++) {
        s->row_start[r] = n;
        if ((n = feed_row(s, rows[r], cols, tc, n)) == UINT32_MAX) return false;
    }
    s->row_start[num_rows] = n;
    fribidi_get_bidi_types(s->text, (FriBidiStrIndex)n, s->types);
    retype_controls(s, n);
    fribidi_get_bracket_types(s->text, (FriBidiStrIndex)n, s->types, s->brackets);
    // FriBidi before 1.0.17 walks back to the previous strong char for every unresolved bracket pair (N0); scan bounds
    // that walk, and skipping N0 past the budget keeps every version linear.
    uint64_t scan = 0;
    int64_t last = -1;
    for (uint32_t i = 0; i < n; i++) {
        if (s->types[i] & (FRIBIDI_MASK_LETTER | FRIBIDI_MASK_NUMBER)) last = i;
        else if (FRIBIDI_IS_BRACKET_OPEN(s->brackets[i])) scan += (uint64_t)(i - last);
    }
    if (scan > (uint64_t)BIDI_N0_SCAN_PER_CHAR * n) {
        for (uint32_t i = 0; i < n; i++) s->brackets[i] = FRIBIDI_NO_BRACKET;
    }
    FriBidiParType dir = base;
    // Returns the highest level plus one, 0 on failure. Only an LTR result can leave every level at 0, an RTL one has them all at
    // 1 or more.
    if (fribidi_get_par_embedding_levels_ex(s->types, s->brackets, (FriBidiStrIndex)n, &dir, s->levels) <= 1) return false;
    const FriBidiLevel base_level = FRIBIDI_DIR_TO_LEVEL(dir);
    for (unsigned r = 0; r < num_rows; r++) {
        const uint32_t row_first = s->row_start[r], row_end = s->row_start[r + 1];
        // L1 for the blanks ending the row, as fribidi_reorder_line() does it, which FriBidi does only at the paragraph's end
        for (uint32_t i = row_end; i > row_first && FRIBIDI_IS_EXPLICIT_OR_BN_OR_WS(s->types[i - 1]); i--) s->levels[i - 1] = base_level;
        uint8_t *row = levels + (size_t)r * cols;
        index_type col = 0;
        uint8_t level = base_level;
        for (uint32_t i = row_first; i < row_end; i++) {
            for (const index_type c = s->cell_of_char[i]; col < c; col++) row[col] = level;
            level = (uint8_t)s->levels[i];
        }
        for (; col < cols; col++) row[col] = level;
    }
    return true;
}

bool
bidi_visual_order(const uint8_t *levels, index_type cols, uint16_t *v2l) {
    uint8_t max_level = 0, min_level = UINT8_MAX;
    for (index_type x = 0; x < cols; x++) {
        max_level = MAX(max_level, levels[x]);
        min_level = MIN(min_level, levels[x]);
    }
    const unsigned min_odd = min_level | 1u;
    if (max_level < min_odd) return false;
    for (index_type x = 0; x < cols; x++) v2l[x] = (uint16_t)x;
    for (unsigned level = max_level; level >= min_odd; level--) {
        for (index_type x = 0; x < cols;) {
            if (levels[x] < level) {
                x++;
                continue;
            }
            index_type end = x + 1;
            while (end < cols && levels[end] >= level) end++;
            for (index_type a = x, b = end - 1; a < b; a++, b--) {
                const uint16_t t = v2l[a];
                v2l[a] = v2l[b];
                v2l[b] = t;
            }
            x = end;
        }
    }
    for (index_type x = 0; x < cols; x++) {
        if (v2l[x] != x) return true;
    }
    return false;
}

void
bidi_permute_row_bytes(BidiScratch *s, const uint16_t *v2l, index_type cols, uint8_t *row, size_t stride) {
    const size_t sz = (size_t)cols * stride;
    ensure_space_for(s, row_bytes, uint8_t, sz, row_bytes_cap, sz, false);
    memcpy(s->row_bytes, row, sz);
    for (index_type v = 0; v < cols; v++) memcpy(row + v * stride, s->row_bytes + v2l[v] * stride, stride);
}
