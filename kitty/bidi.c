/*
 * bidi.c
 *
 * Distributed under terms of the GPL3 license.
 */

#include "bidi.h"
#include <xxhash.h>

// Isolates fed to FriBidi as isolates per paragraph; later ones are neutrals. FriBidi's NEWS lists quadratic time in FSIs
// before 1.0.17 and in back-to-back isolates before 1.0.18.
#define BIDI_MAX_ISOLATES 32
// The longest walk from a bracket pair back to the previous strong char fed to a FriBidi before 1.0.17, which takes quadratic
// time in such walks
#define BIDI_N0_MAX_WALK 32
// Flags in cell_of_char: a char that takes no column, fed in its cell's column, and the separator fed for a scaled cell
#define BIDI_EXTRA_CHAR (1u << 31)
#define BIDI_FIXED_CELL (1u << 30)

// False for codepoints outside the blocks with right-to-left or Arabic letters
static inline bool
bidi_codepoint_may_reorder(char_type ch) {
    if (ch < 0x590) return false;
    if (ch < 0xfb1d) return ch <= 0x8ff;
    if (ch >= 0x1f000) return false;
    return ch <= 0xfdff || ch - 0xfe70 <= 0xfefe - 0xfe70 || ch - 0x10800 <= 0x10fff - 0x10800 || ch - 0x1e800 <= 0x1efff - 0x1e800;
}

// Keeps of a run of marks its first and last and the first of each other mark in it, each mark being a strong type of its own.
// The resolution reads of the run only its first type (P2), the types at its ends (W rules, N1, N2) and which types it holds
// (N0), never the levels of its marks. Returns the new length.
static inline unsigned
collapse_marks(char_type *run, unsigned len) {
    if (len < 3) return len;
    const char_type first = run[0], last = run[len - 1];
    unsigned n = 1;
    for (unsigned i = 1; i < len - 1; i++) {
        bool kept = run[i] == first || run[i] == last;
        for (unsigned j = 1; j < n && !kept; j++) kept = run[j] == run[i];
        if (!kept) run[n++] = run[i];
    }
    run[n++] = last;
    return n;
}

// Stores in buf the isolate controls and marks among the chars after the cell's first char, each run of marks collapsed
static inline unsigned
cell_extra_chars(const CPUCell *c, const TextCache *tc, char_type buf[MAX_NUM_CODEPOINTS_PER_CELL]) {
    if (!c->ch_is_idx || (c->is_multicell && (c->x || c->y))) return 0;
    ListOfChars lc = {.chars = buf, .capacity = MAX_NUM_CODEPOINTS_PER_CELL};
    if (!tc_chars_at_index_without_alloc(tc, c->ch_or_idx, &lc)) return 0;
    unsigned n = 0, run = 0;
    // A tab's second char is the number of cells it skips
    for (size_t i = lc.chars[0] == '\t' ? 2 : 1; i < lc.count; i++) {
        const char_type ch = lc.chars[i];
        if (ch - 0x2066u <= 0x2069u - 0x2066u) {
            n = run + collapse_marks(buf + run, n - run);
            buf[n++] = ch;
            run = n;
        } else if ((ch | 1) == 0x200f || ch == 0x61c) buf[n++] = ch;
    }
    return run + collapse_marks(buf + run, n - run);
}

// The char a cell is fed as. A multicell's text is shaped in the direction HarfBuzz guesses from all of it, so the multicell
// is fed as its first strong char, or its first char when it has none. Embeddings and overrides are strong to FriBidi but
// fed as neutrals (retype_controls()), so only letters count.
static inline char_type
cell_lead_char(const CPUCell *c, const TextCache *tc) {
    if (!c->is_multicell || !c->ch_is_idx || c->x || c->y) return cell_first_char(c, tc);
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    ListOfChars lc = {.chars = buf, .capacity = MAX_NUM_CODEPOINTS_PER_CELL};
    if (tc_chars_at_index_without_alloc(tc, c->ch_or_idx, &lc)) {
        for (size_t i = 0; i < lc.count; i++) {
            if (FRIBIDI_IS_LETTER(fribidi_get_bidi_type(lc.chars[i]))) return lc.chars[i];
        }
    }
    return cell_first_char(c, tc);
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

void
bidi_scratch_trim(BidiScratch *s) {
    if (s->peak && s->levels_cap > MAX(8 * s->peak, (size_t)1 << 16)) bidi_scratch_free(s);
    s->peak = 0;
}

bool
bidi_cells_may_reorder(const CPUCell *cells, index_type num, const TextCache *tc) {
    char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
    for (const CPUCell *c = cells, *end = cells + num; c < end; c++) {
        // Resolved so that its rows have levels, at which it is shaped in the direction guessed from its text
        if (c->is_multicell && c->scale > 1) return true;
        char_type ch = c->ch_or_idx;
        if (c->ch_is_idx) {
            if (c->is_multicell) {
                if (c->x || c->y) continue;
                // Any of its chars may be the one it is fed as
                ListOfChars lc = {.chars = buf, .capacity = MAX_NUM_CODEPOINTS_PER_CELL};
                if (tc_chars_at_index_without_alloc(tc, ch, &lc)) {
                    for (size_t i = 1; i < lc.count; i++) {
                        if (bidi_codepoint_may_reorder(lc.chars[i])) return true;
                    }
                }
            }
            ch = tc_first_char_at_index(tc, ch);
        }
        // An RLM written by OSC 66 as the only char of a cell is fed as a strong char
        if (bidi_codepoint_may_reorder(ch) || ch == 0x200f) return true;
        if (!c->ch_is_idx) continue;
        // Likewise an RLI, RLM or ALM after the first char
        for (unsigned i = 0, k = cell_extra_chars(c, tc, buf); i < k; i++) {
            if (buf[i] == 0x2067 || buf[i] == 0x200f || buf[i] == 0x61c) return true;
        }
    }
    return false;
}

// Appends the row's chars to s->text and returns the new length. Each column of a scaled cell, which spans several rows each
// showing a slice of the same glyphs, is a segment separator: the text on either side is reordered within its span.
static inline uint32_t
feed_row(BidiScratch *s, const CPUCell *cells, index_type cols, const TextCache *tc, uint32_t n) {
    FriBidiChar *restrict text = s->text + n, *const text_start = text;
    uint32_t *restrict cell_of_char = s->cell_of_char + n;
    for (index_type col = 0; col < cols; col++) {
        const CPUCell *c = cells + col;
        if (c->is_multicell) {
            if (c->scale > 1) {
                *text++ = '\t';
                *cell_of_char++ = col | BIDI_FIXED_CELL;
                continue;
            }
            if (col && c->x) continue;
        }
        const char_type ch = cell_lead_char(c, tc);
        *text++ = ch ? ch : ' ';
        *cell_of_char++ = col;
        if (!c->ch_is_idx) continue;
        char_type buf[MAX_NUM_CODEPOINTS_PER_CELL];
        // Within the scratch: bidi_resolve_paragraph() sized it for MAX_NUM_CODEPOINTS_PER_CELL chars per cell
        for (unsigned i = 0, k = cell_extra_chars(c, tc, buf); i < k; i++) {
            *text++ = buf[i];
            *cell_of_char++ = col | BIDI_EXTRA_CHAR;
        }
    }
    return n + (uint32_t)(text - text_start);
}

// Embeddings, overrides and isolate controls that start a cell (OSC 66) are fed as neutrals, and so are the isolates over
// the BIDI_MAX_ISOLATES cap. A PDI matches a capped initiator while any is open, as those open after the honoured ones.
static void
retype_controls(BidiScratch *s, uint32_t n) {
    unsigned honoured = 0, open_honoured = 0, open_capped = 0;
    for (uint32_t i = 0; i < n; i++) {
        const FriBidiCharType t = s->types[i];
        if (!(t & (FRIBIDI_MASK_EXPLICIT | FRIBIDI_MASK_ISOLATE))) continue;
        if ((t & FRIBIDI_MASK_EXPLICIT) || !(s->cell_of_char[i] & BIDI_EXTRA_CHAR)) s->types[i] = FRIBIDI_TYPE_ON;
        else if (t == FRIBIDI_TYPE_PDI) {
            if (open_capped) {
                open_capped--;
                s->types[i] = FRIBIDI_TYPE_ON;
            } else if (open_honoured) open_honoured--;
        } else if (honoured < BIDI_MAX_ISOLATES) {
            honoured++;
            open_honoured++;
        } else {
            open_capped++;
            s->types[i] = FRIBIDI_TYPE_ON;
        }
    }
}

static int n0_walk_is_slow = -1;

bool
bidi_n0_walk_is_slow(void) {
    // The version loaded, not the one built against: 1.0.16 to 1.0.18 share a soname. A version that does not parse is slow.
    if (n0_walk_is_slow < 0) {
        unsigned major, minor, micro;
        n0_walk_is_slow =
            !(sscanf(fribidi_version_info, "(GNU FriBidi) %u.%u.%u", &major, &minor, &micro) == 3 && (major > 1 || (major == 1 && (minor > 0 || micro >= 17))));
    }
    return n0_walk_is_slow;
}

// Brackets that BD16 does not pair within their isolating run sequence are fed as neutrals: FriBidi keeps pairs inside it only
// from 1.0.18. FriBidi before 1.0.17 also walks back from the opening bracket of every pair (N0) to the previous strong char,
// so with it a pair whose walk is longer than BIDI_N0_MAX_WALK chars is fed as two neutrals too.
static void
limit_bracket_pairs(BidiScratch *s, uint32_t n) {
    const bool slow_walk = bidi_n0_walk_is_slow();
    enum { DEPTHS = BIDI_MAX_ISOLATES + 1, STACK = FRIBIDI_BIDI_MAX_NESTED_BRACKET_PAIRS };
    struct {
        uint32_t at;
        bool near;
    } open[DEPTHS * STACK];
    struct {
        uint32_t base;
        bool full;
    } seq[DEPTHS] = {{0}};
    int64_t last_strong[DEPTHS];
    for (unsigned d = 0; d < DEPTHS; d++) last_strong[d] = -1;
    FriBidiBracketType *br = s->brackets;
    unsigned depth = 0;
    uint32_t top = 0;
#define unpair_open_down_to(bottom) \
    for (; top > (bottom); top--) br[open[top - 1].at] = FRIBIDI_NO_BRACKET
    for (uint32_t i = 0; i < n; i++) {
        const FriBidiCharType t = s->types[i];
        const FriBidiBracketType b = br[i];
        if (t & (FRIBIDI_MASK_LETTER | FRIBIDI_MASK_NUMBER)) last_strong[depth] = i;
        else if (FRIBIDI_IS_ISOLATE(t)) {
            if (t != FRIBIDI_TYPE_PDI) {
                depth++;
                last_strong[depth] = i;
                seq[depth].base = top;
                seq[depth].full = false;
            } else if (depth) {
                unpair_open_down_to(seq[depth].base);
                depth--;
            }
        } else if (b == FRIBIDI_NO_BRACKET) continue;
        else if (seq[depth].full) br[i] = FRIBIDI_NO_BRACKET;
        else if (FRIBIDI_IS_BRACKET_OPEN(b)) {
            if (top - seq[depth].base == STACK) {
                // BD16 stops for the rest of the isolating run sequence
                seq[depth].full = true;
                unpair_open_down_to(seq[depth].base);
                br[i] = FRIBIDI_NO_BRACKET;
            } else {
                open[top].at = i;
                open[top++].near = !slow_walk || (int64_t)i - last_strong[depth] <= BIDI_N0_MAX_WALK;
            }
        } else {
            uint32_t p = top;
            while (p > seq[depth].base && FRIBIDI_BRACKET_ID(br[open[p - 1].at]) != FRIBIDI_BRACKET_ID(b)) p--;
            if (p == seq[depth].base) br[i] = FRIBIDI_NO_BRACKET;
            else {
                unpair_open_down_to(p);
                if (!open[--top].near) br[open[top].at] = br[i] = FRIBIDI_NO_BRACKET;
            }
        }
    }
    unpair_open_down_to(0);
#undef unpair_open_down_to
}

bool
bidi_resolve_paragraph(
    BidiScratch *s, const CPUCell *const *rows, unsigned num_rows, index_type cols, const TextCache *tc, FriBidiParType base, uint8_t *levels) {
    ensure_space_for(s, row_start, uint32_t, num_rows + 1, row_start_cap, num_rows + 1, false);
    uint32_t n = 0;
    for (unsigned r = 0; r < num_rows; r++) {
        // A cell feeds one char plus at most one for each char after its first, a scaled cell one char per column
        const size_t cap = n + (size_t)cols * MAX_NUM_CODEPOINTS_PER_CELL;
        ensure_space_for(s, text, FriBidiChar, cap, text_cap, cap, false);
        ensure_space_for(s, cell_of_char, uint32_t, cap, cell_of_char_cap, cap, false);
        s->row_start[r] = n;
        n = feed_row(s, rows[r], cols, tc, n);
    }
    s->row_start[num_rows] = n;
    s->peak = MAX(s->peak, (size_t)n);
    ensure_space_for(s, types, FriBidiCharType, n, types_cap, n, false);
    ensure_space_for(s, brackets, FriBidiBracketType, n, brackets_cap, n, false);
    ensure_space_for(s, levels, FriBidiLevel, n, levels_cap, n, false);
    fribidi_get_bidi_types(s->text, (FriBidiStrIndex)n, s->types);
    retype_controls(s, n);
    fribidi_get_bracket_types(s->text, (FriBidiStrIndex)n, s->types, s->brackets);
    limit_bracket_pairs(s, n);
    FriBidiParType dir = base;
    // Returns the highest level plus one, 0 on failure; 1 means every level is 0, so nothing to reorder, though a scaled cell
    // still needs its level to be shaped at
    const FriBidiLevel max_level = fribidi_get_par_embedding_levels_ex(s->types, s->brackets, (FriBidiStrIndex)n, &dir, s->levels);
    if (!max_level) return false;
    if (max_level == 1) {
        bool fixed = false;
        for (uint32_t i = 0; i < n && !fixed; i++) fixed = s->cell_of_char[i] & BIDI_FIXED_CELL;
        if (!fixed) return false;
    }
    const FriBidiLevel base_level = FRIBIDI_DIR_TO_LEVEL(dir);
    for (unsigned r = 0; r < num_rows; r++) {
        const uint32_t row_first = s->row_start[r], row_end = s->row_start[r + 1];
        // L1 for the whitespace and isolate controls ending the row, which FriBidi resets only at the paragraph's end
        for (uint32_t i = row_end; i > row_first && (FRIBIDI_IS_EXPLICIT_OR_BN_OR_WS(s->types[i - 1]) || FRIBIDI_IS_ISOLATE(s->types[i - 1])); i--)
            s->levels[i - 1] = base_level;
        uint8_t *row = levels + (size_t)r * cols;
        index_type col = 0;
        uint8_t level = base_level;
        for (uint32_t i = row_first; i < row_end; i++) {
            const uint32_t entry = s->cell_of_char[i];
            if (entry & BIDI_EXTRA_CHAR) continue;
            for (const index_type c = entry & ~BIDI_FIXED_CELL; col < c; col++) row[col] = level;
            level = entry & BIDI_FIXED_CELL ? BIDI_LEVEL_FIXED : (uint8_t)s->levels[i];
        }
        for (; col < cols; col++) row[col] = level;
    }
    return true;
}

void
bidi_fix_scaled_cells(const CPUCell *const *rows, unsigned num_rows, index_type cols, uint8_t *levels) {
    for (unsigned r = 0; r < num_rows; r++) {
        for (index_type x = 0; x < cols; x++) {
            if (rows[r][x].is_multicell && rows[r][x].scale > 1) levels[(size_t)r * cols + x] = BIDI_LEVEL_FIXED;
        }
    }
}

bool
bidi_visual_order(const uint8_t *levels, index_type cols, uint16_t *v2l) {
#define level_at(x) ((uint8_t)(levels[x] & (BIDI_LEVEL_FIXED - 1)))
    uint8_t max_level = 0, min_level = UINT8_MAX;
    for (index_type x = 0; x < cols; x++) {
        max_level = MAX(max_level, level_at(x));
        min_level = MIN(min_level, level_at(x));
    }
    const unsigned min_odd = min_level | 1u;
    if (max_level < min_odd) return false;
    for (index_type x = 0; x < cols; x++) v2l[x] = (uint16_t)x;
    for (unsigned level = max_level; level >= min_odd; level--) {
        for (index_type x = 0; x < cols;) {
            if (level_at(x) < level) {
                x++;
                continue;
            }
            index_type end = x + 1;
            while (end < cols && level_at(end) >= level) end++;
            for (index_type a = x, b = end - 1; a < b; a++, b--) {
                const uint16_t t = v2l[a];
                v2l[a] = v2l[b];
                v2l[b] = t;
            }
            x = end;
        }
    }
#undef level_at
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

static void
bidi_maps_free_rows(BidiMaps *b) {
    free(b->levels);
    free(b->v2l);
    free(b->active);
    free(b->has_levels);
    free(b->relevel);
    free(b->below_hash);
    free(b->below_valid);
}

void
bidi_maps_free(BidiMaps *b) {
    if (!b) return;
    bidi_scratch_free(&b->scratch);
    bidi_maps_free_rows(b);
    free(b->row_levels);
    free(b->para_levels);
    free(b->para_rows);
    free(b);
}

void
bidi_maps_invalidate_row_hashes(Screen *self) {
    BidiMaps *b = self->bidi_maps;
    if (b && b->below_valid) memset(b->below_valid, 0, b->rows * sizeof(b->below_valid[0]));
}

// NULL for a screen wider than v2l can index, shown in logical order
BidiMaps *
bidi_maps_for_upload(Screen *self, const LineBuf *source, index_type rows) {
    if (self->columns > UINT16_MAX + 1u) return NULL;
    if (!self->bidi_maps && !(self->bidi_maps = calloc(1, sizeof(BidiMaps)))) fatal("Out of memory allocating bidi maps");
    BidiMaps *b = self->bidi_maps;
    const index_type cols = self->columns;
    if (b->rows != rows || b->cols != cols) {
        const size_t cells = (size_t)rows * cols;
        bidi_maps_free_rows(b);
        b->levels = calloc(cells, sizeof(b->levels[0]));
        b->v2l = malloc(cells * sizeof(b->v2l[0]));
        b->active = calloc(rows, sizeof(b->active[0]));
        b->has_levels = calloc(rows, sizeof(b->has_levels[0]));
        b->relevel = calloc(rows, sizeof(b->relevel[0]));
        b->below_hash = malloc(rows * sizeof(b->below_hash[0]));
        b->below_valid = calloc(rows, sizeof(b->below_valid[0]));
        if (!b->levels || !b->v2l || !b->active || !b->has_levels || !b->relevel || !b->below_hash || !b->below_valid)
            fatal("Out of memory allocating bidi maps");
        b->rows = rows;
        b->cols = cols;
        b->changed = true;
    }
    ensure_space_for(b, row_levels, uint8_t, cols, row_levels_cap, cols, false);
    b->source = source;
    return b;
}

// y < 0 is history line -(y + 1)
typedef struct BidiRows {
    Screen *screen;
    int lowest, end; // the rows that exist: [lowest, end)
    int top;         // the row shown in render row 0
    int pushed;      // the history rows y >= pushed were pushed since the last walk of a view of history rows only
} BidiRows;

static const CPUCell *
bidi_row_cells(const BidiRows *src, int y) {
    return y < 0 ? historybuf_cpu_cells(src->screen->historybuf, -(y + 1)) : linebuf_cpu_cells_for_line(src->screen->linebuf, y);
}

static LineAttrs *
bidi_row_attrs(const BidiRows *src, int y) {
    return y < 0 ? historybuf_attrs(src->screen->historybuf, -(y + 1)) : src->screen->linebuf->line_attrs + y;
}

// A history row has its render cells only in the history render cache, which holds the rows on view, so y must be on view or a
// linebuf row
static const RenderCell *
bidi_row_render_cells(const BidiRows *src, int y) {
    Line line;
    if (y < 0) historybuf_init_line_for_render(src->screen->historybuf, -(y + 1), &line, render_lines_for_screen(src->screen));
    else linebuf_init_line_at(src->screen->linebuf, y, &line);
    return line.render_cells;
}

static bool
bidi_levels_differ(const RenderCell *rc, const uint8_t *levels, index_type cols) {
    for (index_type x = 0; x < cols; x++) {
        if (rc[x].level != levels[x]) return true;
    }
    return false;
}

static void
bidi_set_row_levels(BidiMaps *b, index_type r, const uint8_t *levels) {
    const index_type cols = b->cols;
    uint8_t *dest = b->levels + (size_t)r * cols;
    if (!memcmp(dest, levels, cols)) return;
    memcpy(dest, levels, cols);
    bool any = false;
    for (index_type x = 0; x < cols && !any; x++) any = dest[x] != 0;
    b->has_levels[r] = any;
    b->active[r] = any && bidi_visual_order(dest, cols, b->v2l + (size_t)r * cols);
    b->changed = true;
}

static FriBidiParType
bidi_record_base(LineAttrs a) {
    return a.bidi_auto ? (a.bidi_rtl ? FRIBIDI_PAR_WRTL : FRIBIDI_PAR_WLTR) : (a.bidi_rtl ? FRIBIDI_PAR_RTL : FRIBIDI_PAR_LTR);
}

static uint64_t
bidi_row_hash(const BidiRows *src, int y, index_type cols) {
    const LineAttrs a = *bidi_row_attrs(src, y);
    // Without the upload's memo, which follows from the cells
    const uint64_t seed = (a.bidi_stamp != BIDI_STAMP_NONE) | a.bidi_rtl << 1 | a.bidi_auto << 2;
    return XXH3_64bits_withSeed(bidi_row_cells(src, y), cols * sizeof(CPUCell), seed);
}

static bool
bidi_row_changed(const BidiMaps *b, const BidiRows *src, int y, int end) {
    if (!bidi_row_attrs(src, y)->has_dirty_text) return false;
    if (y < end) return true;
    if (y < 0) return y >= src->pushed;
    return !b->below_valid[y] || bidi_row_hash(src, y, b->cols) != b->below_hash[y];
}

// Sets the levels of the rows on view of the paragraph [first, last]. Clean rows, and the row above the first one for its soft
// wrap, keep the levels they were rendered at; otherwise the paragraph is resolved again, as a dirty row's stored levels are
// stale, and so are those of a paragraph cut by a window edge that moved. Rows above the view are not checked: a push leaves
// them dirty and an edit before it dirtied the row below. Nor are history rows below the view pushed before the last walk of a
// view of history rows only, which evicted the others from the history render cache, nor screen rows below the view unchanged
// since the last shown resolve read them.
static void
bidi_map_paragraph(BidiMaps *b, const BidiRows *src, int first, int last, bool cut_by_moved_edge, bool shown) {
    const index_type cols = b->cols;
    const int top = src->top, end = top + (int)b->rows;
    bool dirty = cut_by_moved_edge;
    for (int y = MAX(first > src->lowest ? first - 1 : first, top); y <= last && !dirty; y++) dirty = bidi_row_changed(b, src, y, end);
    if (!dirty) {
        for (int y = MAX(first, top); y <= last && y < end; y++) {
            const RenderCell *rc = bidi_row_render_cells(src, y);
            for (index_type x = 0; x < cols; x++) b->row_levels[x] = rc[x].level;
            bidi_set_row_levels(b, y - top, b->row_levels);
            b->relevel[y - top] = false;
        }
        return;
    }
    b->resolved++;
    const TextCache *tc = src->screen->text_cache;
    const unsigned num_rows = last - first + 1;
    const size_t num_cells = (size_t)num_rows * cols;
    ensure_space_for(b, para_rows, const CPUCell *, num_rows, para_rows_cap, num_rows, false);
    ensure_space_for(b, para_levels, uint8_t, num_cells, para_levels_cap, num_cells, false);
    bool rtl = false, placeholders = false;
    for (int y = first; y <= last; y++) {
        LineAttrs *a = bidi_row_attrs(src, y);
        const CPUCell *cells = bidi_row_cells(src, y);
        b->para_rows[y - first] = cells;
        if (a->has_dirty_text && a->bidi_stamp != BIDI_STAMP_NONE)
            a->bidi_stamp = bidi_cells_may_reorder(cells, cols, tc) ? BIDI_STAMP_RTL : BIDI_STAMP_IMPLICIT;
        rtl |= a->bidi_stamp == BIDI_STAMP_RTL;
        placeholders |= a->has_image_placeholders;
    }
    const LineAttrs lead = *bidi_row_attrs(src, first);
    if (!(rtl || lead.bidi_rtl) || placeholders ||
        !bidi_resolve_paragraph(&b->scratch, b->para_rows, num_rows, cols, tc, bidi_record_base(lead), b->para_levels)) {
        memset(b->para_levels, 0, num_cells);
        // A scaled cell is shaped alike on every row it covers, which may be in a resolved paragraph. Without placeholders,
        // a stamped row holding one has its paragraph resolved. A row without a record keeps no levels, so its text is shaped
        // in the direction guessed from it, as with bidi mode off.
        for (int y = first; placeholders && y <= last; y++) {
            if (bidi_row_attrs(src, y)->bidi_stamp != BIDI_STAMP_NONE)
                bidi_fix_scaled_cells(b->para_rows + (y - first), 1, cols, b->para_levels + (size_t)(y - first) * cols);
        }
    }
    for (int y = first; y <= last; y++) {
        const uint8_t *levels = b->para_levels + (size_t)(y - first) * cols;
        if (top <= y && y < end) {
            b->relevel[y - top] = bidi_levels_differ(bidi_row_render_cells(src, y), levels, cols);
            bidi_set_row_levels(b, y - top, levels);
        } else if (!bidi_row_attrs(src, y)->has_dirty_text) {
            // A history row off view has its stored levels in the history render cache, if anywhere
            if (y < 0) historybuf_mark_line_dirty(src->screen->historybuf, -(y + 1));
            else if (bidi_levels_differ(bidi_row_render_cells(src, y), levels, cols)) linebuf_mark_line_dirty(src->screen->linebuf, y);
        }
    }
    if (!shown) return;
    for (int y = MAX(first, MAX(end, 0)); y <= last; y++) {
        b->below_hash[y] = bidi_row_hash(src, y, cols);
        b->below_valid[y] = true;
    }
}

// Sets the levels of every render row. A paragraph is resolved over the rows on view and a screen height above and below
// them, so a longer one is laid out from its part in that window. A walk not shown leaves the window of the last shown one,
// which the rendered levels are for.
static void
bidi_map_rows(BidiMaps *b, const BidiRows *src, bool shown) {
    const int top = src->top, end = top + (int)b->rows, margin = (int)src->screen->lines;
    const int lo = MAX(src->lowest, top - margin), hi = MIN(src->end, end + margin);
    const int64_t origin = (int64_t)src->screen->historybuf->num_pushed;
    const bool lo_moved = origin + lo != b->window_lo, hi_moved = origin + hi != b->window_hi;
    if (shown) {
        b->window_lo = origin + lo;
        b->window_hi = origin + hi;
    }
    b->resolved = 0;
    // A row on view is read and rendered without its hash being kept
    const int on_view = src->screen->linebuf == b->below_source ? MIN(end, src->end) : src->end;
    if (on_view > 0) memset(b->below_valid, 0, on_view * sizeof(b->below_valid[0]));
    b->below_source = src->screen->linebuf;
    memset(b->row_levels, 0, b->cols);
    for (int y = top; y < end; y++) {
        if (src->lowest <= y && y < src->end) continue;
        bidi_set_row_levels(b, y - top, b->row_levels);
        b->relevel[y - top] = false;
    }
    for (int y = MAX(top, src->lowest); y < MIN(end, src->end);) {
        int first = y, last = y;
        while (first > lo && range_line_is_continued(src->screen, first)) first--;
        while (last + 1 < hi && range_line_is_continued(src->screen, last + 1)) last++;
        const bool cut = (lo_moved && first == lo && range_line_is_continued(src->screen, first)) ||
                         (hi_moved && last + 1 == hi && range_line_is_continued(src->screen, hi));
        bidi_map_paragraph(b, src, first, last, cut, shown);
        y = last + 1;
    }
    // A dirty row's soft wrap can change the paragraph of the row below it, which is checked only once shown
    const int below = MIN(end, src->end);
    if (below < src->end && bidi_row_attrs(src, below - 1)->has_dirty_text) {
        if (below < 0) historybuf_mark_line_dirty(src->screen->historybuf, -(below + 1));
        else linebuf_mark_line_dirty(src->screen->linebuf, below);
    }
    bidi_scratch_trim(&b->scratch);
    b->valid = true;
}

// Levels left in the render cells would be trusted if the buffer were stamped again, so its rows holding any are rendered again
static void
bidi_unstamp(LineBuf *lb) {
    lb->bidi_stamped = false;
    Line line;
    for (index_type y = 0; y < lb->ynum; y++) {
        linebuf_init_line_at(lb, y, &line);
        bool any = false;
        for (index_type x = 0; x < lb->xnum; x++) {
            any |= line.render_cells[x].level != 0;
            line.render_cells[x].level = 0;
        }
        if (any) linebuf_mark_line_dirty(lb, y);
    }
}

// NULL for a frame shown in logical order. The buffer drops its flag once none of its rows has a record, unless the main one
// continues a history line while the history holds a record. A frame not shown (paused) does not mark its rows on view as
// rendered with the history rows below them.
BidiMaps *
bidi_prepare_live(Screen *self, bool shown) {
    if (OPT(force_ltr)) return NULL;
    LineBuf *lb = self->linebuf;
    HistoryBuf *hb = self->historybuf;
    if (lb->bidi_stamped && !(lb == self->main_linebuf && hb->bidi_stamped && hb->count && history_buf_endswith_wrap(hb))) {
        bool stamped = false;
        for (index_type y = 0; y < lb->ynum && !stamped; y++) stamped = lb->line_attrs[y].bidi_stamp != BIDI_STAMP_NONE;
        if (!stamped) bidi_unstamp(lb);
    }
    // The history is the main screen's, shown above the alternate screen too when scrolled back
    if (!bidi_live_frame_reorders(self)) return NULL;
    const index_type rows = render_lines_for_screen(self);
    BidiMaps *b = bidi_maps_for_upload(self, lb, rows);
    if (!b) return NULL;
    // History rows rendered without the walk hold stale levels, which it would trust
    if (!b->live) historybuf_invalidate_render_cache(hb);
    b->live = true;
    const BidiRows src = {
        .screen = self,
        .lowest = -(int)hb->count,
        .end = (int)self->lines,
        .top = -(int)(self->scrolled_by + pixel_scroll_enabled(self)),
        .pushed = -(int)MIN(hb->num_pushed - hb->bidi_walked, (uint64_t)hb->count)};
    Line line;
    for (int y = MAX(src.top, src.lowest); y < MIN(src.top + (int)rows, 0); y++) {
        if (historybuf_init_line_for_render(hb, -(y + 1), &line, rows)) historybuf_mark_line_dirty(hb, -(y + 1));
    }
    bidi_map_rows(b, &src, shown);
    if (shown && src.top >= src.lowest && src.top + (int)rows <= 0) hb->bidi_walked = hb->num_pushed;
    return b;
}

void
bidi_store_levels(const BidiMaps *b, index_type r, Line *line) {
    const uint8_t *levels = b->levels + (size_t)r * b->cols;
    for (index_type x = 0; x < b->cols; x++) line->render_cells[x].level = levels[x];
}

index_type
bidi_visual_x(const BidiMaps *b, index_type r, index_type x, index_type span) {
    if (r >= b->rows || x >= b->cols || !b->active[r]) return x;
    const uint16_t *v2l = b->v2l + (size_t)r * b->cols;
    const index_type end = x + MAX(span, 1u);
    for (index_type v = 0; v < b->cols; v++) {
        if (x <= v2l[v] && v2l[v] < end) return v;
    }
    return x;
}

index_type
bidi_logical_x(const BidiMaps *b, index_type r, index_type visual_x) {
    if (r >= b->rows || visual_x >= b->cols || !b->active[r]) return visual_x;
    return b->v2l[(size_t)r * b->cols + visual_x];
}

bool
bidi_cell_is_rtl(const BidiMaps *b, index_type r, index_type x) {
    return r < b->rows && x < b->cols && (b->levels[(size_t)r * b->cols + x] & 1);
}

// Blank the cell shown at visual column col of render row r if it is a half of a character whose other half is shown at the adjacent
// column inner, which the pre-edit block covers
void
bidi_blank_cut_half(const BidiMaps *b, const CPUCell *cells, index_type r, CellGPUData *row, index_type col, index_type inner) {
    const index_type l = bidi_logical_x(b, r, col), m = bidi_logical_x(b, r, inner);
    const CPUCell *c = cells + l, *d = cells + m;
    if (!c->is_multicell || !d->is_multicell || c->y != d->y || l - c->x != m - d->x) return;
    const RenderCell blank = {0};
    cells_for_gpu(c, &blank, 1, row + col);
}
