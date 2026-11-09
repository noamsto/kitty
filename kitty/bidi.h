/*
 * bidi.h
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "data-types.h"
#include "state.h"

typedef enum { BIDI_DIR_GUESS = 0, BIDI_DIR_LTR = 1, BIDI_DIR_RTL = 2 } BidiDirection;

#define bidi_enabled() (OPT(bidi_reorder) && !OPT(force_ltr))

// True for codepoints in blocks that contain right-to-left or Arabic
// letters/digits, i.e. text that may need reordering.
static inline bool
bidi_codepoint_may_reorder(char_type ch) {
    if (ch < 0x590) return false;
    return (ch <= 0x8ff) || (0xfb1d <= ch && ch <= 0xfdff) || (0xfe70 <= ch && ch <= 0xfefc) ||
        (0x10800 <= ch && ch <= 0x10fff) || (0x1e800 <= ch && ch <= 0x1efff);
}
