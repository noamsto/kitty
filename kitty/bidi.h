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
typedef enum { BIDI_BASE_NONE, BIDI_BASE_LTR, BIDI_BASE_RTL, BIDI_BASE_AUTO } BidiBase;

// Uses OPT() from state.h, which cannot be included here as it includes screen.h
#define bidi_enabled() (OPT(bidi_reorder) && !OPT(force_ltr))

#define BIDI_MAX_PARAGRAPH_ROWS 256

// Line ids are (kind << 32) | slot; kind 1 and 2 are the main and alt linebuf
#define BIDI_HISTORY_LINE_ID_KIND 3ull

// True for codepoints in blocks that contain right-to-left or Arabic
// letters/digits, i.e. text that may need reordering.
static inline bool
bidi_codepoint_may_reorder(char_type ch) {
    if (ch < 0x590) return false;
    if (ch <= 0x8ff) return true;
    if (ch < 0xfb1d) return false;
    return ch <= 0xfdff || (0xfe70 <= ch && ch <= 0xfefe) || (0x10800 <= ch && ch <= 0x10fff) || (0x1e800 <= ch && ch <= 0x1efff);
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
    uint64_t historybuf_push_generation; // not compared by keys_equal
    index_type rows, cols, scrolled_by;
    index_type historybuf_count, historybuf_start, historybuf_ynum; // not compared by keys_equal
    int row_offset;
    uint8_t base; // the effective BidiBase: it is not derived from the text, so it is part of the key
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
        bool valid, has_bidi;
    } para; // current-frame paragraph cache, range-y addressed
    int *stamp_first, *stamp_last; // range-y bounds of the paragraph that last wrote the row
    bool *stamp_valid;
    bool *rtl; // rows: the paragraph that last settled the row resolved RTL
    // The FriBidi class and bracket type of the character starting in each cell
    // (class 0: none starts there), as fed to the cacheable resolve that wrote
    // the row's current map; sig_valid is cleared by every other map or stamp write
    uint32_t *sig_type, *sig_bracket; // rows*cols, row-major by render row
    bool *sig_valid;                  // rows
    uint64_t *line_id, *prev_line_id; // this frame / last frame, swapped at frame start
    bool *prev_active;                 // snapshot, rows
    uint8_t *prev_levels;              // snapshot, rows*cols
    bool snapshot_taken, moved;
    bool hold; // paused frames since the last live one: the snapshot and line_id are that frame's
    index_type id_hint;
    uint64_t *paused_line_id; // line ids of the paused snapshot rows, not freed with the row maps
    index_type paused_rows, paused_cap;
    struct {
        const void *buf;
        uint64_t generation, push_generation;
        index_type count;
        bool valid;
    } ids_history; // the history when line_id was recorded
    BidiFrameKey key;
    unsigned long long resolve_count;
    bool no_trim; // test only: do not drop the blanks at a paragraph's end
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
// History line ids are ring slots. When the history may have reused a slot
// since line_id was recorded, forgets line_id's history ids, so those lines have
// no last-frame state. If only pushes happened, only the k slots a full ring
// reused, [start_of_data - k, start_of_data), are forgotten; otherwise all are.
void bidi_forget_reused_history_ids(BidiMaps *b, const void *historybuf, uint64_t generation, uint64_t push_generation,
        index_type count, index_type start_of_data, index_type ynum);
// Starts a paused frame, which reads a snapshot: no stamp is trusted (the
// snapshot rows are not range rows), and the line state of the last live frame
// is kept for the level compare of paused frames and of the next live one.
void bidi_frame_begin_paused(BidiMaps *b);
void bidi_cache_invalidate(BidiMaps *b);
// True when every on-screen row of the paragraph on [first, last], from
// first_render_row down, still holds the maps of that same paragraph.
bool bidi_paragraph_cached(const BidiMaps *b, int first, int last, int y_to_render_row, index_type first_render_row);
void bidi_set_line_id(BidiMaps *b, index_type render_row, uint64_t id);
// Returns room for the line ids of a new paused snapshot of rows rows, or
// NULL for none (rows == 0 or no memory). The ids of an earlier snapshot are
// dropped either way; the caller fills every row.
uint64_t *bidi_paused_line_ids(BidiMaps *b, index_type rows);
// Resolves the paragraph on rows [first_y, last_y] and writes the maps of those
// of its rows whose render row, y + y_to_render_row, is on screen and not below
// first_render_row (rows already uploaded this frame keep their maps). Returns
// true when at least one of those rows got a map.
bool bidi_resolve_paragraph(BidiMaps *b, BidiCellSource src, void *ctx, int first_y, int last_y, int y_to_render_row,
    index_type first_render_row, bool cacheable, BidiBase base);
// True when line y, at render_row, still feeds FriBidi the classes and bracket
// types, in the same cells, as when its current map was written by a cacheable
// resolve. Resolves nothing.
bool bidi_row_signature_matches(BidiMaps *b, BidiCellSource src, void *ctx, int y, index_type render_row);
// Sets the same rows as bidi_resolve_paragraph inactive, for a paragraph with
// nothing to reorder.
void bidi_paragraph_inactive(BidiMaps *b, BidiCellSource src, void *ctx, int first_y, int last_y, int y_to_render_row,
    index_type first_render_row, bool cacheable);
index_type bidi_visual_left_x(const BidiMaps *b, index_type render_row, index_type x, index_type cell_cols);
index_type bidi_logical_x(const BidiMaps *b, index_type render_row, index_type visual_x);
bool bidi_cell_is_rtl(const BidiMaps *b, index_type render_row, index_type x);
void bidi_permute_row_bytes(BidiMaps *b, index_type render_row, uint8_t *row);
// Writes the codepoints that FriBidi classes RTL or AL but bidi_codepoint_may_reorder
// does not flag into out, up to cap of them. Returns their total.
size_t bidi_flag_misses(uint32_t *out, size_t cap);
