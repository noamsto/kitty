/*
 * bidi.h
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "data-types.h"
#include "line.h"
#include <fribidi.h>

typedef enum { BIDI_DIR_GUESS = 0, BIDI_DIR_LTR = 1, BIDI_DIR_RTL = 2 } BidiDirection;

// Uses OPT() from state.h, which cannot be included here as it includes screen.h
#define bidi_enabled() (OPT(bidi_reorder) && !OPT(force_ltr))

#define BIDI_MAX_PARAGRAPH_ROWS 256

// True for codepoints in blocks that contain right-to-left or Arabic
// letters/digits, i.e. text that may need reordering.
static inline bool
bidi_codepoint_may_reorder(char_type ch) {
    if (ch < 0x590) return false;
    return (ch <= 0x8ff) || (0xfb1d <= ch && ch <= 0xfdff) || (0xfe70 <= ch && ch <= 0xfefc) ||
        (0x10800 <= ch && ch <= 0x10fff) || (0x1e800 <= ch && ch <= 0x1efff);
}

typedef struct BidiCellSource {
    const CPUCell *(*cells_at)(void *ctx, int y);
    uint64_t (*line_id_at)(void *ctx, int y); // 0 = untracked
    void (*mark_dirty)(void *ctx, int y);      // the line must be reshaped this frame
    const TextCache *text_cache;
} BidiCellSource;

typedef struct BidiFrameKey {
    const void *linebuf, *historybuf;
    uint64_t linebuf_generation, historybuf_generation;
    index_type rows, cols, scrolled_by;
    int row_offset;
    bool valid;
} BidiFrameKey;

typedef struct BidiMaps {
    uint16_t *v2l, *l2v; // rows*cols, row-major by render row
    uint8_t *levels;     // rows*cols, per logical cell
    bool *active;        // rows
    uint8_t *row_scratch; // cols
    index_type cols, rows;
    bool changed; // any row map differs from last frame; makes the selection dirty
    struct {
        int first, last;
        bool valid, has_bidi, dirty_any;
    } para; // current-frame paragraph cache, range-y addressed
    int *stamp_first, *stamp_last; // range-y bounds of the paragraph that last wrote the row
    bool *stamp_valid;
    uint64_t *line_id, *prev_line_id; // this frame / last frame, swapped at frame start
    bool *prev_active;                 // snapshot, rows
    uint8_t *prev_levels;              // snapshot, rows*cols
    bool snapshot_taken, moved;
    index_type id_hint;
    BidiFrameKey key;
    unsigned long long resolve_count;
    FriBidiChar *text;
    FriBidiCharType *types;
    FriBidiBracketType *brackets;
    FriBidiLevel *para_levels;
    FriBidiStrIndex *map;
    uint32_t *cell_of_char, *row_start;
    size_t cap; // reusable scratch
} BidiMaps;

void bidi_maps_free(BidiMaps *b);
bool bidi_maps_ensure(BidiMaps *b, index_type rows, index_type cols);
void bidi_set_row_inactive(BidiMaps *b, index_type render_row);
void bidi_set_all_rows_inactive(BidiMaps *b);
// Starts a frame: the stamps of the last frame stay valid only if the key is
// unchanged and the view did not scroll.
void bidi_frame_begin(BidiMaps *b, BidiFrameKey now, bool scroll_changed);
void bidi_cache_invalidate(BidiMaps *b);
// True when every on-screen row of the paragraph on [first, last], from
// first_render_row down, still holds the maps of that same paragraph.
bool bidi_paragraph_cached(const BidiMaps *b, int first, int last, int y_to_render_row, index_type first_render_row);
void bidi_set_line_id(BidiMaps *b, index_type render_row, uint64_t id);
// Resolves the paragraph on rows [first_y, last_y] and writes the maps of those
// of its rows whose render row, y + y_to_render_row, is on screen and not below
// first_render_row (rows already uploaded this frame keep their maps). Returns
// true when at least one of those rows got a map.
bool bidi_resolve_paragraph(BidiMaps *b, BidiCellSource src, void *ctx, int first_y, int last_y, int y_to_render_row,
    index_type first_render_row, bool cacheable);
// Sets the same rows as bidi_resolve_paragraph inactive, for a paragraph with
// nothing to reorder.
void bidi_paragraph_inactive(BidiMaps *b, BidiCellSource src, void *ctx, int first_y, int last_y, int y_to_render_row,
    index_type first_render_row, bool cacheable);
index_type bidi_visual_left_x(const BidiMaps *b, index_type render_row, index_type x, index_type cell_cols);
index_type bidi_logical_x(const BidiMaps *b, index_type render_row, index_type visual_x);
bool bidi_cell_is_rtl(const BidiMaps *b, index_type render_row, index_type x);
void bidi_permute_row_bytes(BidiMaps *b, index_type render_row, uint8_t *row);
