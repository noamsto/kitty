/*
 * bidi.h
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "data-types.h"
#include "line.h"
#include "screen.h"
#include "state.h"
#include "lineops.h"
#include <fribidi.h>

typedef struct BidiScratch {
    FriBidiChar *text;
    FriBidiCharType *types;
    FriBidiBracketType *brackets;
    FriBidiLevel *levels;
    uint32_t *cell_of_char;
    size_t text_cap, types_cap, brackets_cap, levels_cap, cell_of_char_cap;
    uint32_t *row_start;
    size_t row_start_cap;
    uint8_t *row_bytes;
    size_t row_bytes_cap;
    size_t peak; // the most chars a paragraph fed FriBidi since the last trim
} BidiScratch;

void bidi_scratch_free(BidiScratch *s);
// Frees the buffers when far larger than the paragraphs resolved since the last call needed
void bidi_scratch_trim(BidiScratch *s);
// Whether some cell may be right-to-left, or is scaled
bool bidi_cells_may_reorder(const CPUCell *cells, index_type num, const TextCache *tc);
// Whether the FriBidi in use walks back from every bracket pair to the previous strong char, as before 1.0.17
bool bidi_n0_walk_is_slow(void);
// Resolves the paragraph rows[0..num_rows) from base into levels[r * cols + x], after UAX #9 L1 per row. A scaled cell is a
// segment separator at BIDI_LEVEL_FIXED. Returns false, leaving levels unwritten, when every level is 0.
bool bidi_resolve_paragraph(
    BidiScratch *s, const CPUCell *const *rows, unsigned num_rows, index_type cols, const TextCache *tc, FriBidiParType base, uint8_t *levels);
// Writes BIDI_LEVEL_FIXED into levels[r * cols + x] for each scaled cell of rows[0..num_rows), leaving the other levels
void bidi_fix_scaled_cells(const CPUCell *const *rows, unsigned num_rows, index_type cols, uint8_t *levels);
// Puts in v2l[v] the logical cell shown at visual column v of a row with these levels (UAX #9 L2), BIDI_LEVEL_FIXED counting
// as 0. Returns false for the identity.
bool bidi_visual_order(const uint8_t *levels, index_type cols, uint16_t *v2l);
// row has cols cells of stride bytes each, in logical order: puts them in the visual order v2l
void bidi_permute_row_bytes(BidiScratch *s, const uint16_t *v2l, index_type cols, uint8_t *row, size_t stride);

// Per render row of the last upload: the levels its cells were rendered at and its visual order, valid only for that frame
typedef struct BidiMaps {
    uint8_t *levels;  // rows*cols
    uint16_t *v2l;    // rows*cols: the logical cell shown at each visual column, where active
    bool *active;     // per row: not in logical order
    bool *has_levels; // per row: some level is not 0, so the row is shaped at its levels
    bool *relevel;    // per row: the row must be rendered again, at its new levels
    index_type rows, cols;
    const LineBuf *source; // the active linebuf or the paused snapshot
    bool valid;
    bool live;    // the last live upload ran the walk
    bool changed; // some row's levels changed since the selection last used the map
    bool selection_used_maps;
    unsigned resolved; // paragraphs mapped from their text by the walk of the last upload
    // The rows resolved by the last shown walk, numbered as history lines are
    int64_t window_lo, window_hi;
    // Per screen row below the view, which stays dirty until shown: a hash of it as the last shown resolve read it, valid
    // until the row is on view or another buffer is active
    uint64_t *below_hash;
    bool *below_valid;
    const LineBuf *below_source;
    BidiScratch scratch;
    uint8_t *row_levels, *para_levels;
    size_t row_levels_cap, para_levels_cap;
    const CPUCell **para_rows;
    size_t para_rows_cap;
} BidiMaps;

void bidi_maps_free(BidiMaps *b);
void bidi_maps_invalidate_row_hashes(Screen *self);
BidiMaps *bidi_maps_for_upload(Screen *self, const LineBuf *source, index_type rows);
BidiMaps *bidi_prepare_live(Screen *self, bool shown);
void bidi_store_levels(const BidiMaps *b, index_type r, Line *line);
index_type bidi_logical_x(const BidiMaps *b, index_type r, index_type visual_x);
bool bidi_cell_is_rtl(const BidiMaps *b, index_type r, index_type x);

// Whether history line 0, the leading row of pixel scrolling, is in a recorded paragraph within the screen height above it
// that its resolve reads
static inline bool
bidi_history_line0_recorded(const Screen *self) {
    HistoryBuf *hb = self->historybuf;
    for (index_type l = 0; l < hb->count && l <= self->lines; l++) {
        if (historybuf_attrs(hb, l)->bidi_stamp != BIDI_STAMP_NONE) return true;
        if (!historybuf_is_line_continued(hb, l)) return false;
    }
    return false;
}

static inline bool
bidi_live_frame_reorders(const Screen *self) {
    if (self->linebuf->bidi_stamped) return true;
    if (self->scrolled_by) return self->historybuf->bidi_stamped || self->main_linebuf->bidi_stamped;
    return pixel_scroll_enabled(self) && self->historybuf->bidi_stamped && bidi_history_line0_recorded(self);
}

// Sets the levels to shape render row r at and the order to upload it in, NULL for none. Returns whether the row must be
// rendered again at its levels.
static inline bool
bidi_row(const BidiMaps *b, index_type r, const uint8_t **levels, const uint16_t **v2l) {
    const size_t at = (size_t)r * b->cols;
    if (b->has_levels[r]) *levels = b->levels + at;
    if (b->active[r]) *v2l = b->v2l + at;
    return b->relevel[r];
}
