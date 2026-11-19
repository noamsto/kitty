/*
 * bidi.h
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "data-types.h"
#include "line.h"
#include <fribidi.h>

// Isolate initiators (LRI RLI FSI) of a paragraph fed to FriBidi as isolates, with the PDIs that match them; later ones are fed
// as neutrals. FriBidi's NEWS lists quadratic time in FSIs before 1.0.17 and in back-to-back isolates before 1.0.18.
#define BIDI_MAX_ISOLATES 32

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
} BidiScratch;

void bidi_scratch_free(BidiScratch *s);
// Whether the base codepoint of some cell may be right-to-left, or some cell holds an RLI, RLM or ALM
bool bidi_cells_may_reorder(const CPUCell *cells, index_type num, const TextCache *tc);
// The FriBidi base of a paragraph whose first row has this record: with auto set the first strong character wins and rtl is the fallback
static inline FriBidiParType
bidi_record_base(LineAttrs a) {
    return a.bidi_auto ? (a.bidi_rtl ? FRIBIDI_PAR_WRTL : FRIBIDI_PAR_WLTR) : (a.bidi_rtl ? FRIBIDI_PAR_RTL : FRIBIDI_PAR_LTR);
}
// Resolves the paragraph made of rows[0..num_rows), each of cols cells, from base, into levels[r * cols + x]: the embedding level
// of each cell, after UAX #9 L1 per row. The cells of a char spanning several columns take its level. Returns false, leaving
// levels unwritten, when every level is 0 or the paragraph holds a cell spanning several rows, which is never reordered.
bool bidi_resolve_paragraph(
    BidiScratch *s, const CPUCell *const *rows, unsigned num_rows, index_type cols, const TextCache *tc, FriBidiParType base, uint8_t *levels);
// Puts in v2l[v] the logical cell shown at visual column v of a row with these levels (UAX #9 L2). Returns false for the identity.
bool bidi_visual_order(const uint8_t *levels, index_type cols, uint16_t *v2l);
// row has cols cells of stride bytes each, in logical order: puts them in the visual order v2l
void bidi_permute_row_bytes(BidiScratch *s, const uint16_t *v2l, index_type cols, uint8_t *row, size_t stride);
