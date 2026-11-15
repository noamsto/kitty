/*
 * bidi.h
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "data-types.h"
#include "line.h"
#include <fribidi.h>

#define BIDI_MAX_PARAGRAPH_ROWS 256
// The skip_render_row of bidi_resolve_paragraph() that skips no row
#define BIDI_NO_RENDER_ROW ((index_type)UINT_MAX)

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

typedef struct BidiMaps {
    uint16_t *v2l;   // rows*cols, row-major by render row: the logical cell shown at each visual column
    uint8_t *levels; // rows*cols, the embedding level of each logical cell
    bool *active;    // rows: the row has a map
    index_type cols, rows;
    // The LineBuf the maps were made from: the active linebuf, or the paused snapshot
    const void *source;
    // The walker made the maps for the frame it just uploaded
    bool valid;
    // Some row's map was added, dropped or rewritten differently since the selection last consumed it
    bool changed;
    // The force_ltr option last realised on the screen's buffers
    bool force_ltr_seen;
    // The last selection upload was permuted by the maps
    bool selection_used_maps;
    unsigned long long resolve_count, render_count; // for tests
    // reusable scratch
    FriBidiChar *text;
    FriBidiCharType *types;
    FriBidiBracketType *brackets;
    FriBidiLevel *para_levels;
    FriBidiStrIndex *map;
    uint32_t *cell_of_char;
    uint32_t row_start[BIDI_MAX_PARAGRAPH_ROWS + 1];
    size_t cap;
    uint8_t *row_scratch; // a row of bidi_permute_row_bytes(), cols*stride bytes
    size_t row_scratch_cap;
} BidiMaps;

void bidi_maps_free(BidiMaps *b);
// When rows or cols differ from the maps', reallocates them with no row mapped
// and sets changed. Returns false on allocation failure or cols > UINT16_MAX.
bool bidi_maps_ensure(BidiMaps *b, index_type rows, index_type cols);
// Whether the base codepoint of some cell passes bidi_codepoint_may_reorder()
bool bidi_cells_may_reorder(const CPUCell *cells, index_type num, const TextCache *tc);
// Resolves the paragraph whose rows have the cells rows[0..num_rows) with an
// LTR base. Every row whose render row, first_render_row + row index, is in
// [min_render_row, b->rows) and is not skip_render_row gets its map written, or
// dropped when the resolve fails or leaves its cells in logical order. Returns
// true if any row got a map.
bool bidi_resolve_paragraph(
    BidiMaps *b,
    const CPUCell *const *rows,
    unsigned num_rows,
    const TextCache *tc,
    int first_render_row,
    index_type min_render_row,
    index_type skip_render_row);
void bidi_set_row_inactive(BidiMaps *b, index_type render_row);

// The queries below are the identity, or false, for a row without a map.
// The visual column of the left edge of the logical cell x spanning span cells
index_type bidi_visual_x(const BidiMaps *b, index_type render_row, index_type x, index_type span);
index_type bidi_logical_x(const BidiMaps *b, index_type render_row, index_type visual_x);
// Whether the logical cell x is at an odd (right-to-left) level
bool bidi_cell_is_rtl(const BidiMaps *b, index_type render_row, index_type x);
// row has cols cells of stride bytes each, in logical order: puts them in visual order
void bidi_permute_row_bytes(BidiMaps *b, index_type render_row, uint8_t *row, size_t stride);

// Stores in out, up to cap of them, the codepoints FriBidi classes R or AL that
// bidi_codepoint_may_reorder() rejects, and returns how many there are.
size_t bidi_flag_misses(uint32_t *out, size_t cap);
