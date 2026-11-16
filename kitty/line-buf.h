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
    // Bumped by every primitive that moves rows or clears text without marking
    // the row dirty; the bidi paragraph cache drops its screen-row entries when it changes
    uint64_t bidi_generation;
} LineBuf;


LineBuf *alloc_linebuf(unsigned int, unsigned int, TextCache *);
