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
    const TextCache *text_cache;
} BidiCellSource;

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
// Resolves the paragraph on rows [first_y, last_y] and writes the maps of those
// of its rows whose render row, y + y_to_render_row, is on screen. Returns true
// when at least one on-screen row got a map.
bool bidi_resolve_paragraph(BidiMaps *b, BidiCellSource src, void *ctx, int first_y, int last_y, int y_to_render_row);
index_type bidi_visual_left_x(const BidiMaps *b, index_type render_row, index_type x, index_type cell_cols);
index_type bidi_logical_x(const BidiMaps *b, index_type render_row, index_type visual_x);
bool bidi_cell_is_rtl(const BidiMaps *b, index_type render_row, index_type x);
void bidi_permute_row_bytes(BidiMaps *b, index_type render_row, uint8_t *row);
