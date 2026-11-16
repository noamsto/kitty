/*
 * line-buf.h
 * Copyright (C) 2024 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "line.h"
#include "text-cache.h"

typedef struct {
    PyObject_HEAD

        RenderCell *render_cell_buf;
    CPUCell *cpu_cell_buf;
    index_type xnum, ynum, *line_map, *scratch;
    LineAttrs *line_attrs;
    Line *line;
    TextCache *text_cache;
    bool bidi_stamped; // set by the first bidi record written into this buffer, cleared only by RIS
    // The bidi paragraph cache drops its screen-row entries when bidi_generation changes and follows them when only
    // bidi_scrolls does. Every row-moving primitive and every clear that can leave a row clean bumps bidi_generation. Only
    // a full-height index bumps bidi_scrolls instead, and only the clear of the row such an index just scrolled
    // in bumps nothing. A caller of a full-height index that also edits rows without dirtying them must bump
    // bidi_generation. Index bumps live in linebuf_count_index(), outside linebuf_index() so gcc keeps inlining its guard.
    // Unstamped buffers skip them (RIS bumps bidi_generation and drops the cache), rewrap too (resize invalidates first).
    uint64_t bidi_generation, bidi_scrolls;
} LineBuf;


LineBuf *alloc_linebuf(unsigned int, unsigned int, TextCache *);
