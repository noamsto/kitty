/*
 * bidi.c
 *
 * Distributed under terms of the GPL3 license.
 */

#include "bidi.h"

#define row_of(arr) (b->arr + (size_t)render_row * b->cols)
// In cell_of_char: an isolate control fed with its cell, in the cell's column, after its first char or, for a cell with leading
// controls, before it
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

bool
bidi_row_starts_with_pdi(const CPUCell *cells, index_type num, const TextCache *tc) {
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    return num && cell_has_leading_controls(cells) && cell_isolates(cells, tc, buf) && buf[0] == 0x2069;
}

bool
bidi_cell_has_isolates(const CPUCell *c, const TextCache *tc) {
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    return cell_isolates(c, tc, buf) != 0;
}

bool
bidi_row_has_isolates(const CPUCell *cells, index_type num, const TextCache *tc) {
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    for (index_type x = 0; x < num; x++) {
        if (cell_isolates(cells + x, tc, buf)) return true;
    }
    return false;
}

bool
bidi_cell_is_gap(const CPUCell *c, const TextCache *tc) {
    if (c->is_multicell) return false;
    if (!c->ch_is_idx) return !c->ch_or_idx || c->ch_or_idx == ' ';
    if (cell_has_leading_controls(c)) return false;
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    ListOfChars lc = {.chars = buf, .capacity = MAX_NUM_CODEPOINTS_PER_CELL};
    if (!tc_chars_at_index_without_alloc(tc, c->ch_or_idx, &lc) || !lc.count || buf[0] != ' ') return false;
    return cell_isolates(c, tc, buf) == lc.count - 1;
}

index_type
bidi_cursor_gap(const CPUCell *cells, index_type cols, index_type x, const TextCache *tc) {
    if (!x || x >= cols || !bidi_cell_is_gap(cells + x - 1, tc)) return 0;
    for (index_type i = x; i < cols; i++) {
        if (!bidi_cell_is_gap(cells + i, tc)) return 0;
    }
    return x;
}

static void
free_row_maps(BidiMaps *b) {
    free(b->v2l);
    free(b->levels);
    free(b->active);
    free(b->rtl);
    free(b->region_row);
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
    b->region_row = NULL;
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
    free(b->probe_levels);
    free(b->map);
    free(b->cell_of_char);
    free(b->match);
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
    b->region_row = calloc(rows, sizeof(b->region_row[0]));
    b->row_id = calloc(rows, sizeof(b->row_id[0]));
    b->para_first = calloc(rows, sizeof(b->para_first[0]));
    b->para_last = calloc(rows, sizeof(b->para_last[0]));
    b->para_rows = calloc(rows, sizeof(b->para_rows[0]));
    b->sig = malloc(cells * sizeof(b->sig[0]));
    b->sig_valid = calloc(rows, sizeof(b->sig_valid[0]));
    if (!b->v2l || !b->levels || !b->active || !b->row_id || !b->para_first || !b->para_last || !b->para_rows || !b->sig ||
        !b->sig_valid || !b->rtl || !b->region_row) {
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

unsigned
bidi_row_memo(const CPUCell *cells, index_type num, const TextCache *tc) {
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    bool rtl = false;
    for (const CPUCell *c = cells, *end = cells + num; c < end; c++) {
        if (!c->ch_is_idx) {
            rtl |= bidi_codepoint_may_reorder(c->ch_or_idx);
            continue;
        }
        if (cell_isolates(c, tc, buf)) return BIDI_STAMP_ISOLATES;
        if (!(c->is_multicell && (c->x || c->y))) rtl |= bidi_codepoint_may_reorder(tc_first_char_at_index(tc, c->ch_or_idx));
    }
    return rtl ? BIDI_STAMP_RTL : BIDI_STAMP_IMPLICIT;
}

int
bidi_row_fold_isolates(const CPUCell *cells, index_type num, const TextCache *tc, int depth, bool *has_opener) {
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    for (index_type x = 0; x < num; x++) {
        for (unsigned i = 0, k = cell_isolates(cells + x, tc, buf); i < k; i++) {
            if (buf[i] == 0x2069) depth -= depth > 0;
            else {
                depth++;
                *has_opener = true;
            }
        }
    }
    return depth;
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
    grow(probe_levels);
    grow(map);
    grow(cell_of_char);
    grow(match);
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
        // Within the scratch: resolve_levels sized it for the controls cell_isolates() counted in these same cells
        char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
        const bool leading = feed_iso && c->ch_is_idx && cell_has_leading_controls(c);
        if (UNLIKELY(leading)) {
            for (unsigned i = 0, k = cell_isolates(c, tc, buf); i < k; i++) {
                *text++ = buf[i];
                *cell_of_char++ = row_bits | col | BIDI_EXTRA_CHAR;
            }
        }
        *text++ = ch ? ch : ' ';
        *cell_of_char++ = row_bits | col;
        if (leading || !feed_iso || !c->ch_is_idx) continue;
        for (unsigned i = 0, k = cell_isolates(c, tc, buf); i < k; i++) {
            *text++ = buf[i];
            *cell_of_char++ = row_bits | col | BIDI_EXTRA_CHAR;
        }
    }
    return n + (uint32_t)(text - text_start);
}

static void
retype_controls(BidiMaps *b, uint32_t start, uint32_t end) {
    for (uint32_t i = start; i < end; i++) {
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

// FriBidi before 1.0.17 walks back to the previous strong char for every unresolved bracket pair (N0) in [start, end); scan
// bounds that walk, and skipping N0 past the budget keeps every version linear.
static inline bool
n0_budget_fired(const BidiMaps *b, uint32_t start, uint32_t end) {
    uint64_t scan = 0;
    int64_t last = (int64_t)start - 1;
    for (uint32_t i = start; i < end; i++) {
        if (b->types[i] & (FRIBIDI_MASK_LETTER | FRIBIDI_MASK_NUMBER)) last = i;
        else if (FRIBIDI_IS_BRACKET_OPEN(b->brackets[i])) scan += (uint64_t)(i - last);
    }
    return scan > (uint64_t)BIDI_N0_SCAN_PER_CHAR * (end - start);
}

// In a paragraph that ends inside an isolate fed without its PDI, the blanks written right after a strong letter keep
// the letter's level instead of taking the paragraph's (L1), so a space typed after a word sits where the next word of the
// isolate's run will push it. Empty cells, and any blank after one, stay at the paragraph level. Retypes the kept blanks
// as the letter so that fribidi_reorder_line()'s own L1 leaves them. Returns their count, which follow the n_trim chars
// FriBidi resolved and end in the last row.
static uint32_t
keep_typed_blanks(BidiMaps *b, const CPUCell *last_row, uint32_t n_trim, uint32_t n) {
    const uint32_t letter = n_trim - 1;
    if ((b->cell_of_char[letter] & BIDI_EXTRA_CHAR) || !FRIBIDI_IS_LETTER(b->types[letter])) return 0;
    int depth = 0;
    for (uint32_t i = 0; i < n_trim; i++) {
        if (b->types[i] == FRIBIDI_TYPE_PDI) depth -= depth > 0;
        else if (FRIBIDI_IS_ISOLATE(b->types[i])) depth++;
    }
    if (!depth) return 0;
    uint32_t k = 0;
    while (n_trim + k < n && cell_is_char(last_row + (b->cell_of_char[n_trim + k] & 0xffff), ' ')) k++;
    for (uint32_t i = n_trim; i < n_trim + k; i++) {
        b->para_levels[i] = b->para_levels[letter];
        b->types[i] = b->types[letter];
    }
    return k;
}

static inline bool
is_strong_or_number(FriBidiCharType t) {
    return t == FRIBIDI_TYPE_LTR || t == FRIBIDI_TYPE_RTL || t == FRIBIDI_TYPE_AL || t == FRIBIDI_TYPE_EN || t == FRIBIDI_TYPE_AN;
}

// The cursor gap, in the last row from row_first, when the cells from gap_x on hold only blanks and controls: the blanks
// between the last strong letter or number p and the cursor, and the neutrals and controls among and before them, take the
// levels FriBidi gives them when a probe char follows them instead of the paragraph level (L1) and eos (N1/N2). So a space
// typed after an RTL word shows where the next char of its run will push it. The probe stands for that char: R after an
// odd-level p, else AN after an AN and EN otherwise. It goes right after the gap's last blank, so controls after that blank
// stay after it, as tmux moves its closing PDI past the next char typed. It applies only when p is off the paragraph level,
// as at that level the next char joins the paragraph's run and L1 already shows it, when keep_typed_blanks() left the
// gap's last blank at the paragraph level, when the probe moves no level up to p, and when the last blank ends off the
// paragraph level. Retypes the gap's blanks as their level's direction so that fribidi_reorder_line()'s own L1 leaves
// them. Returns the new n_trim, one past the gap, or n_trim unchanged.
static uint32_t
show_typed_gap(BidiMaps *b, uint32_t row_first, uint32_t gap_x, FriBidiLevel par, uint32_t n_trim, uint32_t n) {
    uint32_t c0 = row_first;
    while (c0 < n && (b->cell_of_char[c0] & 0xffff) < gap_x) c0++;
    for (uint32_t i = c0; i < n; i++) {
        if (b->text[i] != ' ' && !(b->cell_of_char[i] & BIDI_EXTRA_CHAR)) return n_trim;
    }
    // A blank kept by keep_typed_blanks() is retyped as its letter, so p may be one, standing for the letter
    uint32_t p = c0;
    while (p > row_first && !is_strong_or_number(b->types[p - 1])) p--;
    if (p == row_first) return n_trim;
    p--;
    uint32_t P = 0;
    for (uint32_t i = c0; i > p + 1 && !P; i--) {
        if (b->text[i - 1] == ' ' && !(b->cell_of_char[i - 1] & BIDI_EXTRA_CHAR)) P = i;
    }
    // Only [0, n_trim) is resolved; a strong char is never trimmed, but a kept blank stands for one
    if (!P || p >= n_trim) return n_trim;
    const FriBidiLevel lp = b->para_levels[p];
    if (lp == par || (P - 1 < n_trim && b->para_levels[P - 1] != par)) return n_trim;
    // [P, n) holds only blanks and controls, which FriBidi's own L1 puts at the paragraph level
    const uint32_t len = MAX(n_trim, P);
    const FriBidiCharType probe = lp & 1 ? FRIBIDI_TYPE_RTL : b->types[p] == FRIBIDI_TYPE_AN ? FRIBIDI_TYPE_AN : FRIBIDI_TYPE_EN;
    memmove(b->types + P + 1, b->types + P, (n - P) * sizeof(b->types[0]));
    memmove(b->brackets + P + 1, b->brackets + P, (n - P) * sizeof(b->brackets[0]));
    b->types[P] = probe;
    b->brackets[P] = FRIBIDI_NO_BRACKET;
    // The resolved direction, so that the probe cannot flip an auto paragraph
    FriBidiParType dir = par ? FRIBIDI_PAR_RTL : FRIBIDI_PAR_LTR;
    const FriBidiLevel max_level = fribidi_get_par_embedding_levels_ex(b->types, b->brackets, (FriBidiStrIndex)(len + 1), &dir, b->probe_levels);
    memmove(b->types + P, b->types + P + 1, (n - P) * sizeof(b->types[0]));
    memmove(b->brackets + P, b->brackets + P + 1, (n - P) * sizeof(b->brackets[0]));
    if (!max_level) {
        b->resolve_cacheable = false;
        return n_trim;
    }
    for (uint32_t i = 0; i <= p; i++) {
        if (b->probe_levels[i] != b->para_levels[i]) return n_trim;
    }
    if (b->probe_levels[P - 1] == par) return n_trim;
    for (uint32_t i = p + 1; i < P; i++) {
        b->para_levels[i] = b->probe_levels[i];
        if (b->text[i] == ' ' && !(b->cell_of_char[i] & BIDI_EXTRA_CHAR)) b->types[i] = b->para_levels[i] & 1 ? FRIBIDI_TYPE_RTL : FRIBIDI_TYPE_LTR;
    }
    // The retyped blanks must not reach a signature
    b->resolve_signed = false;
    return P;
}

// Resolves the levels of the paragraph's characters into b->para_levels from
// the base *base. Returns whether any row may be reordered. Then *base is the
// resolved FRIBIDI_PAR_LTR or FRIBIDI_PAR_RTL, *n_trim_out the count of
// characters resolved, and the tail [*n_trim_out, n) is at the paragraph
// level. Resolved are the characters FriBidi was given, the typed blanks
// kept by keep_typed_blanks() and the cursor gap of show_typed_gap(). gap_x is
// the cursor's column on the last row when the gap may apply, else 0.
static bool
resolve_levels(BidiMaps *b, const CPUCell *const *rows, unsigned num_rows, const TextCache *tc, FriBidiParType *base, uint32_t *n_trim_out, uint32_t gap_x) {
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
    // One more char for show_typed_gap()'s probe
    if (!ensure_scratch(b, (size_t)num_rows * b->cols + (feed_iso ? isolates : 0) + 1)) {
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
    // retype below leaves none. The exceptions, deviations from L1, are
    // keep_typed_blanks() and the cursor gap of show_typed_gap().
    uint32_t n_trim = n;
    if (!b->no_trim) {
        while (n_trim > b->row_start[num_rows - 1] && b->text[n_trim - 1] == ' ') n_trim--;
    }
    retype_controls(b, 0, n_trim);
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
    const bool budget_fired = n0_budget_fired(b, 0, n_trim);
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
    if (feed_iso && n_trim < n && !b->no_trim) n_trim += keep_typed_blanks(b, rows[num_rows - 1], n_trim, n);
    if (gap_x && !b->no_trim) n_trim = show_typed_gap(b, b->row_start[num_rows - 1], gap_x, *base == FRIBIDI_PAR_RTL, n_trim, n);
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
    move_rows(region_row);
    move_rows(sig_valid);
    move_rows(row_id);
    move_rows(para_first);
    move_rows(para_last);
    move_rows(para_rows);
#undef move_cells
#undef move_rows
    memset(b->active + kept, 0, d * sizeof(b->active[0]));
    memset(b->rtl + kept, 0, d * sizeof(b->rtl[0]));
    memset(b->region_row + kept, 0, d * sizeof(b->region_row[0]));
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
        // A cell's isolate controls, before or after its first char, take no column
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
    if (bidi_row_has_isolates(cells, b->cols, tc)) return false;
    const uint32_t n = feed_row(b, cells, tc, 0, 0, false);
    if (n == UINT32_MAX) return false;
    fribidi_get_bidi_types(b->text, (FriBidiStrIndex)n, b->types);
    // Over all n, as a resolve without the trim would: the trimmed blanks are WS, which the retype leaves alone
    retype_controls(b, 0, n);
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
    BidiMaps *b,
    const CPUCell *const *rows,
    unsigned num_rows,
    const TextCache *tc,
    FriBidiParType base,
    int first_render_row,
    index_type min_render_row,
    index_type gap_x) {
    FriBidiParType dir = base;
    uint32_t n_trim = 0;
    const bool reorder = resolve_levels(b, rows, num_rows, tc, &dir, &n_trim, gap_x);
    const bool rtl = reorder && dir == FRIBIDI_PAR_RTL;
    if (reorder) {
        for (uint32_t i = 0; i < b->row_start[num_rows]; i++) b->map[i] = (FriBidiStrIndex)i;
    }
    bool any_active = false;
    for (unsigned r = 0; r < num_rows; r++) {
        const int render_row = first_render_row + (int)r;
        if (render_row < (int)min_render_row || render_row >= (int)b->rows) continue;
        b->rtl[render_row] = rtl;
        b->region_row[render_row] = false;
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

#define NO_MATCH UINT32_MAX

// Pairs the isolate controls of a fed row as BD9 does, a PDI closing the nearest open initiator: b->match of an initiator
// is its PDI's index, or NO_MATCH when the row leaves it open or it is no control. While an initiator is open its entry
// links to the one open before it, making the stack.
static void
match_isolates(BidiMaps *b, uint32_t n) {
    uint32_t top = NO_MATCH;
    for (uint32_t k = 0; k < n; k++) {
        b->match[k] = NO_MATCH;
        if (!(b->cell_of_char[k] & BIDI_EXTRA_CHAR)) continue;
        if (b->text[k] != 0x2069) {
            b->match[k] = top;
            top = k;
        } else if (top != NO_MATCH) {
            const uint32_t open = top;
            top = b->match[open];
            b->match[open] = k;
        }
    }
    while (top != NO_MATCH) {
        const uint32_t below = b->match[top];
        b->match[top] = NO_MATCH;
        top = below;
    }
}

// Feeds the row with its isolate controls, types its chars and pairs its controls. Returns the char count, or UINT32_MAX
// when the scratch cannot be had or a cell spans several rows.
static uint32_t
feed_region_row(BidiMaps *b, const CPUCell *cells, const TextCache *tc) {
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    size_t isolates = 0;
    for (index_type x = 0; x < b->cols; x++) isolates += cell_isolates(cells + x, tc, buf);
    if (!ensure_scratch(b, (size_t)b->cols + isolates)) return UINT32_MAX;
    const uint32_t n = feed_row(b, cells, tc, 0, 0, true);
    if (n == UINT32_MAX) return n;
    fribidi_get_bidi_types(b->text, (FriBidiStrIndex)n, b->types);
    match_isolates(b, n);
    return n;
}

// Finds the next region of a paired row from char *i: the chars strictly between an initiator and its PDI, for a pair no
// other pair encloses. An initiator left open makes no region, though the pairs after or inside it still do: a field
// truncated to its width loses its PDI, and the cells after the field must stay put. Sets *open and *close to the
// initiator's and the PDI's index.
static bool
next_region(const BidiMaps *b, uint32_t n, uint32_t *i, uint32_t *open, uint32_t *close) {
    for (uint32_t k = *i; k < n; k++) {
        if (b->match[k] == NO_MATCH) continue;
        *open = k;
        *close = b->match[k];
        *i = *close + 1;
        return true;
    }
    *i = n;
    return false;
}

bool
bidi_resolve_regions(BidiMaps *b, const CPUCell *cells, const TextCache *tc, index_type render_row) {
    b->resolve_cacheable = true;
    b->resolve_signed = false;
    b->rtl[render_row] = false;
    b->region_row[render_row] = false;
    const uint32_t n = feed_region_row(b, cells, tc);
    if (n == UINT32_MAX) {
        b->resolve_cacheable = false;
        bidi_set_row_inactive(b, render_row);
        return false;
    }
    memset(b->para_levels, 0, n * sizeof(b->para_levels[0]));
    for (uint32_t i = 0; i < n; i++) b->map[i] = (FriBidiStrIndex)i;
    bool any = false;
    for (uint32_t i = 0, open = 0, close = 0; next_region(b, n, &i, &open, &close);) {
        const uint32_t s = open + 1, len = close - s;
        if (!len) continue;
        unsigned nested = 0;
        bool may_reorder = b->text[open] == 0x2067;
        for (uint32_t k = s; k < close; k++) {
            if (b->cell_of_char[k] & BIDI_EXTRA_CHAR) {
                nested++;
                may_reorder |= b->text[k] == 0x2067;
            } else may_reorder |= bidi_codepoint_may_reorder(b->text[k]);
        }
        if (!may_reorder) continue;
        retype_controls(b, s, close);
        // Each region is its own FriBidi call, so the cap is per region; over it the nested controls are inert
        if (nested > BIDI_MAX_ISOLATES) {
            for (uint32_t k = s; k < close; k++) {
                if (b->cell_of_char[k] & BIDI_EXTRA_CHAR) b->types[k] = FRIBIDI_TYPE_ON;
            }
        }
        fribidi_get_bracket_types(b->text + s, (FriBidiStrIndex)len, b->types + s, b->brackets + s);
        if (n0_budget_fired(b, s, close)) {
            for (uint32_t k = s; k < close; k++) b->brackets[k] = FRIBIDI_NO_BRACKET;
        }
        FriBidiParType base = b->text[open] == 0x2066 ? FRIBIDI_PAR_LTR : b->text[open] == 0x2067 ? FRIBIDI_PAR_RTL : FRIBIDI_PAR_WLTR;
        b->resolve_count++;
        // Both return the highest level plus one, or 0 on failure
        const FriBidiLevel max_level =
            fribidi_get_par_embedding_levels_ex(b->types + s, b->brackets + s, (FriBidiStrIndex)len, &base, b->para_levels + s);
        if (max_level == 1) continue;
        if (max_level) {
            base = FRIBIDI_IS_RTL(base) ? FRIBIDI_PAR_RTL : FRIBIDI_PAR_LTR;
            // As one line, so L1 puts the region's trailing blanks at its base level: a short RTL region is right-aligned
            if (fribidi_reorder_line(0, b->types, (FriBidiStrIndex)len, (FriBidiStrIndex)s, base, b->para_levels, NULL, b->map)) {
                any = true;
                continue;
            }
            for (uint32_t k = s; k < close; k++) b->map[k] = (FriBidiStrIndex)k;
        }
        b->resolve_cacheable = false;
        memset(b->para_levels + s, 0, len * sizeof(b->para_levels[0]));
    }
    if (!any) {
        bidi_set_row_inactive(b, render_row);
        return false;
    }
    write_row(b, render_row, 0, n);
    b->region_row[render_row] = true;
    return true;
}

// Whether FriBidi's P2 finds an FSI region right-to-left: its first L, R or AL char outside nested isolates, which are
// skipped only when fed, as over the cap they are retyped ON
static bool
fsi_region_is_rtl(const BidiMaps *b, uint32_t start, uint32_t end) {
    unsigned nested = 0;
    for (uint32_t k = start; k < end; k++) nested += (b->cell_of_char[k] & BIDI_EXTRA_CHAR) != 0;
    const bool fed = nested <= BIDI_MAX_ISOLATES;
    unsigned depth = 0;
    for (uint32_t k = start; k < end; k++) {
        if (b->cell_of_char[k] & BIDI_EXTRA_CHAR) {
            if (!fed) continue;
            if (b->text[k] == 0x2069) depth -= depth > 0;
            else depth++;
        } else if (!depth && FRIBIDI_IS_LETTER(b->types[k])) return FRIBIDI_IS_RTL(b->types[k]);
    }
    return false;
}

bool
bidi_region_at(BidiMaps *b, const CPUCell *cells, const TextCache *tc, index_type x, index_type *start, index_type *end, bool *rtl) {
    const uint32_t n = feed_region_row(b, cells, tc);
    if (n == UINT32_MAX) return false;
    for (uint32_t i = 0, open = 0, close = 0; next_region(b, n, &i, &open, &close);) {
        // The region's cells are those its chars start in: controls take no cell, so a region of controls only has none
        uint32_t first = open + 1, last = close;
        while (first < close && (b->cell_of_char[first] & BIDI_EXTRA_CHAR)) first++;
        if (first == close) continue;
        while (b->cell_of_char[last - 1] & BIDI_EXTRA_CHAR) last--;
        const index_type lo = b->cell_of_char[first] & 0xffff;
        // Regions come in column order
        if (x < lo) return false;
        const index_type last_col = b->cell_of_char[last - 1] & 0xffff;
        const CPUCell *c = cells + last_col;
        const index_type hi = MIN(last_col + (c->is_multicell ? mcd_x_limit(c) - c->x : 1u), b->cols);
        if (x >= hi) continue;
        *start = lo;
        *end = hi;
        *rtl = b->text[open] == 0x2068 ? fsi_region_is_rtl(b, open + 1, close) : b->text[open] == 0x2067;
        return true;
    }
    return false;
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
