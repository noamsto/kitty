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
// para_rows holds the row count (at most BIDI_MAX_PARAGRAPH_ROWS) and, from this bit, the first row's bidi_rtl and bidi_auto
#define BIDI_KEY_DIR_SHIFT 16
// Steps per character a paragraph's bracket pairing (N0) may walk back to a letter or digit before it is skipped
#define BIDI_N0_SCAN_PER_CHAR 16
// Combined isolate controls a paragraph may feed FriBidi, as before 1.0.17 each FSI can scan to the paragraph end; over it none are fed
#define BIDI_MAX_ISOLATES 32
// Line ids of the paragraph cache: a screen row's id has this bit, a history row's is its absolute line number plus one; 0 is no id
#define BIDI_ID_SCREEN (1ull << 63)

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

// What the screen-row entries of the paragraph cache were written under
typedef struct BidiCacheContext {
    const void *linebuf;   // NULL: no screen-row entries
    uint64_t gen, scrolls; // the linebuf's counters
    uint64_t pushed;       // historybuf->num_pushed
    int view;              // to_render_row: screen row y is shown at render row y + view
} BidiCacheContext;

typedef struct BidiMaps {
    uint16_t *v2l;   // rows*cols, row-major by render row: the logical cell shown at each visual column
    uint8_t *levels; // rows*cols, the embedding level of each logical cell
    bool *active;    // rows: the row has a map
    bool *rtl;       // rows: the paragraph that last wrote or kept the row's entry resolved right-to-left
    // The paragraph cache, per render row: the id of the line shown there when its map was last written or kept, and the ids of
    // the first and last rows, and the row count with the first row's base direction (BIDI_KEY_DIR_SHIFT), of the paragraph that
    // wrote it. Screen ids are valid under the context only.
    uint64_t *row_id, *para_first, *para_last;
    uint32_t *para_rows;
    // rows*cols like levels: per cell a char starts in, the FriBidi type or bracket the last resolve fed for it and whether it
    // was a blank, 0 in the other cells; meaningful only where sig_valid
    uint32_t *sig;
    bool *sig_valid;
    BidiCacheContext ctx;
    // The live ids of the paused snapshot's rows and the live context, recorded when rendering was paused
    uint64_t *paused_ids;
    index_type paused_rows, paused_cap;
    BidiCacheContext paused_ctx;
    // The last resolve may be cached: false when it met a cell spanning several rows or ran out of memory
    bool resolve_cacheable;
    // The last resolve ran FriBidi to success without skipping N0, so the signatures it wrote capture all its inputs
    bool resolve_signed;
    // For tests: resolve the trailing blanks of a paragraph instead of trimming them
    bool no_trim;
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
    unsigned long long resolve_count, render_count, shift_count; // for tests
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
// Whether the base codepoint of some cell passes bidi_codepoint_may_reorder(), or some cell holds an RLI
bool bidi_cells_may_reorder(const CPUCell *cells, index_type num, const TextCache *tc);
// The FriBidi base of a paragraph whose first row has this record: with auto set the first strong character wins and rtl is the fallback
static inline FriBidiParType
bidi_record_base(LineAttrs a) {
    return a.bidi_auto ? (a.bidi_rtl ? FRIBIDI_PAR_WRTL : FRIBIDI_PAR_WLTR) : (a.bidi_rtl ? FRIBIDI_PAR_RTL : FRIBIDI_PAR_LTR);
}
// Resolves the paragraph whose rows have the cells rows[0..num_rows) with the
// base direction base. Every row whose render row, first_render_row + row index, is in
// [min_render_row, b->rows) gets its map written, or
// dropped when the resolve fails or leaves its cells in logical order. Returns
// true if any row got a map.
bool bidi_resolve_paragraph(
    BidiMaps *b, const CPUCell *const *rows, unsigned num_rows, const TextCache *tc, FriBidiParType base, int first_render_row, index_type min_render_row);
void bidi_set_row_inactive(BidiMaps *b, index_type render_row);
// Whether the row's cells would give the signature stored for render_row; overwrites the scratch
bool bidi_row_signature_matches(BidiMaps *b, const CPUCell *cells, const TextCache *tc, index_type render_row);
void bidi_cache_invalidate(BidiMaps *b);
// Drops the entries that mention a screen row, for when the screen rows were moved or the shown buffer changed
void bidi_cache_drop_screen_entries(BidiMaps *b);
// Moves every render row's map and cache entry up by 0 < d < rows rows, leaving the bottom d rows with neither
void bidi_cache_move_rows(BidiMaps *b, index_type d);

// The queries below are the identity, or false, for a row without a map.
// The visual column of the left edge of the logical cell x spanning span cells
index_type bidi_visual_x(const BidiMaps *b, index_type render_row, index_type x, index_type span);
index_type bidi_logical_x(const BidiMaps *b, index_type render_row, index_type visual_x);
// Whether the logical cell x is at an odd (right-to-left) level
bool bidi_cell_is_rtl(const BidiMaps *b, index_type render_row, index_type x);
// The embedding level of the logical cell x
uint8_t bidi_cell_level(const BidiMaps *b, index_type render_row, index_type x);
// row has cols cells of stride bytes each, in logical order: puts them in visual order
void bidi_permute_row_bytes(BidiMaps *b, index_type render_row, uint8_t *row, size_t stride);

// Stores in out, up to cap of them, the codepoints FriBidi classes R or AL that
// bidi_codepoint_may_reorder() rejects, and returns how many there are.
size_t bidi_flag_misses(uint32_t *out, size_t cap);
