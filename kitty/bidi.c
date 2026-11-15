/*
 * bidi.c
 *
 * Distributed under terms of the GPL3 license.
 */

#include "bidi.h"

#define row_of(arr) (b->arr + (size_t)render_row * b->cols)

static void
free_row_maps(BidiMaps *b) {
    free(b->v2l);
    free(b->levels);
    free(b->active);
    free(b->row_id);
    free(b->para_first);
    free(b->para_last);
    free(b->para_rows);
    b->v2l = NULL;
    b->levels = NULL;
    b->active = NULL;
    b->row_id = NULL;
    b->para_first = NULL;
    b->para_last = NULL;
    b->para_rows = NULL;
    b->ctx_linebuf = NULL;
    b->rows = 0;
    b->cols = 0;
}

void
bidi_maps_free(BidiMaps *b) {
    free_row_maps(b);
    free(b->paused_ids);
    free(b->text);
    free(b->types);
    free(b->brackets);
    free(b->para_levels);
    free(b->map);
    free(b->cell_of_char);
    free(b->row_scratch);
    zero_at_ptr(b);
}

bool
bidi_maps_ensure(BidiMaps *b, index_type rows, index_type cols) {
    if (b->v2l && b->rows == rows && b->cols == cols) return true;
    free_row_maps(b);
    if (cols > UINT16_MAX) return false;
    const size_t cells = (size_t)rows * cols;
    b->v2l = malloc(cells * sizeof(b->v2l[0]));
    b->levels = malloc(cells * sizeof(b->levels[0]));
    b->active = calloc(rows, sizeof(b->active[0]));
    b->row_id = calloc(rows, sizeof(b->row_id[0]));
    b->para_first = calloc(rows, sizeof(b->para_first[0]));
    b->para_last = calloc(rows, sizeof(b->para_last[0]));
    b->para_rows = calloc(rows, sizeof(b->para_rows[0]));
    if (!b->v2l || !b->levels || !b->active || !b->row_id || !b->para_first || !b->para_last || !b->para_rows) {
        free_row_maps(b);
        return false;
    }
    b->rows = rows;
    b->cols = cols;
    b->changed = true;
    return true;
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

static bool
ensure_scratch(BidiMaps *b, size_t cap) {
    if (b->cap >= cap) return true;
#define grow(field)                                             \
    {                                                           \
        void *p = realloc(b->field, cap * sizeof(b->field[0])); \
        if (!p) return false;                                   \
        b->field = p;                                           \
    }
    grow(text);
    grow(types);
    grow(brackets);
    grow(para_levels);
    grow(map);
    grow(cell_of_char);
#undef grow
    b->cap = cap;
    return true;
}

// Appends to b->text the characters row r of a paragraph feeds FriBidi, one per
// cell a character starts in, a blank as U+0020. Returns the new length, or
// UINT32_MAX for a row with a cell spanning several rows, which cannot be reordered.
static uint32_t
feed_row(BidiMaps *b, const CPUCell *cells, const TextCache *tc, uint32_t r, uint32_t n) {
    for (index_type col = 0; col < b->cols; col++) {
        const CPUCell *c = cells + col;
        if (c->is_multicell) {
            if (c->scale > 1) return UINT32_MAX;
            if (col && c->x) continue;
        }
        const char_type ch = cell_first_char(c, tc);
        b->text[n] = ch ? ch : ' ';
        b->cell_of_char[n] = (r << 16) | col;
        n++;
    }
    return n;
}

// Resolves the levels of the paragraph's characters into b->para_levels.
// Returns how many characters FriBidi resolved, the rest being blanks at level
// 0, or 0 when the resolve failed or left every level at 0.
static uint32_t
resolve_levels(BidiMaps *b, const CPUCell *const *rows, unsigned num_rows, const TextCache *tc) {
    b->resolve_cacheable = true;
    if (!num_rows || num_rows > BIDI_MAX_PARAGRAPH_ROWS) return 0;
    if (!ensure_scratch(b, (size_t)num_rows * b->cols)) {
        b->resolve_cacheable = false;
        return 0;
    }
    uint32_t n = 0;
    for (unsigned r = 0; r < num_rows; r++) {
        b->row_start[r] = n;
        n = feed_row(b, rows[r], tc, r, n);
        if (n == UINT32_MAX) {
            b->resolve_cacheable = false;
            return 0;
        }
    }
    b->row_start[num_rows] = n;
    fribidi_get_bidi_types(b->text, (FriBidiStrIndex)n, b->types);
    // Blanks at the paragraph's end take the paragraph level (L1) whatever
    // precedes them, so they need not be resolved: only inside an explicit
    // embedding could they set the eos of the run before them (X10), and the
    // retype below leaves none.
    uint32_t n_trim = n;
    while (n_trim > b->row_start[num_rows - 1] && b->text[n_trim - 1] == ' ') n_trim--;
    for (uint32_t i = 0; i < n_trim; i++) {
        // Drawn normally a bidi control is a combining char FriBidi never sees; OSC 66 can start a cell with one, and FriBidi is quadratic in sibling isolates
        if (b->types[i] & (FRIBIDI_MASK_ISOLATE | FRIBIDI_MASK_EXPLICIT)) b->types[i] = FRIBIDI_TYPE_ON;
    }
    if (!n_trim) return 0;
    fribidi_get_bracket_types(b->text, (FriBidiStrIndex)n, b->types, b->brackets);
    // FriBidi before 1.0.17 walks back to the previous strong char for every unresolved bracket pair (N0); scan bounds
    // that walk, and skipping N0 past the budget keeps every version linear.
    uint64_t scan = 0;
    int64_t last = -1;
    for (uint32_t i = 0; i < n_trim; i++) {
        if (b->types[i] & (FRIBIDI_MASK_LETTER | FRIBIDI_MASK_NUMBER)) last = i;
        else if (FRIBIDI_IS_BRACKET_OPEN(b->brackets[i])) scan += (uint64_t)(i - last);
    }
    if (scan > (uint64_t)BIDI_N0_SCAN_PER_CHAR * n_trim) {
        for (uint32_t i = 0; i < n; i++) b->brackets[i] = FRIBIDI_NO_BRACKET;
    }
    FriBidiParType base_dir = FRIBIDI_PAR_LTR;
    b->resolve_count++;
    // Returns the highest level plus one, or 0 on failure
    if (fribidi_get_par_embedding_levels_ex(b->types, b->brackets, (FriBidiStrIndex)n_trim, &base_dir, b->para_levels) <= 1) return 0;
    for (uint32_t i = n_trim; i < n; i++) b->para_levels[i] = 0;
    return n_trim;
}

void
bidi_set_row_inactive(BidiMaps *b, index_type render_row) {
    if (!b->active[render_row]) return;
    b->active[render_row] = false;
    b->changed = true;
}

void
bidi_cache_invalidate(BidiMaps *b) {
    if (b->row_id) memset(b->row_id, 0, b->rows * sizeof(b->row_id[0]));
    b->ctx_linebuf = NULL;
}

void
bidi_cache_drop_screen_entries(BidiMaps *b) {
    // A paragraph starting on a screen row ends on one, so the last row's id tells whether a key mentions the screen
    for (index_type r = 0; r < b->rows; r++) {
        if ((b->row_id[r] | b->para_last[r]) & BIDI_ID_SCREEN) b->row_id[r] = 0;
    }
}

// Writes the row's maps from the visual order of its characters in b->map,
// setting b->changed if they differ from the row's previous maps.
static void
write_row(BidiMaps *b, index_type render_row, uint32_t row_first_char, uint32_t row_end_char) {
    const index_type cols = b->cols;
    uint16_t *v2l = row_of(v2l);
    uint8_t *levels = row_of(levels);
    bool differs = !b->active[render_row];
    index_type v = 0;
    for (uint32_t i = row_first_char; i < row_end_char; i++) {
        const uint32_t logical = (uint32_t)b->map[i];
        const index_type col = b->cell_of_char[logical] & 0xffff;
        const index_type next = logical + 1 < row_end_char ? (b->cell_of_char[logical + 1] & 0xffff) : cols;
        const uint8_t level = (uint8_t)b->para_levels[logical];
        for (index_type c = col; c < next; c++, v++) {
            // The renderer draws the first slice of a right-to-left multi-cell
            // glyph in its last cell, so those cells are shown last to first
            const uint16_t l = (uint16_t)((level & 1) ? col + next - 1 - c : c);
            differs |= (levels[c] != level) | (v2l[v] != l);
            levels[c] = level;
            v2l[v] = l;
        }
    }
    b->active[render_row] = true;
    if (differs) b->changed = true;
}

bool
bidi_resolve_paragraph(
    BidiMaps *b,
    const CPUCell *const *rows,
    unsigned num_rows,
    const TextCache *tc,
    int first_render_row,
    index_type min_render_row,
    index_type skip_render_row) {
    const uint32_t n_trim = resolve_levels(b, rows, num_rows, tc);
    if (n_trim) {
        for (uint32_t i = 0; i < b->row_start[num_rows]; i++) b->map[i] = (FriBidiStrIndex)i;
    }
    bool any_active = false;
    for (unsigned r = 0; r < num_rows; r++) {
        const int render_row = first_render_row + (int)r;
        if (render_row < (int)min_render_row || render_row >= (int)b->rows || (index_type)render_row == skip_render_row) continue;
        if (!n_trim) {
            bidi_set_row_inactive(b, (index_type)render_row);
            continue;
        }
        const uint32_t row_first = b->row_start[r], row_end = b->row_start[r + 1];
        const uint32_t reorder_end = r == num_rows - 1 ? n_trim : row_end;
        // Returns the highest level plus one, or 0 on failure
        if (reorder_end > row_first &&
            fribidi_reorder_line(
                0, b->types, (FriBidiStrIndex)(reorder_end - row_first), (FriBidiStrIndex)row_first, FRIBIDI_PAR_LTR, b->para_levels, NULL, b->map) > 1) {
            write_row(b, (index_type)render_row, row_first, row_end);
            any_active = true;
        } else bidi_set_row_inactive(b, (index_type)render_row);
    }
    return any_active;
}

static inline bool
row_has_map(const BidiMaps *b, index_type render_row, index_type x) {
    return render_row < b->rows && x < b->cols && b->active[render_row];
}

index_type
bidi_visual_x(const BidiMaps *b, index_type render_row, index_type x, index_type span) {
    if (!row_has_map(b, render_row, x)) return x;
    const uint16_t *v2l = row_of(v2l);
    // The cells of a character are adjacent in visual order, so its left edge
    // is the first visual column showing one of them
    const index_type end = x + MAX(span, 1u);
    for (index_type v = 0; v < b->cols; v++) {
        if (x <= v2l[v] && v2l[v] < end) return v;
    }
    return x;
}

index_type
bidi_logical_x(const BidiMaps *b, index_type render_row, index_type visual_x) {
    if (!row_has_map(b, render_row, visual_x)) return visual_x;
    return row_of(v2l)[visual_x];
}

bool
bidi_cell_is_rtl(const BidiMaps *b, index_type render_row, index_type x) {
    return row_has_map(b, render_row, x) && (row_of(levels)[x] & 1);
}

void
bidi_permute_row_bytes(BidiMaps *b, index_type render_row, uint8_t *row, size_t stride) {
    if (render_row >= b->rows || !b->active[render_row]) return;
    const size_t sz = (size_t)b->cols * stride;
    ensure_space_for(b, row_scratch, uint8_t, sz, row_scratch_cap, sz, false);
    memcpy(b->row_scratch, row, sz);
    const uint16_t *v2l = row_of(v2l);
    for (index_type v = 0; v < b->cols; v++) memcpy(row + v * stride, b->row_scratch + v2l[v] * stride, stride);
}

size_t
bidi_flag_misses(uint32_t *out, size_t cap) {
    size_t n = 0;
    for (char_type c = 0; c <= 0x10FFFF; c++) {
        const FriBidiCharType t = fribidi_get_bidi_type(c);
        if ((t == FRIBIDI_TYPE_RTL || t == FRIBIDI_TYPE_AL) && !bidi_codepoint_may_reorder(c)) {
            if (n < cap) out[n] = c;
            n++;
        }
    }
    return n;
}
