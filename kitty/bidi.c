/*
 * bidi.c
 *
 * Distributed under terms of the GPL3 license.
 */

#include "bidi.h"

#define row_of(arr) (b->arr + (size_t)render_row * b->cols)
// In cell_of_char: an isolate control fed after its cell's first char, in the cell's column
#define BIDI_EXTRA_CHAR (1u << 31)

// Stores in buf the isolate controls (LRI RLI FSI PDI) among the chars of a cell after the first, returning how many.
// A cell holding more chars than buf takes, which nothing stores, has none for every caller.
static inline unsigned
cell_isolates(const CPUCell *c, const TextCache *tc, char_type buf[MAX_NUM_CODEPOINTS_PER_CELL]) {
    if (!c->ch_is_idx || (c->is_multicell && (c->x || c->y))) return 0;
    ListOfChars lc = {.chars = buf, .capacity = MAX_NUM_CODEPOINTS_PER_CELL};
    if (!tc_chars_at_index_without_alloc(tc, c->ch_or_idx, &lc)) return 0;
    unsigned n = 0;
    for (size_t i = 1; i < lc.count; i++) {
        if (lc.chars[i] - 0x2066u <= 0x2069u - 0x2066u) buf[n++] = lc.chars[i];
    }
    return n;
}

static bool
row_has_isolates(const CPUCell *cells, index_type num, const TextCache *tc) {
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    for (index_type x = 0; x < num; x++) {
        if (cell_isolates(cells + x, tc, buf)) return true;
    }
    return false;
}

static void
free_row_maps(BidiMaps *b) {
    free(b->v2l);
    free(b->levels);
    free(b->active);
    free(b->rtl);
    free(b->row_id);
    free(b->para_first);
    free(b->para_last);
    free(b->para_rows);
    free(b->sig);
    free(b->sig_valid);
    b->v2l = NULL;
    b->levels = NULL;
    b->active = NULL;
    b->rtl = NULL;
    b->row_id = NULL;
    b->para_first = NULL;
    b->para_last = NULL;
    b->para_rows = NULL;
    b->sig = NULL;
    b->sig_valid = NULL;
    b->ctx.linebuf = NULL;
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
    b->rtl = calloc(rows, sizeof(b->rtl[0]));
    b->row_id = calloc(rows, sizeof(b->row_id[0]));
    b->para_first = calloc(rows, sizeof(b->para_first[0]));
    b->para_last = calloc(rows, sizeof(b->para_last[0]));
    b->para_rows = calloc(rows, sizeof(b->para_rows[0]));
    b->sig = malloc(cells * sizeof(b->sig[0]));
    b->sig_valid = calloc(rows, sizeof(b->sig_valid[0]));
    if (!b->v2l || !b->levels || !b->active || !b->row_id || !b->para_first || !b->para_last || !b->para_rows || !b->sig ||
        !b->sig_valid || !b->rtl) {
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
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    for (const CPUCell *c = cells, *end = cells + num; c < end; c++) {
        char_type ch = c->ch_or_idx;
        if (c->ch_is_idx) {
            if (c->is_multicell && (c->x || c->y)) continue;
            ch = tc_first_char_at_index(tc, ch);
        }
        if (bidi_codepoint_may_reorder(ch)) return true;
        if (!c->ch_is_idx) continue;
        // An RLI reorders digits and neutrals without an RTL letter: a RLI 12 34 PDI shows a 34 12, a RLI bc! PDI a !bc
        for (unsigned i = 0, k = cell_isolates(c, tc, buf); i < k; i++) {
            if (buf[i] == 0x2067) return true;
        }
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

// Appends to b->text the characters row r of a paragraph feeds FriBidi: per cell a
// character starts in its first char, a blank as U+0020, then with feed_iso its
// isolate controls. Returns the new length, or UINT32_MAX for a row with a cell
// spanning several rows, which cannot be reordered.
static inline uint32_t
feed_row(BidiMaps *b, const CPUCell *cells, const TextCache *tc, uint32_t r, uint32_t n, bool feed_iso) {
    // Locals, as the stores through b->text could otherwise alias b's fields and make every iteration reload them
    FriBidiChar *restrict text = b->text + n, *const text_start = text;
    uint32_t *restrict cell_of_char = b->cell_of_char + n;
    const uint32_t row_bits = r << 16;
    const index_type cols = b->cols;
    for (index_type col = 0; col < cols; col++) {
        const CPUCell *c = cells + col;
        if (c->is_multicell) {
            if (c->scale > 1) return UINT32_MAX;
            if (col && c->x) continue;
        }
        const char_type ch = cell_first_char(c, tc);
        *text++ = ch ? ch : ' ';
        *cell_of_char++ = row_bits | col;
        if (!feed_iso || !c->ch_is_idx) continue;
        // Within the scratch: resolve_levels sized it for the controls cell_isolates() counted in these same cells
        char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
        for (unsigned i = 0, k = cell_isolates(c, tc, buf); i < k; i++) {
            *text++ = buf[i];
            *cell_of_char++ = row_bits | col | BIDI_EXTRA_CHAR;
        }
    }
    return n + (uint32_t)(text - text_start);
}

static void
retype_controls(BidiMaps *b, uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        // Embeddings and overrides are never honoured, nor isolates but the combined ones fed under the cap: OSC 66 can start
        // any number of cells with one, and FriBidi before 1.0.17 is quadratic in FSIs
        const FriBidiCharType t = b->types[i];
        if ((t & FRIBIDI_MASK_EXPLICIT) || ((t & FRIBIDI_MASK_ISOLATE) && !(b->cell_of_char[i] & BIDI_EXTRA_CHAR)))
            b->types[i] = FRIBIDI_TYPE_ON;
    }
}

#define SIG_BRACKET (1u << 31)
#define SIG_BLANK (1u << 30)

// Exact: FriBidi type values use bits below 30, and fribidi_get_bracket_types() gives a bracket only to an ON char, so the
// bracket form implies the type. The blank bit is needed because the trim drops only chars fed as U+0020 (U+2003 is WS
// too), and n_trim scales the N0 budget threshold.
// Branch-free so that a row's loop vectorizes.
static inline uint32_t
sig_of(FriBidiCharType type, FriBidiBracketType bracket, FriBidiChar ch) {
    const uint32_t bracket_sig = SIG_BRACKET | (uint32_t)FRIBIDI_IS_BRACKET_OPEN(bracket) << 21 | FRIBIDI_BRACKET_ID(bracket);
    return (bracket != FRIBIDI_NO_BRACKET ? bracket_sig : (uint32_t)type) | (ch == ' ' ? SIG_BLANK : 0);
}

// Resolves the levels of the paragraph's characters into b->para_levels from
// the base *base. Returns whether any row may be reordered. Then *base is the
// resolved FRIBIDI_PAR_LTR or FRIBIDI_PAR_RTL, *n_trim_out the count of
// characters FriBidi resolved, and the blank tail [*n_trim_out, n) is at the
// paragraph level.
static bool
resolve_levels(BidiMaps *b, const CPUCell *const *rows, unsigned num_rows, const TextCache *tc, FriBidiParType *base, uint32_t *n_trim_out) {
    b->resolve_cacheable = true;
    b->resolve_signed = false;
    if (!num_rows || num_rows > BIDI_MAX_PARAGRAPH_ROWS) return false;
    // Counted before anything is fed, as the scratch must hold the controls; over the cap none are fed, keeping FriBidi linear
    unsigned isolates = 0;
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    for (unsigned r = 0; r < num_rows && isolates <= BIDI_MAX_ISOLATES; r++) {
        for (index_type x = 0; x < b->cols && isolates <= BIDI_MAX_ISOLATES; x++) isolates += cell_isolates(rows[r] + x, tc, buf);
    }
    const bool feed_iso = isolates && isolates <= BIDI_MAX_ISOLATES;
    if (!ensure_scratch(b, (size_t)num_rows * b->cols + (feed_iso ? isolates : 0))) {
        b->resolve_cacheable = false;
        return false;
    }
    uint32_t n = 0;
    for (unsigned r = 0; r < num_rows; r++) {
        b->row_start[r] = n;
        n = feed_row(b, rows[r], tc, r, n, feed_iso);
        if (n == UINT32_MAX) {
            b->resolve_cacheable = false;
            return false;
        }
    }
    b->row_start[num_rows] = n;
    fribidi_get_bidi_types(b->text, (FriBidiStrIndex)n, b->types);
    // Blanks at the paragraph's end take the paragraph level (L1) whatever
    // precedes them, so they need not be resolved: only inside an explicit
    // embedding could they set the eos of the run before them (X10), and the
    // retype below leaves none.
    uint32_t n_trim = n;
    if (!b->no_trim) {
        while (n_trim > b->row_start[num_rows - 1] && b->text[n_trim - 1] == ' ') n_trim--;
    }
    retype_controls(b, n_trim);
    if (!n_trim) {
        // A right-to-left base puts the blanks at level 1. The brackets were not computed, so no signature is written.
        if (*base != FRIBIDI_PAR_RTL && *base != FRIBIDI_PAR_WRTL) return false;
        *base = FRIBIDI_PAR_RTL;
        for (uint32_t i = 0; i < n; i++) b->para_levels[i] = 1;
        b->resolve_count++;
        *n_trim_out = 0;
        return true;
    }
    fribidi_get_bracket_types(b->text, (FriBidiStrIndex)n, b->types, b->brackets);
    // FriBidi before 1.0.17 walks back to the previous strong char for every unresolved bracket pair (N0); scan bounds
    // that walk, and skipping N0 past the budget keeps every version linear.
    uint64_t scan = 0;
    int64_t last = -1;
    for (uint32_t i = 0; i < n_trim; i++) {
        if (b->types[i] & (FRIBIDI_MASK_LETTER | FRIBIDI_MASK_NUMBER)) last = i;
        else if (FRIBIDI_IS_BRACKET_OPEN(b->brackets[i])) scan += (uint64_t)(i - last);
    }
    const bool budget_fired = scan > (uint64_t)BIDI_N0_SCAN_PER_CHAR * n_trim;
    if (budget_fired) {
        for (uint32_t i = 0; i < n; i++) b->brackets[i] = FRIBIDI_NO_BRACKET;
    }
    b->resolve_count++;
    // Returns the highest level plus one: 0 is a failure, which must not be cached, and 1 is a paragraph with every level 0
    const FriBidiLevel max_level = fribidi_get_par_embedding_levels_ex(b->types, b->brackets, (FriBidiStrIndex)n_trim, base, b->para_levels);
    if (!max_level) {
        b->resolve_cacheable = false;
        return false;
    }
    // FriBidi leaves a weak base in place when no strong character decides
    *base = FRIBIDI_IS_RTL(*base) ? FRIBIDI_PAR_RTL : FRIBIDI_PAR_LTR;
    // A signature has no room for a cell's controls, and over the cap a row losing one changes the layout
    b->resolve_signed = !budget_fired && !isolates;
    // Only an LTR result can leave every level at 0, an RTL one has them all at 1 or more
    if (max_level <= 1) return false;
    for (uint32_t i = n_trim; i < n; i++) b->para_levels[i] = *base == FRIBIDI_PAR_RTL;
    *n_trim_out = n_trim;
    return true;
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
    b->ctx.linebuf = NULL;
}

void
bidi_cache_drop_screen_entries(BidiMaps *b) {
    // A paragraph starting on a screen row ends on one, so the last row's id tells whether a key mentions the screen
    for (index_type r = 0; r < b->rows; r++) {
        if ((b->row_id[r] | b->para_last[r]) & BIDI_ID_SCREEN) b->row_id[r] = 0;
    }
}

void
bidi_cache_move_rows(BidiMaps *b, index_type d) {
    const index_type kept = b->rows - d;
    const size_t cells = (size_t)kept * b->cols, skip = (size_t)d * b->cols;
#define move_cells(arr) memmove(b->arr, b->arr + skip, cells * sizeof(b->arr[0]))
#define move_rows(arr) memmove(b->arr, b->arr + d, kept * sizeof(b->arr[0]))
    move_cells(v2l);
    move_cells(levels);
    move_cells(sig);
    move_rows(active);
    move_rows(rtl);
    move_rows(sig_valid);
    move_rows(row_id);
    move_rows(para_first);
    move_rows(para_last);
    move_rows(para_rows);
#undef move_cells
#undef move_rows
    memset(b->active + kept, 0, d * sizeof(b->active[0]));
    memset(b->rtl + kept, 0, d * sizeof(b->rtl[0]));
    memset(b->sig_valid + kept, 0, d * sizeof(b->sig_valid[0]));
    memset(b->row_id + kept, 0, d * sizeof(b->row_id[0]));
    b->changed = true;
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
        const uint32_t logical = (uint32_t)b->map[i], at = b->cell_of_char[logical];
        const index_type col = at & 0xffff;
        index_type next = logical + 1 < row_end_char ? (b->cell_of_char[logical + 1] & 0xffff) : cols;
        const uint8_t level = (uint8_t)b->para_levels[logical];
        if (next == col + 1 && !(at & BIDI_EXTRA_CHAR)) {
            // The common char of one cell, without the loop's setup
            differs |= (levels[col] != level) | (v2l[v] != col);
            levels[col] = level;
            v2l[v++] = (uint16_t)col;
            continue;
        }
        // A cell's isolate controls follow its first char and take no column
        if (at & BIDI_EXTRA_CHAR) continue;
        if (next == col) {
            uint32_t j = logical + 1;
            while (j < row_end_char && (b->cell_of_char[j] & BIDI_EXTRA_CHAR)) j++;
            next = j < row_end_char ? (b->cell_of_char[j] & 0xffff) : cols;
        }
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

static void
write_signature(BidiMaps *b, index_type render_row, uint32_t row_first_char, uint32_t row_end_char) {
    uint32_t *restrict sig = row_of(sig);
    const index_type cols = b->cols;
    const FriBidiCharType *restrict types = b->types + row_first_char;
    const FriBidiBracketType *restrict brackets = b->brackets + row_first_char;
    const FriBidiChar *restrict text = b->text + row_first_char;
    const uint32_t n = row_end_char - row_first_char;
    // A signed row feeds at most one char per cell, so a full row has char c in cell c
    if (n == cols) {
        for (index_type c = 0; c < cols; c++) sig[c] = sig_of(types[c], brackets[c], text[c]);
        return;
    }
    const uint32_t *cell_of_char = b->cell_of_char + row_first_char;
    index_type c = 0;
    for (uint32_t i = 0; i < n; i++) {
        for (const index_type col = cell_of_char[i] & 0xffff; c < col; c++) sig[c] = 0;
        sig[c++] = sig_of(types[i], brackets[i], text[i]);
    }
    for (; c < cols; c++) sig[c] = 0;
}

bool
bidi_row_signature_matches(BidiMaps *b, const CPUCell *cells, const TextCache *tc, index_type render_row) {
    if (render_row >= b->rows || !b->sig_valid[render_row] || !ensure_scratch(b, b->cols)) return false;
    // A paragraph holding a control is never signed
    if (row_has_isolates(cells, b->cols, tc)) return false;
    const uint32_t n = feed_row(b, cells, tc, 0, 0, false);
    if (n == UINT32_MAX) return false;
    fribidi_get_bidi_types(b->text, (FriBidiStrIndex)n, b->types);
    // Over all n, as a resolve without the trim would: the trimmed blanks are WS, which the retype leaves alone
    retype_controls(b, n);
    fribidi_get_bracket_types(b->text, (FriBidiStrIndex)n, b->types, b->brackets);
    const uint32_t *sig = row_of(sig);
    index_type c = 0;
    for (uint32_t i = 0; i < n; i++) {
        // No signature is 0, so a char starting in a cell that used to be a continuation, or the reverse, never matches
        for (const index_type col = b->cell_of_char[i] & 0xffff; c < col; c++) {
            if (sig[c]) return false;
        }
        if (sig[c++] != sig_of(b->types[i], b->brackets[i], b->text[i])) return false;
    }
    for (; c < b->cols; c++) {
        if (sig[c]) return false;
    }
    return true;
}

bool
bidi_resolve_paragraph(
    BidiMaps *b, const CPUCell *const *rows, unsigned num_rows, const TextCache *tc, FriBidiParType base, int first_render_row, index_type min_render_row) {
    FriBidiParType dir = base;
    uint32_t n_trim = 0;
    const bool reorder = resolve_levels(b, rows, num_rows, tc, &dir, &n_trim);
    const bool rtl = reorder && dir == FRIBIDI_PAR_RTL;
    if (reorder) {
        for (uint32_t i = 0; i < b->row_start[num_rows]; i++) b->map[i] = (FriBidiStrIndex)i;
    }
    bool any_active = false;
    for (unsigned r = 0; r < num_rows; r++) {
        const int render_row = first_render_row + (int)r;
        if (render_row < (int)min_render_row || render_row >= (int)b->rows) continue;
        b->rtl[render_row] = rtl;
        // Also when FriBidi left every level at 0 and n_trim is 0
        if (b->resolve_signed) write_signature(b, (index_type)render_row, b->row_start[r], b->row_start[r + 1]);
        if (!reorder) {
            bidi_set_row_inactive(b, (index_type)render_row);
            continue;
        }
        const uint32_t row_first = b->row_start[r], row_end = b->row_start[r + 1];
        const uint32_t reorder_end = r == num_rows - 1 ? n_trim : row_end;
        // Returns the highest level plus one, or 0 on failure
        FriBidiLevel ret = 0;
        if (reorder_end > row_first) {
            ret = fribidi_reorder_line(
                0, b->types, (FriBidiStrIndex)(reorder_end - row_first), (FriBidiStrIndex)row_first, dir, b->para_levels, NULL, b->map);
        }
        if (rtl && reorder_end < row_end) {
            // The tail is at level 1 after every other char, so only L2's final level-1 reversal moves it: to the left of the
            // reordered part, in reverse. An all-blank row is reordered too.
            const uint32_t k = row_end - reorder_end;
            memmove(b->map + row_first + k, b->map + row_first, (reorder_end - row_first) * sizeof(b->map[0]));
            for (uint32_t j = 0; j < k; j++) b->map[row_first + j] = (FriBidiStrIndex)(row_end - 1 - j);
            if (reorder_end == row_first) ret = 2;
        }
        if (ret > 1) {
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

uint8_t
bidi_cell_level(const BidiMaps *b, index_type render_row, index_type x) {
    return row_has_map(b, render_row, x) ? row_of(levels)[x] : 0;
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
