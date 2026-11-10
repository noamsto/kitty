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
    b->v2l = NULL;
    b->l2v = NULL;
    b->levels = NULL;
    b->active = NULL;
    b->row_scratch = NULL;
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
    zero_at_ptr(b);
}

bool
bidi_maps_ensure(BidiMaps *b, index_type rows, index_type cols) {
    if (b->v2l && b->rows == rows && b->cols == cols) return true;
    const bool had_maps = b->v2l != NULL;
    free_row_maps(b);
    if (had_maps) b->changed = true;
    if (!rows || !cols || cols > UINT16_MAX) return false;
    const size_t cells = (size_t)rows * cols;
    b->v2l = malloc(cells * sizeof(b->v2l[0]));
    b->l2v = malloc(cells * sizeof(b->l2v[0]));
    b->levels = malloc(cells * sizeof(b->levels[0]));
    b->active = calloc(rows, sizeof(b->active[0]));
    b->row_scratch = malloc(cols);
    if (!b->v2l || !b->l2v || !b->levels || !b->active || !b->row_scratch) {
        free_row_maps(b);
        return false;
    }
    b->rows = rows;
    b->cols = cols;
    return true;
}

void
bidi_set_row_inactive(BidiMaps *b, index_type render_row) {
    if (!b->v2l || render_row >= b->rows || !b->active[render_row]) return;
    b->active[render_row] = false;
    b->changed = true;
}

void
bidi_set_all_rows_inactive(BidiMaps *b) {
    for (index_type r = 0; r < b->rows; r++) bidi_set_row_inactive(b, r);
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

static void
set_paragraph_inactive(BidiMaps *b, int first_y, int last_y, int y_to_render_row, index_type first_render_row) {
    for (int y = first_y; y <= last_y; y++) {
        const int render_row = y + y_to_render_row;
        if (render_row >= (int)first_render_row) bidi_set_row_inactive(b, (index_type)render_row);
    }
}

// Writes the row's maps from the visual order of its characters in b->map.
static void
write_row(BidiMaps *b, index_type render_row, uint32_t row_first_char, uint32_t row_end_char) {
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
        for (index_type c = col; c < next; c++) levels[c] = level;
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
bidi_resolve_paragraph(BidiMaps *b, BidiCellSource src, void *ctx, int first_y, int last_y, int y_to_render_row, index_type first_render_row) {
    const int num_rows = last_y - first_y + 1;
    const index_type cols = b->cols;
    if (num_rows < 1 || num_rows > BIDI_MAX_PARAGRAPH_ROWS || !ensure_scratch(b, (size_t)num_rows * cols)) goto no_bidi;
    uint32_t n = 0;
    for (int r = 0; r < num_rows; r++) {
        b->row_start[r] = n;
        const CPUCell *cells = src.cells_at(ctx, first_y + r);
        for (index_type col = 0; col < cols; col++) {
            const CPUCell *c = cells + col;
            if (col && c->is_multicell && c->x) continue;
            const char_type ch = cell_first_char(c, src.text_cache);
            b->text[n] = ch ? ch : ' ';
            b->cell_of_char[n] = ((uint32_t)r << 16) | col;
            n++;
        }
    }
    b->row_start[num_rows] = n;

    fribidi_get_bidi_types(b->text, (FriBidiStrIndex)n, b->types);
    fribidi_get_bracket_types(b->text, (FriBidiStrIndex)n, b->types, b->brackets);
    FriBidiParType base_dir = FRIBIDI_PAR_LTR;
    if (fribidi_get_par_embedding_levels_ex(b->types, b->brackets, (FriBidiStrIndex)n, &base_dir, b->para_levels) <= 1) goto no_bidi;

    for (uint32_t i = 0; i < n; i++) b->map[i] = (FriBidiStrIndex)i;
    bool any_active = false;
    for (int r = 0; r < num_rows; r++) {
        const int render_row = first_y + r + y_to_render_row;
        if (render_row < (int)first_render_row || render_row >= (int)b->rows) continue;
        const uint32_t row_first = b->row_start[r], row_end = b->row_start[r + 1];
        const FriBidiLevel ret = fribidi_reorder_line(
            0, b->types, (FriBidiStrIndex)(row_end - row_first), (FriBidiStrIndex)row_first, FRIBIDI_PAR_LTR, b->para_levels, NULL, b->map);
        if (ret <= 1) {
            bidi_set_row_inactive(b, (index_type)render_row);
            continue;
        }
        write_row(b, (index_type)render_row, row_first, row_end);
        any_active = true;
    }
    return any_active;

no_bidi:
    set_paragraph_inactive(b, first_y, last_y, y_to_render_row, first_render_row);
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
