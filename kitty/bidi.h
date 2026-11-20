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
    size_t peak; // the most chars a paragraph fed FriBidi since the last trim
} BidiScratch;

// The columns [at[2 * i], at[2 * i + 1]) of a row, in order, that are not laid out: those of the horizontally scrolling regions
// covering it. And the num_edges edges of its spans, as screen_bidi_span_edges() puts them.
typedef struct BidiFixedColumns {
    unsigned count, num_edges;
    index_type at[2 * MAX_SMOOTH_SCROLL_REGIONS], edges[2 * MAX_SMOOTH_SCROLL_REGIONS];
} BidiFixedColumns;

// Whether the FriBidi in use walks back from every bracket pair to the previous strong char, as before 1.0.17
bool bidi_n0_walk_is_slow(void);
// Writes the cols cells of dest, stride bytes each, in visual order: cell v is the cell v2l[v] of src, which is in logical order
void bidi_copy_row_bytes(const uint16_t *v2l, index_type cols, const uint8_t *src, uint8_t *dest, size_t stride);

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
    bool edges_changed;          // the edges of a region narrower than the screen moved: the next walk orders every row again
    int walk_top;                // the row shown in render row 0 by the last walk
    const struct BidiRows *walk; // the rows of the walk in progress
    unsigned resolved;           // paragraphs mapped from their text by the walk of the last upload
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
    uint16_t *row_v2l;
    size_t row_v2l_cap;
    const CPUCell **para_rows;
    size_t para_rows_cap;
    BidiFixedColumns *para_fixed;
    size_t para_fixed_cap;
} BidiMaps;

void bidi_maps_free(BidiMaps *b);
void bidi_maps_invalidate_row_hashes(Screen *self);
BidiMaps *bidi_maps_for_upload(Screen *self, const LineBuf *source, index_type rows);
BidiMaps *bidi_prepare_live(Screen *self, bool shown);
void bidi_store_levels(const BidiMaps *b, index_type r, Line *line);
index_type bidi_logical_x(const BidiMaps *b, index_type r, index_type visual_x);
bool bidi_cell_is_rtl(const BidiMaps *b, index_type r, index_type x);
void bidi_maps_edges_changed(BidiMaps *b);
// The visual order of n cells as a span of their own, from the levels stored in their render cells, NULL for the identity
const uint16_t *bidi_span_order(BidiMaps *b, const RenderCell *rc, index_type n);
// The levels to render a line drawn only in the strip of a region at, NULL for none: with fixed, BIDI_LEVEL_FIXED for every cell
// when the line has a record, so that it is shaped as with bidi mode off, else the levels stored in its render cells
const uint8_t *bidi_strip_line_levels(BidiMaps *b, Line *line, bool fixed);
// Stores in rc, the render cells of the columns [left, left + width) of screen row y, the levels its paragraph is laid out with when
// a row of it changed since it was rendered; else rc keeps the levels it was rendered at
void bidi_store_kept_row_levels(Screen *self, index_type y, index_type left, index_type width, RenderCell *rc);
// Stores the levels a line was rendered at in its render cells, 0 for none
void bidi_store_line_levels(Line *line, const uint8_t *levels);
// Puts in edges the sorted columns, other than 0 and the screen width, where the regions narrower than the screen covering screen
// row y start or end, returns how many
unsigned screen_bidi_span_edges(const Screen *self, index_type y, index_type *edges);
// Puts in ans the columns of screen row y covered by horizontally scrolling regions and the edges of its spans
void screen_bidi_fixed_columns(const Screen *self, index_type y, BidiFixedColumns *ans);
// Whether the strip of a shifted region shows the newest scrollback line, and that line has a record
bool screen_bidi_strip_shows_recorded_history(const Screen *self);

// Whether the rows kept by a region may hold a record, which their strip shows at the levels they were rendered at
static inline bool
bidi_kept_rows_stamped(const Screen *self) {
    for (unsigned i = 0; i < self->smooth_scroll.count; i++) {
        const LineBuf *o = self->smooth_scroll.regions[i].overflow;
        if (o && o->bidi_stamped) return true;
    }
    return false;
}

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

bool bidi_live_frame_reorders_unstamped(const Screen *self);

static inline bool
bidi_live_frame_reorders(const Screen *self) {
    if (self->linebuf->bidi_stamped) return true;
    if (!self->smooth_scroll.count && !self->historybuf->bidi_stamped && !self->main_linebuf->bidi_stamped) return false;
    return bidi_live_frame_reorders_unstamped(self);
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
