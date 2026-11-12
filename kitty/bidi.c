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
    free(b->l2v);
    free(b->levels);
    free(b->active);
    free(b->row_scratch);
    free(b->stamp_first);
    free(b->stamp_last);
    free(b->stamp_valid);
    free(b->rtl);
    free(b->sig_type);
    free(b->sig_bracket);
    free(b->sig_valid);
    free(b->line_id);
    free(b->prev_line_id);
    free(b->prev_active);
    free(b->prev_levels);
    b->v2l = NULL;
    b->l2v = NULL;
    b->levels = NULL;
    b->active = NULL;
    b->row_scratch = NULL;
    b->stamp_first = NULL;
    b->stamp_last = NULL;
    b->stamp_valid = NULL;
    b->rtl = NULL;
    b->sig_type = NULL;
    b->sig_bracket = NULL;
    b->sig_valid = NULL;
    b->line_id = NULL;
    b->prev_line_id = NULL;
    b->prev_active = NULL;
    b->prev_levels = NULL;
    b->snapshot_taken = false;
    b->hold = false;
    b->ids_history.valid = false;
    b->rows = 0;
    b->cols = 0;
}

void
bidi_maps_free(BidiMaps *b) {
    free_row_maps(b);
    free(b->text);
    free(b->types);
    free(b->brackets);
    free(b->para_levels);
    free(b->map);
    free(b->cell_of_char);
    free(b->row_start);
    free(b->paused_line_id);
    zero_at_ptr(b);
}

bool
bidi_maps_ensure(BidiMaps *b, index_type rows, index_type cols) {
    if (b->v2l && b->rows == rows && b->cols == cols) return true;
    const bool had_maps = b->v2l != NULL;
    free_row_maps(b);
    if (had_maps) {
        b->changed = true;
        bidi_cache_invalidate(b);
    }
    if (!rows || !cols || cols > UINT16_MAX) return false;
    const size_t cells = (size_t)rows * cols;
    b->v2l = malloc(cells * sizeof(b->v2l[0]));
    b->l2v = malloc(cells * sizeof(b->l2v[0]));
    b->levels = malloc(cells * sizeof(b->levels[0]));
    b->active = calloc(rows, sizeof(b->active[0]));
    b->row_scratch = malloc(cols);
    b->stamp_first = calloc(rows, sizeof(b->stamp_first[0]));
    b->stamp_last = calloc(rows, sizeof(b->stamp_last[0]));
    b->stamp_valid = calloc(rows, sizeof(b->stamp_valid[0]));
    b->rtl = calloc(rows, sizeof(b->rtl[0]));
    b->sig_type = malloc(cells * sizeof(b->sig_type[0]));
    b->sig_bracket = malloc(cells * sizeof(b->sig_bracket[0]));
    b->sig_valid = calloc(rows, sizeof(b->sig_valid[0]));
    b->line_id = calloc(rows, sizeof(b->line_id[0]));
    b->prev_line_id = calloc(rows, sizeof(b->prev_line_id[0]));
    b->prev_active = calloc(rows, sizeof(b->prev_active[0]));
    b->prev_levels = calloc(cells, sizeof(b->prev_levels[0]));
    if (!b->v2l || !b->l2v || !b->levels || !b->active || !b->row_scratch || !b->stamp_first || !b->stamp_last ||
        !b->stamp_valid || !b->rtl || !b->sig_type || !b->sig_bracket || !b->sig_valid || !b->line_id || !b->prev_line_id ||
        !b->prev_active || !b->prev_levels) {
        free_row_maps(b);
        return false;
    }
    b->rows = rows;
    b->cols = cols;
    return true;
}

static void
copy_snapshot(BidiMaps *b) {
    memcpy(b->prev_active, b->active, b->rows * sizeof(b->active[0]));
    memcpy(b->prev_levels, b->levels, (size_t)b->rows * b->cols * sizeof(b->levels[0]));
    b->snapshot_taken = true;
}

// Keeps last frame's state for the level compare; taken before the first change
// of a frame, as the compare looks lines up by their position last frame. A
// held snapshot counts as taken.
static void
take_snapshot(BidiMaps *b) {
    if (!b->snapshot_taken) copy_snapshot(b);
}

static void
row_inactive(BidiMaps *b, index_type render_row) {
    if (!b->active[render_row]) return;
    take_snapshot(b);
    b->active[render_row] = false;
    b->changed = true;
}

void
bidi_set_row_inactive(BidiMaps *b, index_type render_row) {
    if (!b->v2l || render_row >= b->rows) return;
    b->stamp_valid[render_row] = false;
    b->rtl[render_row] = false;
    b->sig_valid[render_row] = false;
    row_inactive(b, render_row);
}

void
bidi_set_all_rows_inactive(BidiMaps *b) {
    // The rows are drawn without levels from now on: compare with that instead
    b->hold = false;
    for (index_type r = 0; r < b->rows; r++) bidi_set_row_inactive(b, r);
}

static bool
keys_equal(const BidiFrameKey *a, const BidiFrameKey *k) {
    return a->linebuf == k->linebuf && a->historybuf == k->historybuf && a->linebuf_generation == k->linebuf_generation &&
           a->historybuf_generation == k->historybuf_generation && a->rows == k->rows && a->cols == k->cols &&
           a->scrolled_by == k->scrolled_by && a->row_offset == k->row_offset &&
           a->base == k->base;
}

void
bidi_forget_reused_history_ids(BidiMaps *b, const void *historybuf, uint64_t generation, uint64_t push_generation,
        index_type count, index_type start_of_data, index_type ynum) {
    if (!b->line_id) return;
    const uint64_t pushes = push_generation - b->ids_history.push_generation;
    uint64_t k = ynum; // slots reused by full-ring pushes; ynum or more means all
    if (b->ids_history.valid && b->ids_history.buf == historybuf && count >= b->ids_history.count &&
            generation - b->ids_history.generation == pushes) {
        // pushes only: each one that did not grow count reused the oldest slot
        k = pushes - (count - b->ids_history.count);
        if (k == 0) return;
    }
    for (index_type r = 0; r < b->rows; r++) {
        if ((b->line_id[r] >> 32) != BIDI_HISTORY_LINE_ID_KIND) continue;
        // forgotten slots are [start_of_data - k, start_of_data) mod ynum. Adding
        // ynum before subtracting keeps the unsigned offset from underflowing, as k < ynum
        const index_type slot = (index_type)(b->line_id[r] & 0xffffffffu);
        if (k >= ynum || (slot + ynum - (start_of_data + ynum - (index_type)k) % ynum) % ynum < k) b->line_id[r] = 0;
    }
}

void
bidi_frame_begin(BidiMaps *b, BidiFrameKey now, bool scroll_changed) {
    b->moved = !(b->key.valid && keys_equal(&b->key, &now) && !scroll_changed);
    b->key = now;
    b->key.valid = true;
    // A held snapshot and line_id are both from the last live frame
    b->snapshot_taken = b->hold;
    b->hold = false;
    if (!b->v2l) return;
    bidi_forget_reused_history_ids(b, now.historybuf, now.historybuf_generation, now.historybuf_push_generation,
        now.historybuf_count, now.historybuf_start, now.historybuf_ynum);
    if (b->moved) {
        memset(b->stamp_valid, 0, b->rows * sizeof(b->stamp_valid[0]));
        memset(b->sig_valid, 0, b->rows * sizeof(b->sig_valid[0]));
    }
    uint64_t *ids = b->prev_line_id;
    b->prev_line_id = b->line_id;
    b->line_id = ids;
    memset(b->line_id, 0, b->rows * sizeof(b->line_id[0]));
    b->ids_history.buf = now.historybuf;
    b->ids_history.generation = now.historybuf_generation;
    b->ids_history.push_generation = now.historybuf_push_generation;
    b->ids_history.count = now.historybuf_count;
    b->ids_history.valid = true;
}

void
bidi_frame_begin_paused(BidiMaps *b) {
    if (b->v2l && !b->hold) {
        copy_snapshot(b);
        b->hold = true;
    }
    b->moved = true;
    bidi_cache_invalidate(b);
}

void
bidi_cache_invalidate(BidiMaps *b) {
    b->key.valid = false;
    if (b->stamp_valid) memset(b->stamp_valid, 0, b->rows * sizeof(b->stamp_valid[0]));
    if (b->sig_valid) memset(b->sig_valid, 0, b->rows * sizeof(b->sig_valid[0]));
}

bool
bidi_paragraph_cached(const BidiMaps *b, int first, int last, int y_to_render_row, index_type first_render_row) {
    if (b->moved || !b->v2l) return false;
    for (int y = first; y <= last; y++) {
        const int r = y + y_to_render_row;
        if (r < (int)first_render_row || r >= (int)b->rows) continue;
        if (!b->stamp_valid[r] || b->stamp_first[r] != first || b->stamp_last[r] != last) return false;
    }
    return true;
}

void
bidi_set_line_id(BidiMaps *b, index_type render_row, uint64_t id) {
    if (b->line_id && render_row < b->rows) b->line_id[render_row] = id;
}

uint64_t *
bidi_paused_line_ids(BidiMaps *b, index_type rows) {
    b->paused_rows = 0;
    if (!rows) return NULL;
    if (rows > b->paused_cap) {
        free(b->paused_line_id);
        b->paused_line_id = malloc(rows * sizeof(b->paused_line_id[0]));
        b->paused_cap = b->paused_line_id ? rows : 0;
        if (!b->paused_line_id) return NULL;
    }
    b->paused_rows = rows;
    return b->paused_line_id;
}

// Marks line y dirty when its active bit or levels differ from those it had in
// the last live frame, wherever it was on screen then: a split or join can
// change the levels of a line whose text did not change, and its shaping
// depends on them. A line with no state from that frame is marked if it has
// levels now, as its glyphs may have been shaped at others.
static void
note_line_state(BidiMaps *b, BidiCellSource src, void *ctx, int y, index_type r) {
    if (!src.mark_dirty) return;
    const uint64_t id = src.line_id_at ? src.line_id_at(ctx, y) : 0;
    // Paused frames do not replace the last live frame's ids
    const uint64_t *prev_ids = b->hold ? b->line_id : b->prev_line_id;
    index_type p = r;
    if (id && prev_ids[p] != id) {
        p = b->id_hint + 1;
        if (p >= b->rows || prev_ids[p] != id) {
            for (p = 0; p < b->rows && prev_ids[p] != id; p++);
        }
    }
    if (!id || p >= b->rows) {
        if (b->active[r]) src.mark_dirty(ctx, y);
        return;
    }
    b->id_hint = p;
    // Before any change this frame, the live maps are still last frame's
    const bool *prev_active = b->snapshot_taken ? b->prev_active : b->active;
    const uint8_t *prev_levels = b->snapshot_taken ? b->prev_levels : b->levels;
    const size_t cols = b->cols;
    if (prev_active[p] != b->active[r] ||
        (b->active[r] && memcmp(prev_levels + p * cols, b->levels + r * cols, cols * sizeof(b->levels[0])) != 0))
        src.mark_dirty(ctx, y);
}

static void
settle_row(BidiMaps *b, BidiCellSource src, void *ctx, int y, index_type r, int first_y, int last_y, bool cacheable, bool rtl) {
    b->stamp_first[r] = first_y;
    b->stamp_last[r] = last_y;
    b->stamp_valid[r] = cacheable;
    b->rtl[r] = rtl;
    // Only bidi_resolve_paragraph sets a signature, after this
    b->sig_valid[r] = false;
    note_line_state(b, src, ctx, y, r);
}

static bool
ensure_scratch(BidiMaps *b, size_t cap) {
    if (!b->row_start) {
        b->row_start = malloc((BIDI_MAX_PARAGRAPH_ROWS + 1) * sizeof(b->row_start[0]));
        if (!b->row_start) return false;
    }
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
// cell a character starts in, a blank as U+0020. Returns the new length.
static uint32_t
feed_row(BidiMaps *b, const CPUCell *cells, const TextCache *text_cache, uint32_t r, uint32_t n) {
    for (index_type col = 0; col < b->cols; col++) {
        const CPUCell *c = cells + col;
        if (col && c->is_multicell && c->x) continue;
        const char_type ch = cell_first_char(c, text_cache);
        b->text[n] = ch ? ch : ' ';
        b->cell_of_char[n] = (r << 16) | col;
        n++;
    }
    return n;
}

// Stores the classes and bracket types of characters [row_first, row_end) at the
// cells they start in, after the row's settle_row
static void
write_signature(BidiMaps *b, index_type render_row, uint32_t row_first, uint32_t row_end) {
    uint32_t *types = row_of(sig_type), *brackets = row_of(sig_bracket);
    memset(types, 0, b->cols * sizeof(types[0]));
    memset(brackets, 0, b->cols * sizeof(brackets[0]));
    for (uint32_t i = row_first; i < row_end; i++) {
        const index_type col = b->cell_of_char[i] & 0xffff;
        types[col] = b->types[i];
        brackets[col] = b->brackets[i];
    }
    b->sig_valid[render_row] = true;
}

bool
bidi_row_signature_matches(BidiMaps *b, BidiCellSource src, void *ctx, int y, index_type render_row) {
    if (!b->v2l || render_row >= b->rows || !b->sig_valid[render_row] || !ensure_scratch(b, b->cols)) return false;
    const uint32_t n = feed_row(b, src.cells_at(ctx, y), src.text_cache, 0, 0);
    fribidi_get_bidi_types(b->text, (FriBidiStrIndex)n, b->types);
    fribidi_get_bracket_types(b->text, (FriBidiStrIndex)n, b->types, b->brackets);
    const uint32_t *types = row_of(sig_type), *brackets = row_of(sig_bracket);
    index_type col = 0;
    for (uint32_t i = 0; i < n; i++, col++) {
        // No class is 0, so a character starting in a cell that used to be a
        // continuation, or the reverse, never matches
        for (const index_type start = b->cell_of_char[i] & 0xffff; col < start; col++) {
            if (types[col]) return false;
        }
        if (types[col] != b->types[i] || brackets[col] != b->brackets[i]) return false;
    }
    for (; col < b->cols; col++) {
        if (types[col]) return false;
    }
    return true;
}

void
bidi_paragraph_inactive(BidiMaps *b, BidiCellSource src, void *ctx, int first_y, int last_y, int y_to_render_row,
    index_type first_render_row, bool cacheable) {
    if (!b->v2l) return;
    for (int y = first_y; y <= last_y; y++) {
        const int render_row = y + y_to_render_row;
        if (render_row < (int)first_render_row || render_row >= (int)b->rows) continue;
        row_inactive(b, (index_type)render_row);
        settle_row(b, src, ctx, y, (index_type)render_row, first_y, last_y, cacheable, false);
    }
}

// Writes the row's maps from the visual order of its characters in b->map.
static void
write_row(BidiMaps *b, index_type render_row, uint32_t row_first_char, uint32_t row_end_char) {
    take_snapshot(b);
    const index_type cols = b->cols;
    // l2v is rebuilt below, so it doubles as the buffer for the candidate
    // v2l, which has to be compared with the previous frame's before overwrite
    uint16_t *candidate = row_of(l2v), *v2l = row_of(v2l);
    uint8_t *levels = row_of(levels);
    index_type v = 0;
    for (uint32_t i = row_first_char; i < row_end_char; i++) {
        const uint32_t logical = (uint32_t)b->map[i];
        const index_type col = b->cell_of_char[logical] & 0xffff;
        const index_type next = logical + 1 < row_end_char ? (b->cell_of_char[logical + 1] & 0xffff) : cols;
        const uint8_t level = (uint8_t)b->para_levels[logical];
        levels[col] = level;
        if (next == col + 1) { candidate[v++] = (uint16_t)col; continue; }
        for (index_type c = col + 1; c < next; c++) levels[c] = level;
        if (level & 1) {
            for (index_type c = next; c-- > col;) candidate[v++] = (uint16_t)c;
        } else {
            for (index_type c = col; c < next; c++) candidate[v++] = (uint16_t)c;
        }
    }
    if (!b->active[render_row] || memcmp(v2l, candidate, cols * sizeof(v2l[0])) != 0) {
        memcpy(v2l, candidate, cols * sizeof(v2l[0]));
        b->active[render_row] = true;
        b->changed = true;
    }
    for (index_type i = 0; i < cols; i++) candidate[v2l[i]] = (uint16_t)i;
}

bool
bidi_resolve_paragraph(BidiMaps *b, BidiCellSource src, void *ctx, int first_y, int last_y, int y_to_render_row,
    index_type first_render_row, bool cacheable, BidiBase base) {
    const int num_rows = last_y - first_y + 1;
    const index_type cols = b->cols;
    FriBidiLevel max_level = 0;
    // A failed resolve leaves the rows unmapped for this frame only
    if (num_rows < 1 || num_rows > BIDI_MAX_PARAGRAPH_ROWS || !ensure_scratch(b, (size_t)num_rows * cols)) {
        cacheable = false;
        goto no_bidi;
    }
    uint32_t n = 0;
    for (int r = 0; r < num_rows; r++) {
        b->row_start[r] = n;
        n = feed_row(b, src.cells_at(ctx, first_y + r), src.text_cache, (uint32_t)r, n);
    }
    b->row_start[num_rows] = n;

    fribidi_get_bidi_types(b->text, (FriBidiStrIndex)n, b->types);
    fribidi_get_bracket_types(b->text, (FriBidiStrIndex)n, b->types, b->brackets);

    // Blanks at the paragraph's end take the paragraph level by L1 whatever precedes them, as
    // the last characters no W or N rule sees them as a neighbour, and P2-P3 skip them. Only at the end: interior removal can change W4.
    // Not with an explicit embedding: the blanks' level then sets the eos of the run before them (X10).
    uint32_t n_trim = n;
    if (!b->no_trim) {
        const uint32_t last_row_start = b->row_start[num_rows - 1];
        while (n_trim > last_row_start && b->text[n_trim - 1] == ' ') n_trim--;
        // the trimmed characters are all U+0020, so scanning the rest covers every type
        for (uint32_t i = 0; i < n_trim; i++) {
            if (FRIBIDI_IS_EXPLICIT(b->types[i])) { n_trim = n; break; }
        }
    }
    // ON takes the direction of the first strong character (P2-P3) and stays ON when there is none
    FriBidiParType base_dir = base == BIDI_BASE_RTL ? FRIBIDI_PAR_RTL : base == BIDI_BASE_AUTO ? FRIBIDI_PAR_ON : FRIBIDI_PAR_LTR;
    if (n_trim) {
        b->resolve_count++;
        // Returns the highest level plus one, or 0 on failure
        max_level = fribidi_get_par_embedding_levels_ex(b->types, b->brackets, (FriBidiStrIndex)n_trim, &base_dir, b->para_levels);
    } else if (base_dir == FRIBIDI_PAR_RTL) {
        // All blank under a forced RTL base: every cell takes level 1 below, so the row is reversed
        b->resolve_count++;
        max_level = 2;
    } else goto no_bidi;
    const bool rtl = base_dir == FRIBIDI_PAR_RTL;
    const FriBidiParType line_dir = rtl ? FRIBIDI_PAR_RTL : FRIBIDI_PAR_LTR;
    for (uint32_t i = n_trim; i < n; i++) b->para_levels[i] = rtl ? 1 : 0;
    if (!max_level) cacheable = false;
    if (max_level <= 1) goto no_bidi;

    for (uint32_t i = 0; i < n; i++) b->map[i] = (FriBidiStrIndex)i;
    bool any_active = false;
    for (int r = 0; r < num_rows; r++) {
        const int render_row = first_y + r + y_to_render_row;
        if (render_row < (int)first_render_row || render_row >= (int)b->rows) continue;
        const uint32_t row_first = b->row_start[r], row_end = b->row_start[r + 1];
        const uint32_t reorder_end = r == num_rows - 1 ? n_trim : row_end;
        FriBidiLevel ret = reorder_end <= row_first ? 0 : fribidi_reorder_line(
            0, b->types, (FriBidiStrIndex)(reorder_end - row_first), (FriBidiStrIndex)row_first, line_dir, b->para_levels, NULL, b->map);
        if (rtl && reorder_end < row_end) {
            // The trimmed blanks are at level 1 and after every other character, so only L2's
            // final, level 1 reversal moves them: reversed, to the left of FriBidi's order of the rest
            const uint32_t k = row_end - reorder_end;
            memmove(b->map + row_first + k, b->map + row_first, (reorder_end - row_first) * sizeof(b->map[0]));
            for (uint32_t j = 0; j < k; j++) b->map[row_first + j] = (FriBidiStrIndex)(row_end - 1 - j);
            // Every level is at least 1, so an all blank row is reordered too
            if (reorder_end == row_first) ret = 2;
        }
        if (ret <= 1) row_inactive(b, (index_type)render_row);
        else {
            write_row(b, (index_type)render_row, row_first, row_end);
            any_active = true;
        }
        settle_row(b, src, ctx, first_y + r, (index_type)render_row, first_y, last_y, cacheable, rtl);
        if (cacheable) write_signature(b, (index_type)render_row, row_first, row_end);
    }
    return any_active;

no_bidi:
    bidi_paragraph_inactive(b, src, ctx, first_y, last_y, y_to_render_row, first_render_row, cacheable);
    // FriBidi ran and found nothing to reorder: these inputs give inactive rows
    if (cacheable && max_level > 0) {
        for (int r = 0; r < num_rows; r++) {
            const int render_row = first_y + r + y_to_render_row;
            if (render_row < (int)first_render_row || render_row >= (int)b->rows) continue;
            write_signature(b, (index_type)render_row, b->row_start[r], b->row_start[r + 1]);
        }
    }
    return false;
}

index_type
bidi_visual_left_x(const BidiMaps *b, index_type render_row, index_type x, index_type cell_cols) {
    if (!b->v2l || render_row >= b->rows || x >= b->cols || !b->active[render_row]) return x;
    const uint16_t *l2v = row_of(l2v);
    index_type ans = l2v[x];
    const index_type limit = MIN(b->cols, x + MAX(cell_cols, 1u));
    for (index_type i = x + 1; i < limit; i++) ans = MIN(ans, (index_type)l2v[i]);
    return ans;
}

index_type
bidi_logical_x(const BidiMaps *b, index_type render_row, index_type visual_x) {
    if (!b->v2l || render_row >= b->rows || visual_x >= b->cols || !b->active[render_row]) return visual_x;
    return row_of(v2l)[visual_x];
}

bool
bidi_cell_is_rtl(const BidiMaps *b, index_type render_row, index_type x) {
    if (!b->v2l || render_row >= b->rows || x >= b->cols || !b->active[render_row]) return false;
    return row_of(levels)[x] & 1;
}

void
bidi_permute_row_bytes(BidiMaps *b, index_type render_row, uint8_t *row) {
    memcpy(b->row_scratch, row, b->cols);
    const uint16_t *v2l = row_of(v2l);
    for (index_type v = 0; v < b->cols; v++) row[v] = b->row_scratch[v2l[v]];
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
