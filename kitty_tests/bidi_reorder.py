#!/usr/bin/env python
# License: GPLv3

import os
import random
import shutil
import struct
import subprocess
from collections import namedtuple

from kitty.fast_data_types import (
    BIDI_MAX_ISOLATES,
    BIDI_MAX_PARAGRAPH_ROWS,
    CURSOR_BEAM,
    CURSOR_BLOCK,
    CURSOR_HOLLOW,
    CURSOR_UNDERLINE,
    get_options,
    test_screen_update_cell_data,
)
from kitty.fonts.render import setup_for_testing

from .base import BaseTest, draw_multicell, parse_bytes

A = 'אבג'  # three Hebrew letters, bidi class R
ID10 = tuple(range(10))
LRM, RLM, ALM = '\u200e', '\u200f', '\u061c'
RTL_PLAIN = (9, 8, 7) + ID10[:7]
MIXED = 'abc' + A
MIXED_V2L = (0, 1, 2, 5, 4, 3) + ID10[6:]
# a row whose map is not its own inverse: R R WS EN EN, N1 makes the space R
NSI = 'אב 12'
NSI_LEVELS = (1, 1, 1, 2, 2)
NSI_V2L = (3, 4, 2, 1, 0)
NSI_L2V = (4, 3, 2, 0, 1)
EXTRA_CURSOR = 1 << 2  # an extra cursor of shape 1 in the selection buffer
NONE, IMPLICIT, RTL = 0, 1, 2  # bidi_stamp values, RTL being the upload's memo
CellGPUData = namedtuple('CellGPUData', 'sprite_idx style_and_mark')
GPU_CELL = struct.Struct('=II')  # CellGPUData in kitty/line.h
CELL_GPU_STYLE_SHIFT = 2


def gpu_row(s, buf, y):
    # the buffer has an extra leading render row when pixel scroll is on
    row_size = s.columns * GPU_CELL.size
    start = (y + len(buf) // row_size - s.lines) * row_size
    return [CellGPUData(*c) for c in GPU_CELL.iter_unpack(buf[start : start + row_size])]


def styles(row):
    return [c.style_and_mark >> CELL_GPU_STYLE_SHIFT for c in row]


def draw_coloured(s, text, first_colour=1):
    # each character gets its own 256-colour fg, so its own style index
    for i, ch in enumerate(text):
        parse_bytes(s, f'\x1b[38;5;{first_colour + i}m{ch}'.encode())
    parse_bytes(s, b'\x1b[m')


ISOLATES = '⁦⁧⁨⁩'  # LRI RLI FSI PDI
LRE, RLE, PDF, LRO, RLO = '‪‫‬‭‮'
BIDI_CONTROLS = ISOLATES + LRE + RLE + PDF + LRO + RLO
NO_BRACKETS = str.maketrans('()', '!!')


def osc66_controls(text, control_as=None):
    # each bidi control of text in an OSC 66 cell of its own, the only way a cell can start with one
    return ''.join(f'\x1b]66;w=1;{control_as or ch}\x07' if ch in BIDI_CONTROLS else ch for ch in text).encode()


# Isolate regions
FSI, LRI, RLI, PDI = '⁨', '⁦', '⁧', '⁩'
REGION_GUTTER = ('   12 ' + FSI + 'שלום world' + PDI).encode()
REGION_GUTTER_V2L = tuple(range(6)) + (11, 12, 13, 14, 15, 10, 9, 8, 7, 6) + tuple(range(16, 20))
REGION_GUTTER_LEVELS = (0,) * 6 + (1,) * 5 + (2,) * 5 + (0,) * 4
CLAUDE_NO_PDI = ('❯ ' + FSI + 'הסדר לא טוב' + '\x1b[C' + '\x1b[1;20H│' + '\x1b[1;15H').encode()
CLAUDE_FIELD_END = ('❯ ' + FSI + 'הסדר לא טוב' + '\x1b[C' + '\x1b[1;20H' + PDI + '│' + '\x1b[1;15H').encode()


def ID(a, b):
    return tuple(range(a, b))


class TestBidiReorder(BaseTest):
    def setUp(self):
        self.enterContext(setup_for_testing())

    def screen(self, cols=10, lines=5, scrollback=5, **opts):
        s = self.create_screen(cols=cols, lines=lines, scrollback=scrollback, options=opts)
        parse_bytes(s, b'\x1b[8h')
        return s

    # Hooks take render rows: with pixel scroll on, screen row y is render row y + 1
    def v2l(self, s, y):
        v2l = s.test_bidi_v2l(y + 1)
        if v2l is not None:
            self.ae(sorted(v2l), list(range(s.columns)), 'v2l must be a bijection over the columns')
        return v2l

    def levels(self, s, y):
        levels = s.test_bidi_levels(y + 1)
        return None if levels is None else tuple(levels)

    def memo(self, s, y):
        return s.test_bidi_line_record(y)[0]

    def refresh(self, s, y):
        test_screen_update_cell_data(s)
        return self.v2l(s, y)

    def frame(self, s):
        # (resolves, renders) of one frame
        before = s.test_bidi_counts()
        test_screen_update_cell_data(s)
        return tuple(a - b for a, b in zip(s.test_bidi_counts(), before))

    def uploaded(self, s, y):
        # CellGPUData of screen row y after rendering a frame
        return gpu_row(s, test_screen_update_cell_data(s), y)

    def sprites(self, row):
        return [c.sprite_idx for c in row]

    def fresh_sprites(self, text, cols=5, **opts):
        # sprites of text drawn alone on a new screen with the same options
        f = self.screen(cols=cols, lines=2, **opts)
        f.draw(text)
        ans = self.sprites(self.uploaded(f, 0))
        self.assertTrue(any(ans))
        return ans

    def guessed_sprites(self, text, cols=5):
        # sprites of text drawn alone on a screen that never sent CSI 8 h, so shaped without the walker
        f = self.create_screen(cols=cols, lines=2, options={})
        f.draw(text)
        return self.sprites(self.uploaded(f, 0))

    def assert_shaped(self, s, y, fresh, msg=''):
        # the glyphs of row y in a new frame are those of its text shaped alone
        self.ae(self.sprites(self.uploaded(s, y)), fresh, msg)

    def paragraph(self, *rows, lines=5, **opts):
        # a 5 column screen with each row drawn in turn, so soft-wrapped into one paragraph
        s = self.screen(cols=5, lines=lines, **opts)
        for row in rows:
            s.draw(row)
        return s

    def split(self, s, row=1, ch='ו'):
        # overwrite the last cell of the row above linebuf row `row`, which clears its soft wrap
        s.cursor_position(row, 5)
        s.draw(ch)
        self.assertFalse(s.linebuf.is_continued(row))

    def mixed(self, **opts):
        s = self.screen(**opts)
        s.draw(MIXED)
        return s

    def nsi(self):
        s = self.screen(cols=5)
        s.draw(NSI)
        self.ae(self.refresh(s, 0), NSI_V2L)
        return s

    # The cache tests below address the hooks by screen row; the render row offset follows the live options and screen
    def rr(self, s, y):
        return y + int(bool(get_options().pixel_scroll) and not s.is_using_alternate_linebuf())

    def vmaps(self, s):
        return tuple(s.test_bidi_v2l(self.rr(s, y)) for y in range(s.lines))

    def lv(self, s, y):
        levels = s.test_bidi_levels(self.rr(s, y))
        return None if levels is None else tuple(levels)

    def state(self, s):
        return self.vmaps(s), tuple(self.lv(s, y) for y in range(s.lines))

    def fill(self, s, rows):
        for y, text in enumerate(rows):
            s.cursor_position(y + 1, 1)
            s.draw(text)
        s.cursor_position(1, 1)

    def filled(self, rows, **opts):
        s = self.screen(**opts)
        self.fill(s, rows)
        return s

    def put(self, s, y, x, text):
        s.cursor_position(y, x)
        s.draw(text)

    def warm(self, s):
        # history lines stay dirty until first shown, and a first frame writes the cache
        for _ in range(2):
            self.frame(s)

    def prerender(self, s):
        for up in (True, False):
            s.scroll(s.historybuf.count, up)
            self.frame(s)

    def history(self, rows, up=0):
        # 5x3 screen, one row per line, every history line rendered once
        s = self.screen(cols=5, lines=3, scrollback=10)
        for r in rows:
            parse_bytes(s, r.encode() + b'\r\n')
        self.prerender(s)
        if up:
            s.scroll(up, True)
        return s

    def plain_maps(self, rows, cols=10, lines=0):
        # the maps of a screen filled with rows, with nothing pushed into the history
        f = self.screen(cols=cols, lines=lines or len(rows))
        self.fill(f, rows)
        self.ae(f.historybuf.count, 0)
        self.frame(f)
        return self.vmaps(f)

    def trigger_case(self, build, trigger, expect=None, fresh=True, resolves=None):
        # maps after the trigger frame, equal to a fresh screen's (fresh=False: a first frame changes the view) and to expect when given
        s = build()
        self.warm(s)
        trigger(s)
        resolved = self.frame(s)[0]
        if resolves is None:
            self.assertGreater(resolved, 0, 'the trigger must make the paragraph resolve again')
        else:
            self.ae(resolved, resolves)
        got = self.vmaps(s)
        if fresh:
            f = build()
            trigger(f)
            self.frame(f)
            self.ae(got, self.vmaps(f))
        if expect is not None:
            self.ae(got, expect)
        return got

    def beta_over_a(self, s, y, scrolled_by=0, renders=2):
        # ב over row 2 turns the ! ending row 1 from N2 level 0 into N1 level 1: one re-resolve redraws row 1 (levels) and row 2 (written)
        s.cursor_position(3, 1)
        s.draw('ב')
        self.ae((s.scrolled_by, s.linebuf.dirty_lines()), (scrolled_by, [2]))
        self.ae(self.frame(s), (1, renders))
        self.ae(self.lv(s, y), (2, 2, 1, 2, 1))

    def selection_rows(self, s, raw):
        # the bytes of each screen row of a selection buffer with one byte per cell
        offset = len(raw) // s.columns - s.lines  # the extra leading render row of pixel scroll
        return [list(raw[s.columns * (y + offset) : s.columns * (y + offset + 1)]) for y in range(s.lines)]

    def lit(self, s):
        # the columns lit on row 0 by a new selection upload; no other row may be lit
        rows = self.selection_rows(s, s.current_selections())
        self.ae(rows[1:], [[0] * s.columns] * (s.lines - 1))
        return [x for x, b in enumerate(rows[0]) if b]

    def selected_columns(self, s, start, end):
        # the visual columns lit on row 0 after a frame when logical cells start..end are selected
        s.clear_selection()
        s.start_selection(start, 0)
        s.update_selection(end, 0)
        test_screen_update_cell_data(s)
        return self.lit(s)

    def queries(self, s):
        # the selection of logical cells 3..4, the cursor's column and the logical column of visual column 3, on row 0
        return self.selected_columns(s, 3, 4), s.test_cursor_render_x(), s.test_bidi_logical_x(3, 0)

    def test_levels_and_maps(self):
        for text, levels, v2l in (
            ('abc' + A, (0, 0, 0, 1, 1, 1, 0, 0, 0, 0), (0, 1, 2, 5, 4, 3, 6, 7, 8, 9)),
            ('אב, גד', (1, 1, 1, 1, 1, 1, 0, 0, 0, 0), (5, 4, 3, 2, 1, 0, 6, 7, 8, 9)),
            ('א好ב', (1, 0, 0, 1, 0, 0, 0, 0, 0, 0), ID10),
            ('אב好גד', (1, 1, 0, 0, 1, 1, 0, 0, 0, 0), (1, 0, 2, 3, 5, 4, 6, 7, 8, 9)),
            ('א😀ב', (1, 1, 1, 1, 0, 0, 0, 0, 0, 0), (3, 2, 1, 0, 4, 5, 6, 7, 8, 9)),
        ):
            with self.subTest(text=text):
                s = self.screen()
                s.draw(text)
                self.ae((self.refresh(s, 0), self.levels(s, 0)), (v2l, levels))
        # logical cell 1 (span 1) is at visual column 2; the emoji's span of 2 starts at its second cell, column 1
        s = self.screen()
        s.draw('א😀ב')
        self.refresh(s, 0)
        self.ae((s.test_bidi_visual_x(1, 0), s.test_bidi_visual_x(1, 0, 2)), (2, 1))

    def test_unflagged_continuation_and_history(self):
        # the continuation row (levels 2,2,1,2,2) has no RTL character of its own
        s = self.screen(cols=5, lines=5)
        s.draw('אבגדה12 34')
        v2l = self.refresh(s, 0)
        self.ae((bool(str(s.line(1))), self.memo(s, 1), self.memo(s, 0)), (True, IMPLICIT, RTL))
        self.ae([v2l, self.levels(s, 1), self.v2l(s, 1)], [(4, 3, 2, 1, 0), (2, 2, 1, 2, 2), (3, 4, 2, 0, 1)])
        for lines, texts, rows in (
            (2, ('אבגדה', 'ובזחט', 'יכלמנ', '12 34'), ((0, (4, 3, 2, 1, 0)), (1, (3, 4, 2, 0, 1)))),
            (1, ('אבגדה', 'ובזחט', '12 34'), ((0, (3, 4, 2, 0, 1)),)),
        ):
            with self.subTest(lines=lines):
                s = self.screen(cols=5, lines=lines, scrollback=10)
                s.draw(''.join(texts))
                self.ae(s.historybuf.count, 2)
                self.ae([self.refresh(s, y) for y, _ in rows], [v2l for _, v2l in rows])

    def test_no_map_without_reordering(self):
        s = self.screen()
        s.draw('hello world')
        self.ae((self.refresh(s, 0), self.memo(s, 0)), (None, IMPLICIT))
        s = self.create_screen(cols=10, options={})  # no CSI 8 h, so no walker runs and the screen has no maps to count with
        s.draw('abc' + A)
        self.ae((self.refresh(s, 0), self.memo(s, 0), s.test_bidi_counts()), (None, NONE, (0, 0)))
        s = self.screen(force_ltr=True)  # force_ltr wins over CSI 8 h
        s.draw('abc' + A)
        self.assertIsNone(self.refresh(s, 0))

    def test_flag_range_boundaries(self):
        # each side of the classifier's boundaries (zero-width ones cannot be drawn alone: the nearest drawable stands in)
        flagged = (0x590, 0x8C9, 0xFB1D, 0xFDFF, 0xFE70, 0xFEFE, 0x10800, 0x10FFF, 0x1E800, 0x1EFFF)
        unflagged = (0x58F, 0x904, 0xFB1C, 0xFE10, 0xFE6F, 0xFF01, 0x107FF, 0x11003, 0x1E7FF, 0x1F000)
        for cp, want in [(cp, RTL) for cp in flagged] + [(cp, IMPLICIT) for cp in unflagged]:
            with self.subTest(hex(cp)):
                s = self.screen()
                s.draw(chr(cp))
                self.assertGreater(s.cursor.x, 0, 'codepoint did not draw')
                test_screen_update_cell_data(s)
                self.ae(self.memo(s, 0), want)
        # RLM is zero width, so it never needs the classifier
        self.ae(self.screen().test_bidi_flag_misses(), [0x200F])

    def test_flag_named_scripts(self):
        # no assigned R/AL letter lies outside the classifier; U+FEFD and U+FEFE are unassigned, AL by default
        for cp in (0x0710, 0x0784, 0x07CA, 0x0800, 0x0840, 0x08A0, 0x0870, 0xFB2A, 0xFB50, 0xFE8D, 0x10A10, 0x1E900, 0x1EE00, 0xFEFD, 0xFEFE):
            with self.subTest(hex(cp)):
                s = self.screen()
                s.draw('ab ' + chr(cp) + chr(cp))
                v2l = self.refresh(s, 0)
                self.assertTrue(v2l and v2l != ID10, f'U+{cp:04X} was not flagged and reordered')

    def test_bypass(self):
        # a row with an image placeholder or scaled text is not reordered
        for what, extra, mapped in (
            ('plain', lambda s: None, True),
            ('image placeholder', lambda s: s.draw(chr(0x10EEEE)), False),
            ('scaled text', lambda s: draw_multicell(s, 'x', scale=2), False),
        ):
            with self.subTest(what):
                s = self.screen()
                s.draw(A)
                extra(s)
                self.ae(self.refresh(s, 0) is not None, mapped)

    def test_upload_permutes_cells(self):
        # each visual cell is the logical cell v2l[visual], told apart by style, on a map that is not its own inverse
        s = self.screen(cols=5)
        draw_coloured(s, NSI)
        logical = [s.cell_style_idx(0, x) for x in range(5)]
        self.ae(len(set(logical)), 5)
        self.ae(styles(self.uploaded(s, 0)), [logical[NSI_V2L[v]] for v in range(5)])
        s = self.screen(force_ltr=True)
        draw_coloured(s, 'abc' + A)
        logical = [s.cell_style_idx(0, x) for x in range(6)]
        self.ae(styles(self.uploaded(s, 0))[:6], logical)

    def test_widen_merges_flag_and_map(self):
        s = self.screen(cols=5, lines=5)
        s.draw('abcde' + A)
        self.ae((self.refresh(s, 1), [self.memo(s, 0), self.memo(s, 1)]), ((2, 1, 0, 3, 4), [IMPLICIT, RTL]))
        s.resize(5, 25)
        self.ae((self.refresh(s, 0), self.memo(s, 0)), (tuple(range(5)) + (7, 6, 5) + tuple(range(8, 25)), RTL))

    def test_latin_paragraph_dirties_only_written_row(self):
        # nothing to reorder: a write to one row of a soft-wrapped paragraph re-shapes that row only
        s = self.screen(cols=10, lines=8)
        s.draw('x' * 75)
        self.frame(s)
        self.ae(self.frame(s), (0, 0))
        s.draw('x')
        self.ae(self.frame(s), (0, 1))

    def test_level_change_redraws_paragraph(self):
        # losing the last RTL char: row 1, shaped at 2,2,1,2,2, re-shapes at level 0 though only row 0 was written
        s = self.paragraph('אבגד.', '12 34')
        self.frame(s)
        self.ae(self.levels(s, 1), (2, 2, 1, 2, 2))
        s.cursor_position(1, 1)
        s.draw('abcd')  # row 0 keeps its last cell, as writing it would split the paragraph
        self.ae((s.linebuf.is_continued(1), s.linebuf.dirty_lines()), (True, [0]))
        self.ae(self.frame(s)[1], 2)
        self.assertIsNone(self.v2l(s, 1))
        self.assert_shaped(s, 1, self.fresh_sprites('12 34'))

    def test_split_paragraph_redraws_old_continuation(self):
        # once split, row 1 (N0 made the brackets of (12) R) has no RTL and no RTL memo: re-shape it plain
        s = self.paragraph('אבגדה', '(12)')
        self.frame(s)
        self.ae((self.levels(s, 1), self.memo(s, 1)), ((1, 2, 2, 1, 0), IMPLICIT))
        self.split(s, ch='x')
        self.ae(s.linebuf.dirty_lines(), [0])
        self.ae(self.frame(s)[1], 2)
        self.assertIsNone(self.v2l(s, 1))
        self.assert_shaped(s, 1, self.fresh_sprites('(12)'))

    def test_tall_paragraph_keeps_uploaded_maps(self):
        # 400 rows on 300 lines (100 in history): ! between R letters maps (1,0) except at the window end where it is (0,1)
        s = self.screen(cols=2, lines=300, scrollback=200)
        s.draw('א!' * 400)
        self.ae(s.historybuf.count, 100)
        test_screen_update_cell_data(s)
        maps = tuple(s.test_bidi_v2l(r) for r in range(s.lines + 1))
        self.ae(set(maps), {(0, 1), (1, 0)})
        self.ae([r for r, m in enumerate(maps) if m == (0, 1)], [156, 284, 300])

    def test_osc66_text_reordered(self):
        s = self.screen()
        parse_bytes(s, 'ab\x1b]66;w=1;א\x07\x1b]66;w=1;ב\x07c'.encode())
        self.ae((self.refresh(s, 0), self.memo(s, 0)), ((0, 1, 3, 2) + ID10[4:], RTL))
        # a right-to-left character keeps its cells together, shown last to first
        s = self.screen()
        draw_multicell(s, 'א', width=2)
        draw_multicell(s, 'ב', width=2)
        self.ae((self.refresh(s, 0), self.levels(s, 0)), ((3, 2, 1, 0) + ID10[4:], (1, 1, 1, 1) + (0,) * 6))

    def maps(self, data, cols, lines):
        # (v2l, levels) of every screen row after one frame of data
        s = self.screen(cols=cols, lines=lines)
        parse_bytes(s, data)
        test_screen_update_cell_data(s)
        return [(self.v2l(s, y), self.levels(s, y)) for y in range(lines)]

    def assert_controls_resolve_as_neutrals(self, text, cols, lines):
        # a combined isolate control reaches FriBidi and an embedding or override does not; one in an OSC 66 cell resolves as ! does
        neutral = self.maps(osc66_controls(text, '!'), cols, lines)
        self.assertIsNotNone(neutral[0][0])
        self.ae(self.maps(osc66_controls(text), cols, lines), neutral)

    def test_osc66_isolates_resolve_as_neutrals(self):
        self.assert_controls_resolve_as_neutrals('a⁧bא⁩⁦ב⁩⁨ג⁩', cols=12, lines=2)

    def test_osc66_isolate_flood_resolves_as_neutrals(self):
        # thousands of sibling isolate pairs in one soft-wrapped paragraph
        self.assert_controls_resolve_as_neutrals('א' + '⁧x⁩' * 3000, cols=200, lines=50)

    def test_osc66_embeddings_resolve_as_neutrals(self):
        self.assert_controls_resolve_as_neutrals('aא' + RLE + 'bc', cols=12, lines=2)
        self.assert_controls_resolve_as_neutrals('a' + RLO + 'bc' + PDF + 'd' + LRO + 'אב', cols=12, lines=2)
        # nested to FriBidi's deepest level
        self.assert_controls_resolve_as_neutrals((RLE + LRE) * 62 + 'אa', cols=200, lines=50)

    def assert_rows(self, text, cols, rows, pre=b''):
        # rows: (v2l prefix, levels prefix) of each screen row after one frame of text
        got = self.maps(pre + text.encode(), cols, len(rows))
        for y, (v2l, levels) in enumerate(rows):
            self.assertIsNotNone(got[y][0], f'row {y} has no map')
            self.ae((got[y][0][: len(v2l)], got[y][1][: len(levels)]), (v2l, levels), f'row {y}')

    def test_combined_isolates_reach_fribidi(self):
        # each control is written right after a drawn char, so it is stored in that char's cell and takes no column
        AUTO = b'\x1b[?2501h'
        cases = (
            # RLI makes the digits level 2 and the space between them R: a 34 12
            ('RLI numbers', 'a⁧12 34⁩', 10, b'', [((0, 4, 5, 3, 1, 2), (0, 2, 2, 1, 2, 2))]),
            # the ! takes the isolate's direction (N2, eos R), so it is shown left of bc
            ('RLI neutral after letters', 'a⁧bc!⁩', 10, b'', [((0, 3, 1, 2), (0, 2, 2, 1))]),
            # without the FSI the ! would take the paragraph's L
            ('FSI over hebrew', 'a⁨א!', 10, b'', [((0, 2, 1), (0, 1, 1))]),
            # the PDI ends the isolate: c is at the paragraph level
            ('PDI ends the isolate', 'a⁧!b⁩c', 10, b'', [((0, 2, 1, 3), (0, 1, 2, 0))]),
            # LRI in an RTL paragraph makes the ! L
            ('LRI in rtl paragraph', 'א⁦!a⁩ג', 10, AUTO, [((9, 8, 7, 6, 5, 4, 3, 1, 2, 0), (1, 2, 2, 1, 1, 1, 1, 1, 1, 1))]),
            # an unmatched FSI isolates to the paragraph end, across the soft wrap
            ('wide cell holds the PDI', 'a⁧b😀⁩c', 10, b'', [((0, 3, 2, 1, 4), (0, 2, 1, 1, 0))]),
            ('wide L cell in an RLI', 'a⁧12 中⁩x', 10, b'', [((0, 4, 5, 3, 1, 2, 6), (0, 2, 2, 1, 2, 2, 0))]),
            ('FSI to the paragraph end', 'a⁨אבגדהו!', 5, b'', [((0, 4, 3, 2, 1), (0, 1, 1, 1, 1)), ((2, 1, 0, 3, 4), (1, 1, 1, 0, 0))]),
        )
        for name, text, cols, pre, rows in cases:
            with self.subTest(name):
                self.assert_rows(text, cols, rows, pre)

    def test_combined_isolates_keep_cells(self):
        # the control is stored in the previous cell and takes no column
        for text, cells in (
            ('a⁧bc⁩', ['a⁧', 'b', 'c⁩']),
            ('a⁨אב', ['a⁨', 'א', 'ב']),
            ('a⁦b⁩c', ['a⁦', 'b⁩', 'c']),
            ('a' + RLE + 'bc' + PDF, ['a' + RLE, 'b', 'c' + PDF]),
        ):
            with self.subTest(text=text):
                s = self.screen()
                s.draw(text)
                self.ae(([s.line(0)[i] for i in range(len(cells) + 1)], s.cursor.x), (cells + ['\x00'], len(cells)))

    def test_embeddings_stay_inert(self):
        # combined embeddings and overrides are never fed: maps are those of the text without them
        for control in (LRE, RLE, LRO, RLO):
            with self.subTest(control=control):
                plain = self.maps('אab!12'.encode(), 10, 1)
                self.assertIsNotNone(plain[0][0])
                self.ae(self.maps(('אa' + control + 'b!12' + PDF).encode(), 10, 1), plain)
        plain = self.maps('xabcdאב'.encode(), 10, 1)
        self.ae(self.maps(('x' + RLO + 'abc' + PDF + 'd' + LRE + 'אב' + PDF).encode(), 10, 1), plain)

    def test_isolate_flood(self):
        # every cell of a soft-wrapped paragraph holds a letter and 23 isolate controls: far over any cap, so it lays out as without them
        cell = (ISOLATES * 6)[:23]
        n = 9000
        s = self.screen(cols=200, lines=50)
        parse_bytes(s, ('a' + cell).encode())
        self.ae(len(s.line(0)[0]), 24)
        plain = self.maps(('א' + 'a' * (n - 1)).encode(), 200, 50)
        self.assertIsNotNone(plain[0][0])
        self.ae(self.maps(('א' + cell + ('a' + cell) * (n - 1)).encode(), 200, 50), plain)

    def capped(self):
        # BIDI_MAX_ISOLATES controls, the last in the last cell: RLI 12 34 PDI, then empty isolates (and a lone PDI) in letter
        # cells, which leave every level as it is
        fill = BIDI_MAX_ISOLATES - 2
        return 'א a⁧12 34⁩ ' + 'b⁦⁩' * (fill // 2) + 'b⁩' * (fill % 2), 9 + (fill + 1) // 2

    # a 34 12 after the Hebrew letter, and the same text with no control fed
    CAPPED = (0, 1, 2, 6, 7, 5, 3, 4, 8), (1, 0, 0, 2, 2, 1, 2, 2, 0)
    UNCAPPED = (0, 1, 2, 3, 4, 5, 6, 7, 8), (1, 0, 0, 0, 0, 0, 0, 0, 0)

    def test_isolate_cap(self):
        text, _ = self.capped()
        got = self.maps(text.encode(), 40, 1)[0]
        self.ae((got[0][:9], got[1][:9]), self.CAPPED)
        text += '⁩'
        got = self.maps(text.encode(), 40, 1)[0]
        self.ae((got[0][:9], got[1][:9]), self.UNCAPPED)
        self.ae(got, self.maps(''.join(ch for ch in text if ch not in ISOLATES).encode(), 40, 1)[0])

    def test_isolate_signature_over_cap(self):
        # one control over the cap, alone on the second row: removing it leaves that row with none, yet the paragraph is resolved
        # again (over the cap it was not signed) and its controls are now fed
        text, cells = self.capped()
        s = self.screen(cols=40, lines=3)
        s.draw(text + 'b' * (40 - cells) + 'c⁩d')
        self.warm(s)
        self.ae((self.vmaps(s)[0][:9], self.lv(s, 0)[:9]), self.UNCAPPED)
        self.put(s, 2, 1, 'c')
        self.ae(self.frame(s)[0], 1)
        self.ae((self.vmaps(s)[0][:9], self.lv(s, 0)[:9]), self.CAPPED)

    def test_isolate_signature_shortcut(self):
        # a row that gains or loses an isolate control in a warm paragraph is resolved again, whatever its char classes
        s = self.screen(lines=3)
        s.draw('aא!')
        self.warm(s)
        self.ae((self.vmaps(s)[0][:3], self.lv(s, 0)[:3]), ((0, 1, 2), (0, 1, 0)))
        self.put(s, 1, 1, 'a⁨')
        self.ae(self.frame(s)[0], 1)
        self.ae((self.vmaps(s)[0][:3], self.lv(s, 0)[:3]), ((0, 2, 1), (0, 1, 1)))
        self.put(s, 1, 1, 'a')
        self.ae(self.frame(s)[0], 1)
        self.ae((self.vmaps(s)[0][:3], self.lv(s, 0)[:3]), ((0, 1, 2), (0, 1, 0)))

    def test_ascii_prompt_fsi_fallback(self):
        # P2 and P3 skip the isolate: with no strong char before the FSI the paragraph takes the fallback direction, the isolate is RTL
        s = self.dscreen(self.AUTO)
        s.draw('> שלום')
        self.warm(s)
        self.assertTrue(self.prtl(s, 0))
        s = self.dscreen(self.AUTO)
        s.draw('> ⁨שלום')
        self.warm(s)
        self.assertFalse(self.prtl(s, 0))
        self.ae((self.vmaps(s)[0][:6], self.lv(s, 0)[:6]), ((0, 1, 5, 4, 3, 2), (0, 0, 1, 1, 1, 1)))

    def bracket_maps(self, text, cols, lines):
        # the maps of text, and of text with its brackets as !, which N0 cannot pair
        return self.maps(text.encode(), cols, lines), self.maps(text.translate(NO_BRACKETS).encode(), cols, lines)

    def test_n0_scan_budget_boundary(self):
        # The scan adds, per opening bracket, its distance back to the last letter or digit: 1 for the ( after א, then
        # 2k + 2 for pair k = 0, 1, ... after ב. Against 16 per char, blanks trimmed: 32 pairs give 1057 <= 16 * 68 = 1088,
        # 33 pairs 1123 > 16 * 70 = 1120.
        kept, plain = self.bracket_maps('א(ב)' + '()' * 32, cols=80, lines=1)
        # N0 makes (ב) R after א; as ! the ) would take the paragraph's L from the neutrals up to eos
        self.ae((kept[0][1][:5], plain[0][1][:5]), ((1, 1, 1, 1, 0), (1, 1, 1, 0, 0)))
        skipped, plain = self.bracket_maps('א(ב)' + '()' * 33, cols=80, lines=1)
        self.ae(skipped, plain)

    def test_bracket_flood_resolves_without_n0(self):
        # 3000 pairs soft-wrapped over 31 rows, far over the scan budget
        flood, plain = self.bracket_maps('א(ב)' + '()' * 3000, cols=200, lines=50)
        self.assertIsNotNone(flood[0][0])
        self.ae(flood, plain)

    def test_history_row_reshaped_when_sibling_changes_its_levels(self):
        # ( ends a history row: R before and EN after make it R, mirrored; a after makes it L
        s = self.screen(cols=5, lines=3, scrollback=10)
        parse_bytes(s, b'top\r\n')
        s.draw('אבגד(12 34')
        parse_bytes(s, b'\r\nab\r\ncd')
        self.ae((s.historybuf.count, str(s.historybuf.line(0)), str(s.line(0))), (2, 'אבגד(', '12 34'))
        s.scroll(1, True)
        test_screen_update_cell_data(s)
        self.ae(self.levels(s, 0), (1,) * 5)
        s.cursor_position(1, 1)
        s.draw('a')
        self.ae((s.scrolled_by, s.historybuf.dirty_lines()), (1, []))
        self.ae(self.frame(s)[1], 2)  # the history row's render cache slot is still its own
        self.ae(self.levels(s, 0), (1, 1, 1, 1, 0))
        self.assert_shaped(s, 0, self.fresh_sprites('אבגד('))

    def test_live_row_below_view_reshaped_when_scrolled_into_view(self):
        s = self.screen(cols=5, lines=3, scrollback=10)
        parse_bytes(s, b'h1\r\nh2\r\nh3\r\n')
        s.draw('אבגד (12)')
        self.ae((str(s.line(1)), str(s.line(2))), ('אבגד ', '(12)'))
        test_screen_update_cell_data(s)
        self.ae(self.levels(s, 2), (1, 2, 2, 1, 0))
        s.scroll(1, True)
        test_screen_update_cell_data(s)
        s.cursor_position(2, 1)
        s.draw('abcd')  # the paragraph is now left-to-right, with row 2 below the view
        test_screen_update_cell_data(s)
        self.ae(s.linebuf.dirty_lines(), [])
        s.scroll(1, False)
        test_screen_update_cell_data(s)
        self.assertIsNone(self.levels(s, 2))
        self.assert_shaped(s, 2, self.fresh_sprites('(12)'))

    def test_history_row_above_view_reshaped_when_scrolled_into_view(self):
        # without pixel scroll a scroll does not re-shape every history row, so only the level change can
        opts = {'pixel_scroll': False}
        s = self.screen(cols=5, lines=3, scrollback=10, **opts)
        s.draw('אבגד(12 34')
        parse_bytes(s, b'\r\nab\r\ncd')
        self.ae((s.historybuf.count, str(s.historybuf.line(0))), (1, 'אבגד('))
        s.scroll(1, True)
        test_screen_update_cell_data(s)
        self.ae(tuple(s.test_bidi_levels(0)), (1,) * 5)
        s.scroll(1, False)
        test_screen_update_cell_data(s)
        s.cursor_position(1, 1)
        s.draw('a')
        test_screen_update_cell_data(s)
        self.ae(s.historybuf.dirty_lines(), [])
        s.scroll(1, True)
        self.ae(self.frame(s)[1], 1)  # the history row's render cache slot is still its own
        self.assert_shaped(s, 0, self.fresh_sprites('אבגד(', **opts))

    def test_straddling_paragraph_idle_frames_render_nothing(self):
        # a paragraph with rows above the view, and when scrolled back also below it
        s = self.screen(cols=5, lines=3, scrollback=10)
        s.draw('אבגד.12 34abcdeאבגד.12 34')
        self.ae(s.historybuf.count, 2)
        for up in (0, 1):
            with self.subTest(up=up):
                if up:
                    s.scroll(up, True)
                test_screen_update_cell_data(s)
                self.ae(tuple(s.test_bidi_levels(up)), (2, 2, 1, 2, 2))  # history line 0
                self.ae(self.frame(s), (0, 0))

    def test_unstamped_row_reordered_only_with_its_paragraph(self):
        s = self.create_screen(cols=5, lines=5, options={})
        s.draw('אבגדה')  # written before CSI 8 h
        parse_bytes(s, b'\x1b[8h\x1b[3Habc')
        self.ae([self.memo(s, y) for y in range(3)], [NONE, NONE, IMPLICIT])
        self.assertIsNone(self.refresh(s, 0))
        s = self.create_screen(cols=5, lines=5, options={})
        s.draw('אבגדה')
        parse_bytes(s, b'\x1b[8h')
        s.draw('וזח')
        self.ae((self.refresh(s, 0), self.v2l(s, 1)), ((4, 3, 2, 1, 0), (2, 1, 0, 3, 4)))
        self.ae([self.memo(s, y) for y in range(2)], [NONE, RTL])

    def test_style_table_gc_keeps_permutation(self):
        # the walker uploads every row, so a full re-upload after a style remap is still in visual order
        s = self.screen(cols=5)
        parse_bytes(s, b'\x1b[3H')
        draw_coloured(s, 'abcde', first_colour=100)
        s.erase_in_line(2)
        s.cursor_position(1, 1)
        draw_coloured(s, NSI)
        test_screen_update_cell_data(s)
        before = [s.cell_style_idx(0, x) for x in range(5)]
        s.test_gc_style_table()
        logical = [s.cell_style_idx(0, x) for x in range(5)]
        self.assertNotEqual(before, logical)
        self.ae(styles(self.uploaded(s, 0)), [logical[NSI_V2L[v]] for v in range(5)])

    def test_split_by_erase_and_insert_reshapes_row_below(self):
        def erase(s):
            s.cursor_position(1, 5)
            s.erase_in_display(1)
            return 1

        def insert(s):
            s.cursor_position(2, 1)
            s.insert_lines(1)
            return 2

        for split in (erase, insert):
            with self.subTest(split.__name__):
                s = self.paragraph('אבגדה', '(12)')
                test_screen_update_cell_data(s)
                y = split(s)
                self.assertFalse(s.linebuf.is_continued(y))
                self.ae(str(s.line(y)), '(12)')
                test_screen_update_cell_data(s)
                self.assertIsNone(self.v2l(s, y))
                self.assert_shaped(s, y, self.fresh_sprites('(12)'))

    def test_overlay_row_keeps_map(self):
        # the pre-edit row keeps its map and is uploaded through it, then is as before once the overlay closes
        s = self.screen()
        draw_coloured(s, 'abc' + A)
        # moved into the gap at x 8 (draw leaves it at x 6): the empty cells join the RTL run
        s.cursor_position(1, 9)
        gap_v2l = (0, 1, 2, 7, 6, 5, 4, 3, 8, 9)
        logical = [s.cell_style_idx(0, x) for x in range(10)]
        visual = [logical[v] for v in gap_v2l[:6]]
        self.ae((self.refresh(s, 0), styles(self.uploaded(s, 0))[:6]), (gap_v2l, visual))
        s.test_update_overlay_text('xy')
        self.ae(self.refresh(s, 0), gap_v2l)
        over = styles(self.uploaded(s, 0))[:6]
        # the pre-edit text is drawn over b/c in one style of its own; the other cells keep theirs
        self.assertNotIn(over[1], logical)
        self.ae((over[1:3], over[:1] + over[3:]), ([over[1]] * 2, visual[:1] + visual[3:]))
        s.test_update_overlay_text('')
        self.ae((self.refresh(s, 0), styles(self.uploaded(s, 0))[:6]), (gap_v2l, visual))
        fresh = self.fresh_sprites('abc' + A, cols=10)  # laid out without the gap, in the MIXED_V2L order
        by_cell = {v: fresh[i] for i, v in enumerate(MIXED_V2L)}
        self.assert_shaped(s, 0, [by_cell[v] for v in gap_v2l])

    def test_split_paragraph_reshapes_lower_part(self):
        # the split changes row 1 to 0,0,0,1,0: paused frames show the snapshot, so a split made during the pause shows once unpaused
        def row1(s):
            # sprites and levels of row 1 in a new frame
            return self.sprites(self.uploaded(s, 1)), self.levels(s, 1)

        fresh = self.fresh_sprites('(12ב')
        for split_before_pause, paused_frames in ((False, 0), (False, 1), (False, 2), (True, 1), (True, 2)):
            with self.subTest(split_before_pause=split_before_pause, paused_frames=paused_frames):
                s = self.paragraph('אבגדה', '(12ב')
                unsplit = row1(s)
                self.ae((unsplit[1], self.memo(s, 1)), ((1, 2, 2, 1, 0), RTL))
                self.assertNotEqual(unsplit[0], fresh)
                split = (fresh, (0, 0, 0, 1, 0))
                in_snapshot = split if split_before_pause else unsplit
                if split_before_pause:
                    self.split(s)
                if paused_frames:
                    s.pause_rendering(True, 1000)
                for _ in range(paused_frames):
                    self.ae(row1(s), in_snapshot)
                if not split_before_pause:
                    self.split(s)
                    if paused_frames:
                        self.ae(row1(s), in_snapshot)
                self.ae(s.linebuf.dirty_lines(), [0])  # paused frames shape the snapshot's rows, not the live ones
                if paused_frames:
                    s.pause_rendering(False)
                self.ae(row1(s), split)

    def test_snapshot_carries_stamp(self):
        # no live frame runs, so only the snapshot's own flag can give a paused frame maps
        s = self.create_screen(cols=10, lines=5, scrollback=5, options={})
        visual = (0, 1, 2, 5, 4, 3)
        for stamped in (False, True):
            with self.subTest(stamped=stamped):
                if stamped:
                    parse_bytes(s, b'\x1b[8h\r')
                draw_coloured(s, 'abc' + A)
                logical = [s.cell_style_idx(0, x) for x in range(6)]
                s.pause_rendering(True, 1000)
                row, v2l = styles(self.uploaded(s, 0))[:6], self.v2l(s, 0)
                s.pause_rendering(False)
                if stamped:
                    self.ae((v2l, row), (visual + ID10[6:], [logical[v] for v in visual]))
                else:
                    self.ae((v2l, row, s.test_bidi_counts()), (None, logical, (0, 0)))

    def test_paused_style_table_gc_keeps_permutation(self):
        # the paused walker uploads every snapshot row, so a full re-upload after a style remap is still in visual order
        s = self.screen(cols=5)
        parse_bytes(s, b'\x1b[3H')
        draw_coloured(s, 'abcde', first_colour=100)
        s.erase_in_line(2)
        s.cursor_position(1, 1)
        draw_coloured(s, NSI)
        test_screen_update_cell_data(s)
        s.pause_rendering(True, 1000)
        test_screen_update_cell_data(s)
        before = [s.cell_style_idx(0, x) for x in range(5)]
        s.test_gc_style_table()
        logical = [s.cell_style_idx(0, x) for x in range(5)]
        self.assertNotEqual(before, logical)
        self.ae(styles(self.uploaded(s, 0)), [logical[NSI_V2L[v]] for v in range(5)])

    def test_selection_and_inverse_maps(self):
        # NSI's map is not its own inverse: a swapped map lights visual 3 for logical 0 and visual 1 for logical 3
        s = self.nsi()
        self.ae([self.selected_columns(self.mixed(), 3, 4), self.selected_columns(s, 0, 0), self.selected_columns(s, 3, 3)], [[4, 5], [4], [0]])
        s = self.screen()
        s.draw(MIXED + ' d')
        v2l = self.refresh(s, 0)
        levels = self.levels(s, 0)
        for x in range(s.columns):
            vx = s.test_bidi_visual_x(x, 0)
            self.ae(
                (s.test_bidi_logical_x(vx, 0), vx, s.test_bidi_logical_x(x, 0), s.test_bidi_is_rtl(x, 0)), (x, v2l.index(x), v2l[x], bool(levels[x] & 1)), x
            )
        self.ae((s.test_bidi_visual_x(2, 3), s.test_bidi_logical_x(2, 3), s.test_bidi_is_rtl(2, 3)), (2, 2, False))
        s = self.nsi()
        self.ae(
            (self.levels(s, 0), tuple(s.test_bidi_logical_x(x, 0) for x in range(5)), tuple(s.test_bidi_visual_x(x, 0) for x in range(5))),
            (NSI_LEVELS, NSI_V2L, NSI_L2V),
        )
        # a click's half of an RTL cell is flipped, as its logical left half is drawn on the right, not of the level 2 digits
        self.ae([s.test_bidi_logical_click(vx, 0, True) for vx in (0, 4)], [(3, True), (0, False)])
        self.ae(self.mixed().test_bidi_logical_click(1, 0, True), (1, True))

    def test_selection_stride_moves_whole_cells(self):
        # with several bytes per cell, as on the GPU, each cell's bytes move together: its bits stay in its first byte
        s = self.nsi()
        parse_bytes(s, b'\x1b[>1;2:1:2; q')  # an extra cursor on logical cell 1
        s.start_selection(0, 0)
        s.update_selection(0, 0)
        test_screen_update_cell_data(s)
        one = s.current_selections()
        self.ae(self.selection_rows(s, one)[0], [0, 0, 0, EXTRA_CURSOR, 1])
        for stride in (2, 5):
            with self.subTest(stride=stride):
                self.ae(s.test_apply_selection(stride), b''.join(bytes((c,)) + bytes(stride - 1) for c in one))

    def test_extra_cursors_on_visual_cells(self):
        s = self.mixed()
        parse_bytes(s, b'\x1b[>1;2:1:4;2:1:1;2:2:4; q')  # logical cells 3 and 0 of row 0, 3 of row 1
        test_screen_update_cell_data(s)
        c = EXTRA_CURSOR
        self.ae(self.selection_rows(s, s.current_selections())[:2], [[c, 0, 0, 0, 0, c, 0, 0, 0, 0], [0, 0, 0, c, 0, 0, 0, 0, 0, 0]])

    def test_cursor_render_x(self):
        # the cursor is drawn on its cell's visual column; on a blank cell or the pending wrap column after an RTL
        # character it is drawn left of that character, where the next one goes
        for text, x, want in (
            (MIXED, 3, 5),
            (MIXED, 6, 2),
            ('ls שלום', 7, 2),
            ('שלום' + 'd', 4, 4),
            ('ab' + 'שלום' + ' d', 6, 1),
            (A + ' 12', 6, 2),
            ('abcdef' + 'אבגד', 10, 5),
            ('אבגד' + 'abcdef', 10, 10),
            ('abc', 1, 1),
        ):
            with self.subTest(text=text, x=x):
                s = self.screen()
                s.draw(text)
                if s.cursor.x != x:
                    s.cursor_position(1, x + 1)
                self.ae(s.cursor.x, x)
                test_screen_update_cell_data(s)
                self.ae(s.test_cursor_render_x(), want)
        # the paused snapshot has the leading row of pixel scroll, so its row y + 1 is the cursor's
        s = self.screen()
        parse_bytes(s, f'abcdefgh\r\nls {A}'.encode())
        test_screen_update_cell_data(s)
        self.ae(s.test_cursor_render_x(), 2)
        s.pause_rendering(True, 1000)
        test_screen_update_cell_data(s)
        self.ae(s.test_cursor_render_x(), 2)

    def test_resize_narrower_drops_maps(self):
        # until the next frame, maps sized for the old width must not be used
        s = self.screen(cols=20, lines=3)
        s.draw(''.join(chr(0x5D0 + i) for i in range(20)))
        self.ae(self.refresh(s, 0), tuple(range(19, -1, -1)))
        s.resize(3, 10)
        self.ae([(s.test_bidi_logical_x(x, 0), s.test_bidi_visual_x(x, 0)) for x in range(10)], [(x, x) for x in range(10)])
        self.ae(self.refresh(s, 0), tuple(range(9, -1, -1)))

    def test_alternate_screen_ignores_main_maps(self):
        # without pixel scroll the alternate screen has as many render rows as the main one: only the source and its stamp tell the maps apart
        s = self.mixed(pixel_scroll=False)
        self.ae(self.queries(s), ([4, 5], 2, 5))
        parse_bytes(s, b'\x1b[?1049h\x1b[H')
        s.draw(MIXED)
        self.ae((s.test_bidi_state()[3:], s.cursor.x), ((True, False), 6))
        self.ae(self.queries(s), ([3, 4], 6, 3))
        # once stamped too, the alternate screen has no maps of its own until its next frame
        parse_bytes(s, b'\x1b[8h\x1b[H')
        s.draw(MIXED)
        self.ae((s.test_bidi_state()[4], s.test_cursor_render_x(), s.test_bidi_logical_x(3, 0)), (True, 6, 3))
        parse_bytes(s, b'\x1b[?1049l')
        self.ae((s.test_cursor_render_x(), s.test_bidi_logical_x(3, 0)), (2, 5))

    def test_option_off_at_runtime(self):
        # force_ltr turned on at runtime shapes plainly in logical order, turned off again reorders
        plain, reordered = self.fresh_sprites(MIXED, cols=10, force_ltr=True), self.fresh_sprites(MIXED, cols=10)
        self.assertNotEqual(plain, reordered)
        s = self.mixed()
        self.ae(self.refresh(s, 0), MIXED_V2L)
        for force_ltr, v2l, sprites in ((True, None, plain), (False, MIXED_V2L, reordered)):
            with self.subTest(force_ltr=force_ltr):
                self.set_options({'force_ltr': force_ltr})
                s.test_bidi_check_mode()
                self.ae(
                    (self.sprites(self.uploaded(s, 0)), self.v2l(s, 0), tuple(s.test_bidi_logical_x(x, 0) for x in range(10))),
                    (sprites, v2l, v2l or ID10),
                )
        # frames after a resize under force_ltr leave the selection clean
        s = self.mixed()
        self.refresh(s, 0)
        self.set_options({'force_ltr': True})
        s.resize(5, 8)
        s.start_selection(0, 0)
        s.update_selection(3, 0)
        s.test_bidi_check_mode()
        test_screen_update_cell_data(s)
        s.current_selections()
        self.assertFalse(s.is_selection_dirty())
        test_screen_update_cell_data(s)
        self.assertFalse(s.is_selection_dirty())

    def test_force_ltr_on_at_runtime_stops_using_maps(self):
        s = self.mixed()
        self.ae(self.queries(s), ([4, 5], 2, 5))
        self.assertFalse(s.is_selection_dirty())
        self.set_options({'force_ltr': True})
        # the maps of the last upload are not used even before the next frame
        self.ae((self.v2l(s, 0), s.test_cursor_render_x(), s.test_bidi_logical_x(3, 0), s.is_selection_dirty()), (None, 6, 3, True))
        s.test_bidi_check_mode()
        test_screen_update_cell_data(s)
        self.assertTrue(s.is_selection_dirty())
        self.ae(self.lit(s), [3, 4])
        self.assertFalse(s.is_selection_dirty())
        test_screen_update_cell_data(s)
        self.assertFalse(s.is_selection_dirty())

    def test_force_ltr_change_while_paused(self):
        # the change dirties every snapshot row, so the next paused upload shapes them all in the new mode
        plain, reordered = self.fresh_sprites(MIXED, cols=10, force_ltr=True), self.fresh_sprites(MIXED, cols=10)
        s = self.mixed()
        test_screen_update_cell_data(s)
        s.pause_rendering(True, 1000)
        self.ae(self.sprites(self.uploaded(s, 0)), reordered)
        for force_ltr, v2l, sprites in ((True, None, plain), (False, MIXED_V2L, reordered)):
            with self.subTest(force_ltr=force_ltr):
                self.set_options({'force_ltr': force_ltr})
                self.assertTrue(s.test_bidi_mode_changed())
                s.test_bidi_check_mode()
                self.assertFalse(s.test_bidi_mode_changed())
                renders = s.test_bidi_counts()[1]
                self.ae((self.sprites(self.uploaded(s, 0)), self.v2l(s, 0)), (sprites, v2l))
                if not force_ltr:
                    self.ae(s.test_bidi_counts()[1] - renders, s.lines + 1)

    def test_force_ltr_change_realised_on_any_buffer(self):
        # a screen with maps realises the change whichever buffer it shows; reordered, ( between R letters is
        # shaped mirrored, so a row not shaped again in the new mode keeps a glyph of the old one
        text = 'abc א(ב'
        plain = self.fresh_sprites(text, cols=10, force_ltr=True)
        for what, seq in (('alternate screen', b'\x1b[?1049h'), ('hard reset', b'\x1bc')):
            with self.subTest(what):
                s = self.screen()
                s.draw(text)
                test_screen_update_cell_data(s)
                parse_bytes(s, seq)
                self.set_options({'force_ltr': True})
                self.assertTrue(s.test_bidi_mode_changed())
                s.test_bidi_check_mode()
                self.assertFalse(s.test_bidi_mode_changed())
                if what == 'alternate screen':
                    # the main screen, hidden when the change was realised, is shaped in the new mode once shown again
                    test_screen_update_cell_data(s)
                    parse_bytes(s, b'\x1b[?1049l')
                    self.ae((self.sprites(self.uploaded(s, 0)), self.v2l(s, 0)), (plain, None))

    def test_rows_written_under_force_ltr_reordered_once_it_is_off(self):
        # under force_ltr no row is scanned: turning it off rescans the rows shown meanwhile, live and in the history
        s = self.screen(cols=5, lines=2, scrollback=10, force_ltr=True)
        parse_bytes(s, f'{A}\r\nx\r\n{A}'.encode())
        self.ae((s.historybuf.count, str(s.historybuf.line(0))), (1, A))
        for up in (True, False):
            s.scroll(1, up)
            test_screen_update_cell_data(s)
        self.ae((self.memo(s, 1), s.historybuf.dirty_lines(), s.linebuf.dirty_lines()), (IMPLICIT, [], []))
        self.set_options({'force_ltr': False})
        s.test_bidi_check_mode()
        self.ae(self.refresh(s, 1), (2, 1, 0, 3, 4))
        s.scroll(1, True)
        self.ae(self.refresh(s, 0), (2, 1, 0, 3, 4))

    def test_bdsm_explicit_disables_reordering(self):
        # CSI 8 l changes only what rows written afterwards are stamped with
        v2l = NSI_V2L + ID10[5:]
        s = self.screen()
        for y in range(2):
            parse_bytes(s, f'\x1b[{y + 1};1H'.encode())
            s.draw(NSI)
        before = self.uploaded(s, 0), self.uploaded(s, 1)
        self.ae([self.v2l(s, y) for y in range(2)], [v2l] * 2)
        # guessed shaping reverses only the right-to-left run, so NSI's digits stay on its right
        plain, reordered = self.guessed_sprites(NSI, cols=10), self.fresh_sprites(NSI, cols=10)
        self.assertNotEqual(plain, reordered)
        parse_bytes(s, b'\x1b[8l')
        self.ae((self.uploaded(s, 0), self.uploaded(s, 1)), before)
        # overwriting a stamped row keeps its stamp
        parse_bytes(s, b'\x1b[1;1H')
        s.draw(NSI)
        self.ae((self.refresh(s, 0), self.sprites(self.uploaded(s, 0))), (v2l, reordered))
        # a fresh row is not stamped
        parse_bytes(s, b'\x1b[4;1H')
        s.draw(NSI)
        self.ae((self.refresh(s, 3), self.memo(s, 3), self.sprites(self.uploaded(s, 3))), (None, NONE, plain))
        self.ae(self.v2l(s, 0), v2l)

    def test_tmux_like_opt_out(self):
        # an application on the alternate screen toggling BDSM leaves the main screen's rows as they were
        for end in (b'\x1b[8l\x1b[8h', b'\x1b[8l'):
            with self.subTest(end=end):
                s = self.screen()
                s.draw(MIXED)
                parse_bytes(s, b'\r\n')
                s.draw(A)
                v2l = [self.refresh(s, y) for y in range(2)]
                self.ae(v2l[0], MIXED_V2L)
                self.assertIsNotNone(v2l[1])
                sprites = [self.sprites(self.uploaded(s, y)) for y in range(2)]
                memos = [self.memo(s, y) for y in range(2)]
                parse_bytes(s, b'\x1b[?1049h\x1b[8h')
                s.draw(MIXED)
                parse_bytes(s, b'\x1b[8l')
                s.draw(A)
                parse_bytes(s, b'\x1b[8h' + end + b'\x1b[?1049l')
                self.ae([self.refresh(s, y) for y in range(2)], v2l)
                self.ae([self.sprites(self.uploaded(s, y)) for y in range(2)], sprites)
                self.ae([self.memo(s, y) for y in range(2)], memos)

    def test_arabic_joining_font(self):
        if not shutil.which('fc-match'):
            self.skipTest('fc-match is not available')
        path = subprocess.run(['fc-match', '-f', '%{file}', ':charset=0627'], capture_output=True, text=True).stdout.strip()
        if not path:
            self.skipTest('no font covers U+0627')
        word = 'بيت'  # beh yeh teh: joins to its neighbours in a shaping font, with no ligature
        with setup_for_testing(main_face_path=path):
            # guessed shaping already shows a lone right-to-left run in visual order
            guessed = self.guessed_sprites(word, cols=3)
            if guessed == [self.guessed_sprites(ch, cols=1)[0] for ch in reversed(word)]:
                self.skipTest(f'{path} does not join Arabic letters')
            self.ae(self.fresh_sprites(word, cols=3), guessed)
            s = self.screen(cols=10)
            s.draw('ab ' + word)
            self.ae(self.refresh(s, 0), (0, 1, 2, 5, 4, 3) + ID10[6:])
            self.ae(self.sprites(self.uploaded(s, 0))[3:6], guessed)

    ABC_A = (0, 1, 2, 5, 4, 3) + ID10[6:]
    EDGE_PARAGRAPH = 'אבג abאב 12ab'
    CACHE_ROWS = ['אבג abc', 'abc אבג', 'אב', 'xyz', 'אבגדה']

    def test_dirty_row_redraws_only_rows_whose_levels_change(self):
        s = self.paragraph('אבגדה', '12 3!', 'abcde', 'fghij', 'klmno', lines=6)
        self.frame(s)
        self.ae((self.lv(s, 1), s.linebuf.dirty_lines()), ((2, 2, 1, 2, 0), []))
        self.beta_over_a(s, 1)
        f = self.paragraph('אבגדה', '12 3!', 'בbcde', 'fghij', 'klmno', lines=6)
        self.ae(self.frame(f)[0], 1)
        self.ae(self.lv(f, 1), (2, 2, 1, 2, 1))
        want = [self.sprites(self.uploaded(f, y)) for y in range(5)]
        self.assertTrue(all(map(any, want)))
        self.ae([self.sprites(self.uploaded(s, y)) for y in range(5)], want)
        s.cursor_position(4, 2)
        s.draw('z')  # Latin over Latin changes no levels: only the written row redraws
        self.ae(self.frame(s)[1], 1)

    def test_paused_frame_redraws_only_rows_whose_levels_change(self):
        # only rows 0 (written) and 1 redraw, with and without the leading render row of pixel scroll
        for pixel_scroll in (False, True):
            with self.subTest(pixel_scroll=pixel_scroll):
                opts = {'pixel_scroll': pixel_scroll}
                s = self.paragraph('אבגדה', '(12ב', **opts)
                parse_bytes(s, 'אב\r\nגד'.encode().join((b'\r\n', b'')))
                self.frame(s)
                self.assertTrue(all(self.lv(s, y) is not None for y in (1, 2, 3)))
                self.split(s)
                s.pause_rendering(True, 1000)
                self.ae(self.frame(s)[1], 2)
                self.ae(self.sprites(self.uploaded(s, 0)), self.fresh_sprites('אבגדו', **opts))
                self.ae(self.sprites(self.uploaded(s, 1)), self.fresh_sprites('(12ב', **opts))
                self.ae(self.frame(s)[1], 0)

    def test_line_scrolled_into_view_reshaped_after_split(self):
        # (12ב split while out of view: it reshapes when scrolled back in, live or paused
        for paused in (False, True):
            with self.subTest(paused=paused):
                s = self.screen(cols=5, lines=3, scrollback=10)
                parse_bytes(s, b'x\r\ny\r\n')
                s.draw('אבגדה(12ב')
                self.frame(s)
                self.ae(self.lv(s, 2), (1, 2, 2, 1, 0))
                s.scroll(1, True)
                self.frame(s)
                self.ae(s.scrolled_by, 1)
                self.split(s, row=2)
                s.scroll(1, False)
                self.ae(s.scrolled_by, 0)
                if paused:
                    s.pause_rendering(True, 1000)
                self.ae((self.sprites(self.uploaded(s, 2)), self.lv(s, 2)), (self.fresh_sprites('(12ב'), (0, 0, 0, 1, 0)))

    def test_paused_frame_row_continuing_from_history(self):
        # row 0 (12ב continues from history; the paused snapshot resolves row 0 alone at 0,0,0,1,0, the next live frame at 1,2,2,1,0
        opts = {'pixel_scroll': False}
        s = self.paragraph('אבגדה', '(12ב', lines=3, scrollback=10, **opts)
        parse_bytes(s, b'\r\nx\r\ny')
        self.frame(s)
        self.ae(self.lv(s, 0), (1, 2, 2, 1, 0))
        s.pause_rendering(True, 1000)
        self.frame(s)
        self.ae(self.lv(s, 0), (0, 0, 0, 1, 0))
        self.assert_shaped(s, 0, self.fresh_sprites('(12ב', **opts))
        s.pause_rendering(False)
        self.frame(s)
        self.ae(self.lv(s, 0), (1, 2, 2, 1, 0))
        self.assert_shaped(s, 0, self.sprites(self.uploaded(self.paragraph('אבגדה', '(12ב', lines=3, **opts), 1)))

    def recycle_history_number(self, paused):
        # X is pushed at the absolute history number of O after O was popped: O's glyphs and cache entry must not be reused
        opts = {'pixel_scroll': False}
        s = self.screen(cols=5, lines=3, scrollback=10, **opts)
        s.draw('א2 3ב')  # O
        parse_bytes(s, b'\r\nh\r\ni\r\nj')
        self.ae((s.historybuf.count, str(s.historybuf.line(0))), (1, 'א2 3ב'))
        s.scroll(1, True)
        self.warm(s)
        o = self.fresh_sprites('א2 3ב', **opts)
        self.ae((self.lv(s, 0), self.sprites(self.uploaded(s, 0))), ((1, 2, 1, 2, 1), o))
        if paused:
            s.pause_rendering(True, 1000)
        s.reverse_scroll(1, True)  # O back onto the screen
        self.ae(s.historybuf.count, 0)
        self.put(s, 1, 1, 'א2 3(')  # X
        s.cursor_position(3, 1)
        parse_bytes(s, b'\n')  # X into the history
        self.ae((s.historybuf.count, str(s.historybuf.line(0))), (1, 'א2 3('))
        if paused:
            self.assert_shaped(s, 0, o, 'the snapshot still shows O')
            s.pause_rendering(False)
        s.scroll(1, True)
        x = self.fresh_sprites('א2 3(', **opts)
        self.assertNotEqual(x, o)
        self.assert_shaped(s, 0, x)
        f = self.screen(cols=5, lines=2, **opts)
        f.draw('א2 3(')
        self.frame(f)
        self.ae(self.vmaps(s)[0], self.vmaps(f)[0])

    def test_history_slot_reuse_reshapes_line(self):
        self.recycle_history_number(False)

    def test_history_slot_reuse_reshapes_line_in_paused_frame(self):
        self.recycle_history_number(True)

    def test_full_ring_push_reshapes_only_reused_slots(self):
        # a full ring reuses the slot of the oldest line: only the new line reshapes, wherever the view is
        texts = ['אב 12', 'א2 3(', 'ab אב', 'אבגד.', 'א 12b', '(12ב', 'אב cd', 'a אבג']

        def ring():
            s = self.screen(cols=5, lines=4, scrollback=0, pixel_scroll=False)
            parse_bytes(s, '\r\n'.join(texts).encode())
            self.frame(s)
            self.prerender(s)
            return s

        def push(s, scroll):
            s.scroll(scroll, True)
            self.warm(s)
            self.ae(s.scrolled_by, min(scroll, 4))
            s.cursor_position(4, 1)
            parse_bytes(s, b'\n')
            return self.frame(s)

        def alone(text):
            # the maps and levels of a line drawn alone, and its glyphs
            f = self.screen(cols=5, lines=1, scrollback=0, pixel_scroll=False)
            f.draw(text)
            self.frame(f)
            return self.vmaps(f)[0], self.lv(f, 0), self.fresh_sprites(text, pixel_scroll=False)

        # at scroll 2 the view keeps its lines: the index renames the pushed line to its history id and the screen line in place,
        # so nothing resolves;
        # at scroll 10 the view is capped at the full ring and slides over it: the follow moves the entries up one row, so only
        # the pushed line, never cached below the view, resolves
        for scroll, resolves in ((2, 0), (10, 1)):
            with self.subTest(scroll=scroll):
                s = ring()
                self.ae(s.historybuf.count, 4)
                self.ae(push(s, scroll), (resolves, 1))
                lines = texts[1:] + ['']
                start = len(lines) - 4 - s.scrolled_by
                for y, text in enumerate(lines[start : start + 4]):
                    vmap, levels, sprites = alone(text)
                    self.ae((self.vmaps(s)[y], self.lv(s, y)), (vmap, levels), f'row {y}')
                    self.assert_shaped(s, y, sprites, f'row {y}')

    def test_dirty_row_below_view_redraws_paragraph(self):
        # scrolled up one row: a dirty row below the view re-resolves the paragraph; row 2 redraws, others keep their levels
        s = self.paragraph('אבגדה', 'אבגדה', '12 3!', 'abcde', lines=3, scrollback=10)
        self.warm(s)
        s.scroll(1, True)
        self.warm(s)
        self.ae((s.scrolled_by, s.linebuf.dirty_lines(), self.lv(s, 2)), (1, [], (2, 2, 1, 2, 0)))
        self.beta_over_a(s, 2, scrolled_by=1, renders=1)

    def test_cache_reuses_unchanged_paragraphs(self):
        # one dirty row makes only its own paragraph resolve again
        rows = ['אבג abc'] * 3
        s = self.filled(rows)
        self.ae([self.frame(s)[0], self.frame(s)[0]], [3, 0])
        before = self.vmaps(s)
        self.ae([m is None for m in before], [False] * 3 + [True] * 2)
        s.current_selections()
        self.assertFalse(s.is_selection_dirty())
        self.put(s, 2, 10, 'z')
        self.ae(self.frame(s)[0], 1)
        after = self.vmaps(s)
        self.ae((after[0], after[2]), (before[0], before[2]))
        f = self.filled(rows)
        self.put(f, 2, 10, 'z')
        self.frame(f)
        self.ae(after, self.vmaps(f))
        self.ae(self.frame(s), (0, 0))

    def test_cache_view_change_resolves_again(self):
        # every trigger changes which line a render row shows (or the layout) without a dirty row: the entries are dropped, except
        # that a full-height index follows its lines and keeps them
        rows = self.CACHE_ROWS[:2] + ['אב', 'xyz', 'אבגדה']
        hist = ['אבג', 'abאב', 'אב12', 'xyz', 'אבגדה', 'a', 'אבג abc']
        long_row = 'אבגדהוזחטיכלמ'

        def net_zero_build():
            s = self.screen()
            parse_bytes(s, b'\x1b[?1049h\x1b[8h')
            self.put(s, 4, 1, long_row)
            s.cursor_position(1, 1)
            return s

        def net_zero(s):
            # the long row comes back to row 3 with blank row 4 still its continuation: only the linebuf movement generation differs
            parse_bytes(s, b'\x1b[L\x1b[M')
            self.assertTrue(s.linebuf.is_continued(4))

        def bottom_build():
            s = self.screen(cols=5, lines=5, scrollback=10)
            parse_bytes(s, b'x0\r\nx1\r\nx2\r\n')
            s.draw(self.EDGE_PARAGRAPH)
            self.prerender(s)
            return s

        def top_build():
            s = self.screen(cols=5, lines=5, scrollback=10, pixel_scroll=False)
            s.draw(self.EDGE_PARAGRAPH)
            parse_bytes(s, b'\r\nx1\r\nx2\r\nx3\r\nx4\r\n')
            self.prerender(s)
            s.scroll(3, True)
            return s

        m = self.plain_maps([self.EDGE_PARAGRAPH], 5, lines=5)
        self.ae([x is not None for x in m], [True] * 3 + [False] * 2)
        up, down = (lambda s: s.scroll(1, True)), (lambda s: s.scroll(1, False))
        rev = tuple(range(12, -1, -1))
        # (name, build, trigger, expected maps, compare with a fresh screen, expected resolves or None for some)
        cases = (
            # a full-height index follows the lines instead of dropping them: nothing resolves, and the line pushed into the pixel scroll
            # row is a dirty one-row paragraph that the shortcut verifies
            ('index', lambda: self.filled(rows), lambda s: parse_bytes(s, b'\x1b[5;1H\x1bD'), self.plain_maps(rows[1:], lines=5), True, 0),
            ('reverse index', lambda: self.filled(rows), lambda s: parse_bytes(s, b'\x1b[1;1H\x1bM'), self.plain_maps([''] + rows[:-1]), True, None),
            ('insert then delete line', net_zero_build, net_zero, (None, None, None, rev[3:], None), True, None),
            ('scrollback view', lambda: self.history(hist), up, self.plain_maps(hist[5:], 5, lines=3), False, None),
            ('view crossing bottom edge', bottom_build, up, (None, None, None, m[0], m[1]), False, None),
            ('view crossing top edge', top_build, down, (m[1], m[2], None, None, None), False, None),
            # entries follow here too: the view stays on the same lines, the pushed line is renamed to its history id and the shortcut
            # verifies it on view; the new row is below the view
            (
                'history add while scrolled',
                lambda: self.history(hist[:6], 2),
                lambda s: parse_bytes(s, '\x1b[3;1H\nאב'.encode()),
                self.plain_maps(hist[2:5], 5),
                True,
                0,
            ),
            ('resize rewrap', lambda: self.drawn(long_row), lambda s: s.resize(5, 13), (rev, None, None, None, None), True, None),
        )
        for name, build, trigger, expect, fresh, resolves in cases:
            with self.subTest(name):
                self.trigger_case(build, trigger, expect, fresh, resolves)

    def drawn(self, text, **opts):
        s = self.screen(**opts)
        s.draw(text)
        return s

    def test_cache_option_toggle(self):
        # force_ltr on drops the maps, turning it off resolves again, then the cache holds
        s = self.drawn('abc' + A)
        self.warm(s)
        self.ae(self.vmaps(s)[0], self.ABC_A)
        for force_ltr in (True, False):
            self.set_options({'force_ltr': force_ltr})
            s.test_bidi_check_mode()
            resolves = self.frame(s)[0]
            if force_ltr:
                self.ae((self.vmaps(s)[0], resolves), (None, 0))
        self.assertGreater(resolves, 0)
        self.ae((self.vmaps(s)[0], self.frame(s)[0]), (self.ABC_A, 0))

    def test_cache_losing_and_gaining_rtl(self):
        # Overwriting the only RTL run leaves a Latin row (nothing to resolve); Hebrew written into it resolves
        s = self.drawn('abc' + A)
        self.warm(s)
        self.put(s, 1, 4, 'xyz')
        self.frame(s)
        self.assertIsNone(self.vmaps(s)[0])
        self.warm(s)
        self.put(s, 1, 4, A)
        self.ae((self.frame(s)[0], self.vmaps(s)[0], self.frame(s)[0]), (1, self.ABC_A, 0))

    def test_cache_tall_paragraph_always_resolves(self):
        # a paragraph cut by the 256 row window is never cached: unchanged frames still resolve it
        s = self.screen(cols=2, lines=300, scrollback=200)
        s.draw('א!' * 400)
        self.ae(s.historybuf.count, 100)
        seen = []
        for _ in range(3):
            self.assertGreater(self.frame(s)[0], 0)
            seen.append(self.vmaps(s))
        self.ae(seen, seen[:1] * 3)

    def test_cache_paused_rendering(self):
        # paragraphs entirely inside the view keep their entries through a pause: no resolves while paused or unpausing
        rows = ['אבג abc', 'abc אבג']
        s = self.filled(rows)
        self.warm(s)
        s.pause_rendering(True, 1000)
        self.ae(self.frame(s)[0], 0)
        s.pause_rendering(False)
        self.ae(self.frame(s)[0], 0)
        f = self.filled(rows)
        self.frame(f)
        self.ae(self.vmaps(s), self.vmaps(f))

        # a paragraph with rows above the view is cut by the snapshot, so it resolves while paused and again unpaused
        def build():
            s = self.screen(cols=5, lines=3, scrollback=10)
            s.draw('אבגד.12 34abcdeאבגד.12 34')
            self.ae(s.historybuf.count, 2)
            return s

        s = build()
        self.warm(s)
        s.pause_rendering(True, 1000)
        self.assertGreater(self.frame(s)[0], 0)
        s.pause_rendering(False)
        self.assertGreater(self.frame(s)[0], 0)
        f = build()
        self.frame(f)
        self.ae(self.state(s), self.state(f))

    def test_cache_reset(self):
        # RIS clears the stamps and both flags: no maps until CSI 8 h, and the text written after it resolves
        s = self.drawn('abc' + A)
        self.warm(s)
        self.ae(self.vmaps(s)[0], self.ABC_A)
        parse_bytes(s, b'\x1bc')
        self.frame(s)
        self.ae((self.vmaps(s), s.test_bidi_state()), ((None,) * 5, (False,) * 5))
        s.draw('abc' + A)
        self.frame(s)
        self.assertIsNone(self.vmaps(s)[0])
        parse_bytes(s, b'\x1b[8h\r')
        s.draw('abc' + A)
        self.assertGreater(self.frame(s)[0], 0)
        self.ae((self.vmaps(s)[0], self.frame(s)[0]), (self.ABC_A, 0))

    def test_cache_counter_wrap(self):
        # the generation wraps from UINT64_MAX to 0: rows moved by the delete must not hit the entries of the lines they replace
        rows = ['אבג abc', 'abc אבג', 'אבגדה abc', 'a אב', 'abcd אבגדה']
        s = self.filled(rows)
        s.test_bidi_set_generation(0xFFFFFFFFFFFFFFFF)
        self.warm(s)
        before = self.vmaps(s)
        self.ae(len(set(before)), 5)
        parse_bytes(s, b'\x1b[1;1H\x1b[M')
        self.assertGreaterEqual(self.frame(s)[0], 1)
        self.ae(self.vmaps(s), self.plain_maps(rows[1:], lines=5))
        self.assertNotIn(self.vmaps(s)[0], before[:1])

    def test_cache_resize_while_paused(self):
        # a resize unpauses first: no entry written for the old size or the snapshot may be used for the new one
        rows = ['אבגדה abc', 'abc אבגדה', 'אב 12 abcdef', 'xyz', 'אבגדהוזח']

        def build():
            s = self.filled(rows)
            self.warm(s)
            return s

        s = build()
        s.pause_rendering(True, 1000)
        self.frame(s)
        s.resize(5, 6)
        self.ae(self.frame(s)[0], 3)  # P2 and P3 on the screen, and P1 in the pixel scroll row above it
        # the oracle never rendered at the old size, so it holds no entry a stale lookup could reuse
        f = self.filled(rows)
        f.resize(5, 6)
        self.frame(f)
        self.ae(self.state(s), self.state(f))
        self.ae(self.frame(s), (0, 0))

    def test_hit_counts(self):
        s = self.filled(self.CACHE_ROWS)
        self.ae(self.frame(s)[0], 4)
        self.ae(self.frame(s), (0, 0))  # unchanged
        self.put(s, 2, 8, 'z')
        self.ae(self.frame(s)[0], 1)  # one cell
        self.ae(self.frame(s), (0, 0))
        s = self.history(['אבג', 'abאב', 'אב12', 'xyz', 'אבגדה', 'a', 'אבג abc'], up=2)
        self.warm(s)
        self.ae(self.frame(s), (0, 0))  # static, scrolled back
        # a paragraph with rows above the view (up=0), above and below it (up=1) and below it (up=2)
        for up in range(3):
            with self.subTest(straddling=up):
                s = self.screen(cols=5, lines=3, scrollback=10)
                s.draw('אבגד.12 34abcdeאבגד.12 34')
                if up:
                    self.frame(s)  # output is shown before the user scrolls back, so the rows below the view are clean
                    s.scroll(up, True)
                self.warm(s)
                self.ae(self.frame(s), (0, 0))
                self.ae(self.frame(s), (0, 0))

    def test_cache_scrolled_back_streaming(self):
        # output while scrolled back keeps the view on its lines: visible history paragraphs need no resolve
        lines = ['אבג', 'abאב', 'אב12', 'xyz', 'אבגדה', 'a', 'אבג abc', 'ב(12)', 'אב אב', 'ab', 'אבגדה1', 'xx']

        def build():
            s = self.screen(cols=5, lines=4, scrollback=50)
            for text in lines:
                parse_bytes(s, text.encode() + b'\r\n')
            return s

        s = build()
        s.scroll(6, True)
        self.warm(s)
        self.ae(self.frame(s)[0], 0)
        streamed = ['אב', 'cd', 'אבג', 'ef', 'אבגד']
        for text in streamed:
            parse_bytes(s, text.encode() + b'\r\n')
            self.ae(self.frame(s)[0], 0, text)
        f = build()
        for text in streamed:
            parse_bytes(f, text.encode() + b'\r\n')
        f.scroll(s.scrolled_by, True)
        self.ae(f.scrolled_by, s.scrolled_by)
        self.frame(f)
        self.ae(self.state(s), self.state(f))
        self.assertTrue(any(self.vmaps(s)))

    def test_cache_above_view_recycled_history_number(self):
        # a paragraph with rows above the view: its newest history row is popped and a different line pushed at the same
        # absolute number, which changes the level of the number starting the first row in view
        opts = {'pixel_scroll': False}
        p0, p1, p1b, p2, p3, p4 = '12 34', 'אבגדה', 'abcde', '12 אב', 'ab cd', 'אב ef'

        def build(mid):
            s = self.screen(cols=5, lines=3, scrollback=10, **opts)
            s.draw(p0 + mid + p2 + p3 + p4)
            self.ae((s.historybuf.count, str(s.historybuf.line(0))), (2, mid))
            return s

        s = build(p1)
        self.warm(s)
        before = self.state(s)
        s.reverse_scroll(1, True)  # p1 back onto the screen, p4 lost
        self.ae(s.historybuf.count, 1)
        self.put(s, 1, 1, p1b)
        s.cursor_position(3, 1)
        parse_bytes(s, b'\n')  # p1b into the history at the number of p1
        self.ae((s.historybuf.count, str(s.historybuf.line(0))), (2, p1b))
        self.put(s, 2, 5, p3[-1] + p4)  # p4 again as the continuation of p3, so the paragraph keeps its first row, last row and size
        self.assertGreater(self.frame(s)[0], 0)
        f = build(p1b)
        self.frame(f)
        self.ae(self.state(s), self.state(f))
        self.assertNotEqual(self.state(s), before)

    def test_cache_prompt_erase_moves_history_lines(self):
        # erasing a command deletes history lines from the middle: lines above move down and take the deleted lines' numbers
        def build(erased):
            s = self.screen(cols=10, lines=4, scrollback=30)
            s.draw('before\r\n')
            if erased:
                parse_bytes(s, b'\x1b]133;A\a')
                s.draw('$ p1\r\n')
                parse_bytes(s, b'\x1b]133;C\a')
                s.draw('o0 אב\r\nאבג o1\r\n')
            parse_bytes(s, b'\x1b]133;A\a')
            s.draw('$ l0\r\nאבגדהוזחטיכלמ\r\nabc אבג\r\nl3\r\nl4')
            return s

        s = build(True)
        self.warm(s)
        self.assertGreater(s.historybuf.count, 4)
        self.assertTrue(s.erase_last_command())
        self.assertGreater(self.frame(s)[0], 0)
        f = build(False)
        self.ae(s.historybuf.count, f.historybuf.count)
        self.frame(f)
        self.ae(self.state(s), self.state(f))
        self.assertTrue(any(self.vmaps(s)))

    def test_cache_below_view_rows_moved_under_a_history_entry(self):
        # scrolled back so only history is in view: a paragraph starts in its last row and continues on the screen below,
        # where a clean unrelated line moves into its last row without any row being written
        def build(region=False):
            s = self.screen(cols=5, lines=3, scrollback=10)
            parse_bytes(s, b'x1\r\nx2\r\n')
            s.draw('אבגד(' + '12 34' + 'a) cd')  # Q0 is pushed; Q1 at y0 and Q2 at y1 continue it, Q2 closing Q0's (
            parse_bytes(s, b'\r\nxyz')
            self.ae((s.historybuf.count, str(s.historybuf.line(0))), (3, 'אבגד('))
            self.frame(s)  # every screen row clean
            if region:
                parse_bytes(s, b'\x1b[2;3r')
            s.scroll(3, True)
            self.ae(s.scrolled_by, 3)
            return s

        for what, trigger, region in (
            ('CSI M', lambda s: parse_bytes(s, b'\x1b[2;1H\x1b[M'), False),
            ('CSI L', lambda s: parse_bytes(s, b'\x1b[2;1H\x1b[L'), False),
            ('region index', lambda s: parse_bytes(s, b'\x1b[3;1H\x1bD\x1b[r'), True),
        ):
            with self.subTest(what):
                s = build(region)
                self.warm(s)
                before = self.lv(s, 2)
                self.assertIsNotNone(before)
                trigger(s)
                self.assertTrue(s.linebuf.is_continued(1), 'Q1 still wraps into y1')
                self.ae(self.frame(s)[0], 1)
                self.assertNotEqual(self.lv(s, 2), before)
                f = build(region)
                trigger(f)
                self.frame(f)
                self.ae(self.state(s), self.state(f))
                self.ae(self.frame(s), (0, 0))

    def test_cache_placeholder_rows_never_hit(self):
        # a paragraph with an image placeholder row is never resolved, whatever entries its rows had
        s = self.paragraph('אבגדה', '(12ב')
        self.warm(s)
        self.ae((self.frame(s)[0], self.vmaps(s)[0] is not None), (0, True))  # the unchanged paragraph is cached
        self.put(s, 2, 1, 'ab' + chr(0x10EEEE))
        self.assertTrue(s.linebuf.is_continued(1))
        for _ in range(3):
            self.ae((self.frame(s)[0], self.vmaps(s)[:2]), (0, (None, None)))

    def test_cache_changes_during_a_pause(self):
        # lines moved on the live screen while paused, with no row written: the snapshot keeps showing the old rows, and the
        # next live frame must show the moved ones
        rows = ['אבג abc', 'abc אבג', 'אבגדה abc', 'a אב', 'abcd אבגדה']
        for what, trigger in (
            ('index', lambda s: parse_bytes(s, b'\x1b[5;1H\x1bD')),
            ('CSI M', lambda s: parse_bytes(s, b'\x1b[2;1H\x1b[M')),
            ('CSI L', lambda s: parse_bytes(s, b'\x1b[2;1H\x1b[L')),
        ):
            with self.subTest(what):
                s = self.filled(rows)
                self.warm(s)
                before = self.vmaps(s)
                s.pause_rendering(True, 1000)
                trigger(s)
                self.assertFalse(any(str(s.line(y)) for y in s.linebuf.dirty_lines()), 'only blank rows may be dirty')
                self.ae(self.frame(s)[0], 0)
                self.ae(self.vmaps(s), before)
                s.pause_rendering(False)
                self.frame(s)
                f = self.filled(rows)
                trigger(f)
                self.frame(f)
                self.ae(self.vmaps(s), self.vmaps(f))
                self.assertNotEqual(self.vmaps(s), before)
                self.ae(self.frame(s), (0, 0))

    MIXED3 = 'alpha שלום beta עולם (12) gamma'[:30]  # three 10 column rows, one paragraph
    MIXED3_STATE = (  # (maps, levels)
        ((0, 1, 2, 3, 4, 5, 9, 8, 7, 6),) * 2 + ((4, 2, 3, 1, 0, 5, 6, 7, 8, 9), None),
        ((0,) * 6 + (1,) * 4,) * 2 + ((1, 1, 2, 2, 1, 0, 0, 0, 0, 0), None),
    )

    def dirty(self, s):
        return s.linebuf.dirty_lines()

    def warmed(self, text, **opts):
        s = self.drawn(text, **opts)
        self.warm(s)
        return s

    def stacked(self, lines):
        s = self.screen(cols=5, lines=3, scrollback=10)
        for line in lines:
            s.draw(line)
        return s

    def same_as(self, s, f):
        # s has the maps and levels of a fresh screen f that only saw the final text
        self.frame(f)
        self.ae(self.state(s), self.state(f))

    def test_cache_same_class_edit_skips_resolve(self):
        # a dirty row that feeds FriBidi the same classes, brackets and cell layout keeps its paragraph's maps without resolving
        s = self.screen(lines=4, scrollback=0)
        s.draw(self.MIXED3)
        self.ae([self.frame(s)[0], self.frame(s)[0]], [1, 0])
        self.ae((self.state(s), self.dirty(s)), (self.MIXED3_STATE, []))
        self.put(s, 1, 1, 'x')
        self.ae(self.dirty(s), [0])
        self.ae(self.frame(s)[0], 0)
        self.ae(self.state(s), self.MIXED3_STATE)
        f = self.drawn('x' + self.MIXED3[1:], lines=4, scrollback=0)
        fbuf = test_screen_update_cell_data(f)
        self.ae(self.state(s), self.state(f))
        want = [self.sprites(gpu_row(f, fbuf, y)) for y in range(3)]
        self.assertTrue(all(map(any, want)), 'a blank fresh row')
        buf = test_screen_update_cell_data(s)
        self.ae([self.sprites(gpu_row(s, buf, y)) for y in range(3)], want)

    def test_cache_level_changing_edits_resolve(self):
        # an edit that changes a class, a bracket or the layout resolves again and gives a fresh screen's levels and maps
        wide = {'lines': 1, 'scrollback': 0}
        # (name, screen options, text, col of the edit, edit, text after, levels before, levels after)
        cases = (
            ('i bracket', {}, 'א(ב)c', 2, '[', 'א[ב)c', (1, 1, 1, 1, 0) + (0,) * 5, (1, 1, 1, 0, 0) + (0,) * 5),
            ('ii class', {}, 'אa!ב', 2, 'ג', 'אג!ב', (1, 0, 0, 1) + (0,) * 6, (1, 1, 1, 1) + (0,) * 6),
            ('iii layout', {'cols': 5}, '中בגd', 1, 'aבג中', 'aבג中', (0, 0, 1, 1, 0), (0, 1, 1, 0, 0)),
            ('vi interior continuation', wide, 'א!aב', 2, '😀', 'א😀ב', (1, 0, 0, 1) + (0,) * 6, (1, 1, 1, 1) + (0,) * 6),
            ('vii trailing continuation', {**wide, 'cols': 5}, 'אבג!ד', 4, '😀', 'אבג😀', (1, 1, 1, 1, 1), (1, 1, 1, 0, 0)),
        )
        for name, opts, text, col, new, final, levels0, levels1 in cases:
            with self.subTest(name):
                s = self.warmed(text, **opts)
                self.ae(self.lv(s, 0), levels0)
                self.put(s, 1, col, new)
                self.ae((str(s.linebuf.line(0)), self.frame(s)[0], self.lv(s, 0)), (final, 1, levels1))
                self.same_as(s, self.drawn(final, **opts))
        with self.subTest('iv dirty row below the view'):
            # one paragraph over a history row and linebuf rows 0-2, scrolled up by one: z on row 2 is below the view
            s = self.stacked(('אבגדה', 'אבגדה', '12 3!', 'abcde'))
            self.ae(s.historybuf.count, 1)
            self.frame(s)
            s.scroll(1, True)
            self.frame(s)
            self.warm(s)
            self.put(s, 3, 2, 'z')
            self.ae((s.scrolled_by, self.dirty(s)), (1, [2]))
            self.ae((self.frame(s)[0], self.lv(s, 2)), (1, (2, 2, 1, 2, 0)))
            s.scroll(1, False)
            self.frame(s)
            self.same_as(s, self.stacked(('אבגדה', 'אבגדה', '12 3!', 'azcde')))
        with self.subTest('v explicit embedding then a blank'):
            # ב/, an OSC 66 cell holding RLO, then EM SPACE (WS, not a blank): a blank for it keeps class WS, so the trim must not drop it
            def rlo(last):
                s = self.screen(cols=4, lines=1, scrollback=0)
                s.draw('ב/')
                parse_bytes(s, '\x1b]66;w=1;‮\x07'.encode())
                s.draw(last)
                self.ae(str(s.linebuf.line(0))[2], '‮')
                return s

            s = rlo(' ')
            self.warm(s)
            self.put(s, 1, 4, ' ')
            self.ae(self.frame(s)[0], 1)
            self.same_as(s, rlo(' '))

    def test_cache_bypass_between_resolves(self):
        # A resolves MIXED3; B draws a scale-2 multicell (paragraph inactive); C restores the cells: same FriBidi input as A, B's maps
        s = self.screen(lines=4, scrollback=0)
        s.draw(self.MIXED3)
        self.ae(self.frame(s)[0], 1)
        self.warm(s)
        self.ae(self.vmaps(s), self.MIXED3_STATE[0])
        s.cursor_position(1, 1)
        draw_multicell(s, 'x', scale=2)
        self.ae([s.linebuf.is_continued(y) for y in range(4)], [False, True, True, False])
        self.frame(s)
        self.ae(self.vmaps(s), (None,) * 4)
        self.put(s, 1, 1, self.MIXED3[:2])
        self.put(s, 2, 1, self.MIXED3[10:12])
        self.ae(str(s.linebuf.line(0)) + str(s.linebuf.line(1)), self.MIXED3[:20])
        self.ae([s.linebuf.is_continued(y) for y in range(4)], [False, True, True, False])  # same bounds as in A: only the signatures differ
        self.ae(self.frame(s)[0], 1)
        self.ae(self.state(s), self.MIXED3_STATE)

    def test_trailing_blank_trim_keeps_maps(self):
        # dropping the blanks at a paragraph's end before resolving must not change any row's maps. The trim is exact for
        # FriBidi but not for the N0 budget threshold (it scales with the trimmed length), so these cases stay far from it.
        # keep_typed_blanks is the exception, pinned at the end: it deliberately differs from the untrimmed FriBidi result.
        def wrapped_blank(s):
            s.draw('א' * 10)
            s.draw('ב')
            s.cursor_position(2, 1)
            s.erase_characters(1)
            self.assertTrue(s.linebuf.is_continued(1))

        def rlo_cell(s):  # draw drops a zero-width Cf, an OSC 66 cell keeps it; untrimmed, blanks after the RLO set the eos of the run before
            s.draw('ב/')
            parse_bytes(s, '\x1b]66;w=1;‮\x07'.encode())
            self.ae(str(s.linebuf.line(0))[2], '‮')

        cases = {
            'neutral then blanks': lambda s: s.draw('אבג!'),
            'brackets': lambda s: s.draw('אבג (x)'),
            'ES next to EN': lambda s: s.draw('אב 1+'),
            'ET after EN': lambda s: s.draw('אב 12%'),
            'ET': lambda s: s.draw('אב $'),
            'spaces after R': lambda s: s.draw('אבג   '),
            'blank last row': wrapped_blank,
            'wide char': lambda s: s.draw('אב中'),
            'LTR start': lambda s: s.draw('abc אב'),
            'RLO cell': rlo_cell,
            'FSI unmatched': lambda s: s.draw('א⁨ab!  '),
            'RLI unmatched': lambda s: s.draw('א⁧12 34  '),
            'RLI then PDI': lambda s: s.draw('א⁧12 34⁩  '),
        }
        for seqs in ((), (self.SCP_RTL,), (self.AUTO,)):
            for name, build in cases.items():
                with self.subTest(name, seqs=seqs):
                    screens = []
                    for trim in (True, False):
                        s = self.dscreen(*seqs)
                        s.test_bidi_trim(trim)
                        build(s)
                        s.cursor_position(5, 1)  # the cursor off the row: its gap would take the probe level
                        screens.append(s)
                    for s in screens:
                        self.warm(s)
                    rtl = bool(seqs) and (self.SCP_RTL in seqs or name != 'LTR start')
                    self.ae([self.prtl(s, 0) for s in screens], [rtl, rtl])
                    self.ae(self.state(screens[0]), self.state(screens[1]))
        # the isolates reach the engine whether or not the blanks are trimmed (both unmatched, and the PDI case, end in blanks at the base level)
        for seqs, text, want in (
            ((self.AUTO,), 'א⁨ab!  ', (9, 8, 7, 6, 5, 4, 1, 2, 3, 0)),
            ((), 'a⁧12 34  ', (0, 4, 5, 3, 1, 2, 6, 7, 8, 9)),
            ((), 'a⁧12 34⁩  ', (0, 4, 5, 3, 1, 2, 6, 7, 8, 9)),
        ):
            for trim in (True, False):
                with self.subTest(text=text, trim=trim):
                    s = self.dscreen(*seqs)
                    s.test_bidi_trim(trim)
                    s.draw(text)
                    s.cursor_position(5, 1)  # the cursor off the row: its gap would take the probe level
                    self.warm(s)
                    self.ae(self.vmaps(s)[0], want)
        # with the cursor left in the gap the trimmed and untrimmed results differ: only the trimmed one (the no_trim
        # reference skips the cursor gap) lays the blanks out at the probe level, off the paragraph level
        lv = {}
        for trim in (True, False):
            s = self.dscreen()
            s.test_bidi_trim(trim)
            s.draw('אבג   ')
            self.warm(s)
            lv[trim] = self.lv(s, 0)
        self.ae(lv[False], (1, 1, 1, 0, 0, 0, 0, 0, 0, 0))
        self.ae(lv[True], (1, 1, 1, 1, 1, 1, 0, 0, 0, 0))
        # the one deliberate difference: typed blanks after a letter in an unclosed fed isolate keep the letter's level when
        # trimmed (keep_typed_blanks), while the untrimmed reference resolves them with FriBidi, so they fall to the paragraph level
        for trim, want_v2l, want_levels in (
            (True, (0, 4, 3, 2, 1, 5, 6, 7, 8, 9), (0, 1, 1, 1, 1, 0, 0, 0, 0, 0)),
            (False, (0, 2, 1, 3, 4, 5, 6, 7, 8, 9), (0, 1, 1, 0, 0, 0, 0, 0, 0, 0)),
        ):
            with self.subTest(typed_blanks_in_isolate=trim):
                s = self.dscreen(self.AUTO)
                s.test_bidi_trim(trim)
                s.draw('a⁨אב  ')
                self.warm(s)
                self.ae((self.vmaps(s)[0], self.lv(s, 0)), (want_v2l, want_levels))
        # positive control: the N0 budget threshold follows the trimmed length (70 against 71 cells), so the switch must reach the engine
        states = []
        for trim in (True, False):
            s = self.screen(cols=80, lines=1)
            s.test_bidi_trim(trim)
            s.draw('א(ב)' + '()' * 33 + ' ')
            self.warm(s)
            states.append(self.state(s))
        self.assertNotEqual(*states)

    PARA_ROW = 'ב1aגd'  # R EN L R L: a digit, a Latin and a Hebrew letter in columns 1-4, so an edit never touches the soft-wrap column

    def frame_buf(self, s):
        before = s.test_bidi_counts()[0]
        buf = test_screen_update_cell_data(s)
        return s.test_bidi_counts()[0] - before, buf

    def assert_same_as_fresh(self, s, buf, f, msg=''):
        # maps, levels and sprites of the frame whose upload is buf equal those of a fresh screen f rendered once
        fbuf = test_screen_update_cell_data(f)
        self.ae(self.state(s), self.state(f), msg)
        want = [self.sprites(gpu_row(f, fbuf, y)) for y in range(s.lines)]
        self.assertTrue(any(map(any, want)), 'blank fresh rows')
        self.ae([self.sprites(gpu_row(s, buf, y)) for y in range(s.lines)], want, msg)

    def test_shortcut_class_changing_edits_miss(self):
        # an edit that changes what FriBidi is fed (a class, a bracket, the layout, a blank) resolves once
        def drawn(text):
            return lambda: self.drawn(text)

        cases = (
            ('latin to hebrew', drawn('abc אבג'), lambda s: self.put(s, 1, 2, 'ב')),
            ('bracket added', drawn('אבג abc'), lambda s: self.put(s, 1, 4, '(')),
            ('bracket removed', drawn('א(ב)c'), lambda s: self.put(s, 1, 2, '!')),
            ('( to [', drawn('א(ב)c'), lambda s: self.put(s, 1, 2, '[')),
            ('blank to EM SPACE on the last row tail', drawn('אבג abc'), lambda s: self.put(s, 1, 10, ' ')),
        )
        for name, build, trigger in cases:
            with self.subTest(name):
                self.trigger_case(build, trigger, resolves=1)
        with self.subTest('cell turning into a continuation and back'):
            s = self.warmed('אבג!abc')
            self.put(s, 1, 5, '中')  # replaces the cells of a and b
            self.ae(self.frame(s)[0], 1)
            self.same_as(s, self.drawn('אבג!中c'))
            self.put(s, 1, 5, 'ab')
            self.ae(self.frame(s)[0], 1)
            self.same_as(s, self.drawn('אבג!abc'))

    def test_shortcut_blank_bit_at_budget_boundary(self):
        # 'א(ב)' and 33 pairs: scan 1123 against 16 per char. n_trim is 70 with a U+0020 tail, so 1120 and the budget fires
        # and N0 is skipped; an EM SPACE tail (WS, not a blank) keeps n_trim 71, so 1136 and N0 pairs the brackets. The two
        # texts feed FriBidi the same types and brackets and differ only in the blank bit
        text = 'א(ב)' + '()' * 33

        def build(tail):
            s = self.screen(cols=80, lines=1, scrollback=0)
            s.draw(text + tail)
            self.ae(str(s.linebuf.line(0)), text + tail)
            return s

        s = build(' ')
        self.warm(s)
        warmed = self.state(s)
        self.ae(self.state(self.same_state(build(' '))), warmed)
        self.put(s, 1, 71, ' ')
        self.ae(self.frame(s)[0], 1)
        self.same_as(s, build(' '))
        self.assertNotEqual(self.state(s), warmed, 'the budget firing must change the maps')

    def same_state(self, s):
        self.frame(s)
        return s

    def test_shortcut_same_class_edits(self):
        # a dirty row that feeds FriBidi the same classes keeps the maps of a 4-row soft-wrapped paragraph; the signatures
        # stay valid after a shortcut, so a second edit shortcuts too
        edits = {'digit': (2, '57'), 'latin': (3, 'xq'), 'hebrew': (1, 'תש')}
        for row in (0, 1, 3):
            for kind, (col, chars) in edits.items():
                with self.subTest(row=row, kind=kind):
                    rows = [self.PARA_ROW] * 4
                    s = self.paragraph(*rows)
                    self.ae([s.linebuf.is_continued(y) for y in range(4)], [False, True, True, True])
                    self.ae(self.frame(s)[0], 1)
                    self.ae(self.frame(s)[0], 0)
                    self.assertTrue(all(self.vmaps(s)[:4]))
                    for ch in chars:
                        rows[row] = rows[row][: col - 1] + ch + rows[row][col:]
                        self.put(s, row + 1, col, ch)
                        self.ae(self.dirty(s), [row])
                        resolves, buf = self.frame_buf(s)
                        self.ae(resolves, 0, f'edit {ch}')
                        self.assert_same_as_fresh(s, buf, self.paragraph(*rows), f'edit {ch}')

    def test_shortcut_keeps_memo_current(self):
        # ! and U+0606 are both ON, the second in the RTL blocks: the shortcut keeps the maps but the edited row's RTL memo
        # (it holds no other RTL block char; row 0 is what makes the paragraph resolve) must follow the text
        def build(mid):
            return self.paragraph('אבגדה', f'ab{mid}cd')

        s = build('!')
        self.warm(s)
        memos = []
        for mid in ('؆', '!', '؆'):
            self.put(s, 2, 3, mid)
            self.ae(self.frame(s)[0], 0)
            f = build(mid)
            self.frame(f)
            memos.append(self.memo(f, 1))
            self.ae([self.memo(s, y) for y in range(2)], [self.memo(f, y) for y in range(2)])
            self.ae(self.state(s), self.state(f))
        self.ae(memos, [RTL, IMPLICIT, RTL])

    def test_shortcut_never_with_rows_above_view(self):
        # A history-only paragraph p0 p1 p2 p3, scrolled back so p0 is above the view. p0 is popped back to the screen and
        # pushed again as p0' with other classes (the last strong before the number starting p1 changes), p1..p3 pushed
        # again unchanged, so their ids, texts and signatures are as before and only p0 differs. The shortcut must not trust
        # the dirty on-view rows: p1's levels change.
        p0, p0b, p1, p2, p3 = 'abcd.', 'אבגד.', '12 34', 'אבגדה', 'ab cd'
        up = 3

        def build(first):
            s = self.screen(cols=5, lines=3, scrollback=10, pixel_scroll=False)
            s.draw(first + p1 + p2 + p3)
            parse_bytes(s, b'\r\nx1\r\nx2\r\nx3')
            s.scroll(up, True)
            self.ae((s.historybuf.count, s.scrolled_by), (4, up))
            return s

        def hist(s):
            return [str(s.historybuf.line(i)) for i in range(s.historybuf.count)]

        s = build(p0)
        self.warm(s)
        before, before_state = hist(s), self.state(s)
        self.ae(before, [p3, p2, p1, p0])
        s.scroll(up, False)
        self.frame(s)
        s.reverse_scroll(3, True)
        s.reverse_scroll(1, True)
        self.ae([str(s.line(y)) for y in range(3)], [p0, p1, p2])
        self.ae([s.linebuf.is_continued(y) for y in range(3)], [False, True, True])
        self.put(s, 1, 1, p0b[:4])
        s.cursor_position(3, 5)
        s.draw(p2[-1] + p3)  # p2's last cell again, then p3 as its continuation, which pushes p0'
        parse_bytes(s, b'\r\nx1\r\nx2\r\nx3')
        s.scroll(up, True)
        self.ae((s.historybuf.count, s.scrolled_by), (4, up))
        self.ae(hist(s), [p3, p2, p1, p0b], 'only the row above the view differs')
        self.assertLessEqual({0, 1, 2}, set(s.historybuf.dirty_lines()), 'the on-view rows are dirty again')
        self.assertGreaterEqual(self.frame(s)[0], 1)
        f = build(p0b)
        self.frame(f)
        self.ae(self.state(s), self.state(f))
        self.assertNotEqual(self.state(s), before_state, 'p1 must change levels, or the test cannot see the hole')

    def test_shortcut_n0_budget(self):
        # signatures exist only when the N0 budget did not fire: a same-class edit past it resolves, and so does an edit that
        # moves the paragraph across the threshold
        with self.subTest('past the budget'):
            s = self.screen(cols=200, lines=2, scrollback=0)
            s.draw('א(ב)' + '()' * 40)
            self.warm(s)
            for ch in 'גד':
                self.put(s, 1, 3, ch)
                self.ae(self.frame(s)[0], 1)
            f = self.screen(cols=200, lines=2, scrollback=0)
            f.draw('א(ד)' + '()' * 40)
            self.same_as(s, f)

        def crossing(name, text, col, new, final):
            with self.subTest(name):

                def build(t):
                    s = self.screen(cols=80, lines=1, scrollback=0)
                    s.draw(t)
                    return s

                s = build(text)
                self.warm(s)
                warmed = self.state(s)
                self.put(s, 1, col, new)
                self.ae(self.frame(s)[0], 1)
                self.same_as(s, build(final))
                self.assertNotEqual(self.state(s), warmed)

        pairs = 'א(ב)' + '()' * 33  # 70 chars, scan 1123 > 1120: the budget fires
        crossing('removing a bracket goes under', pairs, 69, '!', pairs[:68] + '!)')
        under = 'א(ב)' + '()' * 32  # 68 chars, scan 1057; an open bracket 66 from the last letter makes 1123 > 16 * 69
        crossing('adding a bracket goes over', under + '!', 69, '(', under + '(')

    def test_shortcut_n0_budget_dirty_row_without_brackets(self):
        # the budget zeroes the brackets of every row of the paragraph, so a same-class edit on a row without brackets would match
        # a signature written after the zeroing; the paragraph past the budget resolves instead
        s = self.screen(cols=40, lines=4, scrollback=0)
        s.draw('א' * 40 + '()' * 50)  # the letters of row 1 come first, so the brackets of rows 2-4 scan from the last of them
        self.warm(s)
        for ch in 'גד':
            self.put(s, 1, 3, ch)
            self.ae(self.frame(s)[0], 1)
        f = self.screen(cols=40, lines=4, scrollback=0)
        f.draw('א' * 2 + 'ד' + 'א' * 37 + '()' * 50)
        self.same_as(s, f)

    def test_shortcut_paused(self):
        # an edit before the pause shortcuts in the paused frame, one during the pause after the unpause
        rows = [self.PARA_ROW] * 4
        s = self.paragraph(*rows)
        self.warm(s)
        self.put(s, 2, 3, 'x')
        rows1 = list(rows)
        rows1[1] = 'ב1xגd'
        s.pause_rendering(True, 100000)
        resolves, buf = self.frame_buf(s)
        self.ae(resolves, 0)
        self.assert_same_as_fresh(s, buf, self.paragraph(*rows1), 'paused')
        s.pause_rendering(False)
        resolves, buf = self.frame_buf(s)
        self.ae(resolves, 0)
        self.assert_same_as_fresh(s, buf, self.paragraph(*rows1), 'unpaused')
        s.pause_rendering(True, 100000)
        self.put(s, 3, 2, '9')
        rows2 = list(rows1)
        rows2[2] = 'ב9aגd'
        resolves, buf = self.frame_buf(s)
        self.ae(resolves, 0)
        self.assert_same_as_fresh(s, buf, self.paragraph(*rows1), 'edit during the pause is not shown')
        s.pause_rendering(False)
        resolves, buf = self.frame_buf(s)
        self.ae(resolves, 0)
        self.assert_same_as_fresh(s, buf, self.paragraph(*rows2), 'edit after the unpause')

    def test_shortcut_mode_toggles(self):
        # with signatures stored (a same-class edit shortcut first), force_ltr and the trim flag make the next frame resolve
        rows = [self.PARA_ROW] * 4
        s = self.paragraph(*rows)
        self.warm(s)
        self.put(s, 1, 2, '5')
        self.ae(self.frame(s)[0], 0)
        rows[0] = 'ב5aגd'
        for force_ltr in (True, False):
            self.set_options({'force_ltr': force_ltr})
            s.test_bidi_check_mode()
            resolves = self.frame(s)[0]
        self.assertGreater(resolves, 0)
        self.same_as(s, self.paragraph(*rows))
        self.ae(self.frame(s)[0], 0)
        self.put(s, 1, 2, '6')
        self.ae(self.frame(s)[0], 0)
        s.test_bidi_trim(False)
        self.assertGreater(self.frame(s)[0], 0)
        f = self.paragraph(*rows[:0], 'ב6aגd', *rows[1:])
        f.test_bidi_trim(False)
        self.same_as(s, f)

    AUTO, SCP_RTL, SCP_LTR = b'\x1b[?2501h', b'\x1b[2 k', b'\x1b[1 k'
    RTL_WRAPPED = (((4, 3, 2, 1, 0), (4, 0, 1, 2, 3), None, None), ((1,) * 5, (2, 2, 2, 2, 1), None, None))  # (maps, levels)
    LTR_WRAPPED = (((0, 4, 3, 2, 1), None, None, None), ((0, 1, 1, 1, 1), None, None, None))

    def dscreen(self, *seqs, **kw):
        s = self.screen(**kw)
        for seq in seqs:
            parse_bytes(s, seq)
        return s

    def prtl(self, s, y):
        return s.test_bidi_paragraph_rtl(self.rr(s, y))

    def wrapped_rtl_screen(self, first='א'):
        s = self.dscreen(self.AUTO, cols=5, lines=4, scrollback=0)
        s.draw(first + 'בגדה')
        s.draw('ab 1')
        return s

    def upload_sprites(self, s, rows=(0, 1)):
        buf = test_screen_update_cell_data(s)
        return [self.sprites(gpu_row(s, buf, y)) for y in rows]

    def edit_first(self, s, ch, resolves, renders, rtl, want):
        # draws ch over the first cell, then one upload: row 0 is the only dirty row, the counts are those of the upload
        s.cursor_position(1, 1)
        s.draw(ch)
        self.ae(self.dirty(s), [0])
        before = s.test_bidi_counts()
        got = self.upload_sprites(s)
        counts = tuple(a - b for a, b in zip(s.test_bidi_counts(), before))
        self.ae((counts, [self.prtl(s, y) for y in (0, 1)], self.state(s)), ((resolves, renders), [rtl, rtl], want))
        return got

    def matches_fresh(self, s, got, f, rows=(0, 1)):
        # s, whose upload was got, has the maps, levels and sprites of a fresh screen f that only saw the final text
        want = self.upload_sprites(f, rows)
        self.ae(self.state(s), self.state(f))
        self.assertTrue(all(map(any, want)), 'a blank fresh row')
        self.ae(got, want)

    def test_rtl_paragraph_right_aligned(self):
        # 2501: the first strong R sets paragraph level 1, the blanks take it (L1): all level 1; no sequence: paragraph level 0
        s = self.dscreen(self.AUTO)
        draw_coloured(s, A)
        logical = [s.cell_style_idx(0, x) for x in range(3)]
        row = self.uploaded(s, 0)
        self.ae((self.lv(s, 0), self.vmaps(s)[0], self.prtl(s, 0)), ((1,) * 10, ID10[::-1], True))
        self.ae((styles(row[7:]), all(c.sprite_idx for c in row[7:]), self.sprites(row[:7])), (logical[::-1], True, [0] * 7))
        self.ae((s.cursor.x, s.test_bidi_visual_x(3, 0)), (3, 6))
        f = self.screen()
        draw_coloured(f, A)
        frow = self.uploaded(f, 0)
        self.ae((self.lv(f, 0), self.vmaps(f)[0], self.prtl(f, 0)), ((1, 1, 1) + (0,) * 7, (2, 1, 0) + ID10[3:], False))
        self.ae(row[9].sprite_idx, frow[2].sprite_idx)

    def test_rtl_paragraph_direction(self):
        # P2-P3: the first strong character (R, AL or L; not EN or AN) sets the direction; per row, per case, after a frame: (levels, map, rtl)
        rev, rev5 = ID10[::-1], (4, 3, 2, 1, 0)
        cases = (
            ('שלום!', 10, (True,), [((1,) * 10, rev, True)]),
            ('שלום!', 10, (False,), [((1, 1, 1, 1) + (0,) * 6, (3, 2, 1, 0) + ID10[4:], False)]),
            ('אבג abc', 10, (True,), [((1, 1, 1, 1, 2, 2, 2, 1, 1, 1), (9, 8, 7, 4, 5, 6, 3, 2, 1, 0), True)]),
            ('abc אבג', 10, (True, False), [((0, 0, 0, 0, 1, 1, 1, 0, 0, 0), (0, 1, 2, 3, 6, 5, 4, 7, 8, 9), False)]),
            ('12 אב ab', 10, (True,), [((2, 2, 1, 1, 1, 1, 2, 2, 1, 1), (9, 8, 6, 7, 5, 4, 3, 2, 0, 1), True)]),
            ('(12 aאבגדה', 5, (True, False), [(None, None, False), ((1,) * 5, rev5, False)]),
            ('אבגדהabcde', 5, (True,), [((1,) * 5, rev5, True), ((2,) * 5, (0, 1, 2, 3, 4), True)]),
            ('مرحبا!', 10, (True,), [((1,) * 10, rev, True)]),
            ('١٢٣ 45!', 10, (True, False), [((2, 2, 2) + (0,) * 7, ID10, False)]),
        )
        for text, cols, autos, rows in cases:
            for auto in autos:
                with self.subTest(text, auto=auto):
                    s = self.dscreen(*([self.AUTO] if auto else []), cols=cols)
                    s.draw(text)
                    self.frame(s)
                    self.ae([(self.lv(s, y), self.vmaps(s)[y], self.prtl(s, y)) for y in range(len(rows))], rows)

    def test_rtl_first_strong_edit(self):
        # writing the first strong character flips the paragraph; row 1 is not written but its levels change, so it must be reshaped
        s = self.wrapped_rtl_screen()
        self.ae(self.frame(s)[0], 1)
        self.warm(s)
        self.ae((self.state(s), [self.prtl(s, y) for y in (0, 1)]), (self.RTL_WRAPPED, [True, True]))
        for ch, rtl, want, resolves, renders, new_screen in (
            ('x', False, self.LTR_WRAPPED, 1, 2, False),
            ('א', True, self.RTL_WRAPPED, 1, 2, False),
            ('ב', True, self.RTL_WRAPPED, 0, 1, True),  # R for R: direction depends on classes alone, same-class skips resolve
        ):
            with self.subTest(ch):
                if new_screen:
                    s = self.wrapped_rtl_screen()
                    self.frame(s)
                    self.warm(s)
                got = self.edit_first(s, ch, resolves, renders, rtl, want)
                self.matches_fresh(s, got, self.wrapped_rtl_screen(ch))

    def test_rtl_paragraph_split_changes_direction(self):
        # clearing row 0's soft wrap makes row 1 its own paragraph with its own first strong character: unwritten, but reshaped
        def build(first, second):
            s = self.dscreen(self.AUTO, cols=5, lines=5)
            s.draw(first)
            s.draw(second)
            return s

        for first, second, rtl_after, map_after in (('abcde', 'אבג', True, (4, 3, 2, 1, 0)), ('אבגדה', 'abc', False, None)):
            with self.subTest(first):
                s = build(first, second)
                self.warm(s)
                self.assertIs(self.prtl(s, 1), not rtl_after)
                self.put(s, 1, 5, 'x')
                self.assertGreater(self.frame(s)[0], 0)
                got = self.upload_sprites(s, (1,))
                self.ae((self.vmaps(s)[1], self.prtl(s, 1)), (map_after, rtl_after))
                f = build(first, second)
                self.put(f, 1, 5, 'x')
                self.matches_fresh(s, got, f, (1,))
                self.warm(s)

    def test_rtl_paragraph_selection_and_mouse(self):
        s = self.dscreen(self.AUTO)
        s.draw(A)
        self.ae((self.selected_columns(s, 0, 2), self.prtl(s, 0)), ([7, 8, 9], True))
        # a click at visual 0, left of the text, is a trailing blank at paragraph level 1, logical 9: the half is flipped
        self.ae((s.test_bidi_logical_x(0, 0), s.test_bidi_is_rtl(9, 0), s.test_bidi_logical_x(9, 0)), (9, True, 0))

    def test_hebrew_first_stays_ltr(self):
        # A Hebrew-first line stays LTR-based when SCP 1 is the only control, 2501 is reset again or nothing is sent; mode 8 reset drops the maps
        ltr = (3, 2, 1, 0) + ID10[4:]
        for seqs, want in (
            ((self.SCP_LTR,), ltr),
            ((self.AUTO, b'\x1b[?2501l'), ltr),
            ((self.AUTO, b'\x1b[8l'), None),
            ((), ltr),
        ):
            with self.subTest(seqs=seqs):
                s = self.dscreen(*seqs)
                s.draw('שלום abc')
                self.frame(s)
                self.ae(self.vmaps(s)[0], want)
                self.ae([s.test_bidi_visual_x(x, 0) for x in range(s.columns)], list(want or ID10))
                self.assertIs(self.prtl(s, 0), False if want else None)

    def test_scp_rtl_latin_paragraph(self):
        # SCP 2 alone makes a Latin-only paragraph a candidate: level 2, right-aligned; with 2501 the weak base finds an LTR paragraph
        s = self.dscreen(self.SCP_RTL)
        s.draw('abc def')
        self.frame(s)
        self.ae((self.lv(s, 0), self.vmaps(s)[0], self.prtl(s, 0)), ((2,) * 7 + (1,) * 3, (9, 8, 7, 0, 1, 2, 3, 4, 5, 6), True))
        s = self.dscreen(self.AUTO, self.SCP_RTL)
        s.draw('abc def')
        self.frame(s)
        self.ae((self.vmaps(s)[0], self.prtl(s, 0)), (None, False))

    def test_rtl_line_direction_at_wrapped_space(self):
        # the space ending row 0 is reset to the paragraph level by L1 in the line's reorder, so it needs the resolved direction
        s = self.dscreen(self.SCP_RTL, cols=5)
        s.draw('abcd efg')
        self.frame(s)
        self.ae((self.lv(s, 0), self.lv(s, 1)), ((2, 2, 2, 2, 1), (2, 2, 2, 1, 1)))
        self.ae(self.vmaps(s)[:2], ((4, 0, 1, 2, 3), (4, 3, 0, 1, 2)))
        self.ae([self.prtl(s, y) for y in (0, 1)], [True, True])

    def blank_row(self, *seqs):
        s = self.dscreen(*seqs)
        s.draw('x')
        parse_bytes(s, b'\r\x1b[2K')  # the row keeps the record the write stamped
        self.frame(s)
        return s

    def test_forced_rtl_blank_rows(self):
        # a paragraph of one blank row has nothing for FriBidi: under an RTL base it is forced to level 1 and reversed, under LTR it has no map
        want = (ID10[::-1], (1,) * 10, True)
        for seqs in ((self.SCP_RTL,), (self.AUTO, self.SCP_RTL)):
            with self.subTest(seqs=seqs):
                s = self.blank_row(*seqs)
                self.ae((self.vmaps(s)[0], self.lv(s, 0), self.prtl(s, 0)), want)
        s = self.blank_row(self.AUTO)
        self.ae((self.vmaps(s)[0], self.prtl(s, 0)), (None, False))
        # a blank last row of a wrapped paragraph is reversed too
        s = self.dscreen(self.SCP_RTL)
        s.draw('א' * 10)
        s.draw('ב')
        s.cursor_position(2, 1)
        s.erase_characters(1)
        self.assertTrue(s.linebuf.is_continued(1))
        self.frame(s)
        self.ae([(self.vmaps(s)[y], self.lv(s, y), self.prtl(s, y)) for y in (0, 1)], [want, want])

    def test_rtl_tail_with_wide_and_multicell(self):
        # the blank tail goes first, reversed, ahead of the reordered rest; trimming it changes neither maps nor levels, as no fed isolate is involved
        def wide(s):
            s.draw('אב中')
            return 6

        def multicell(s):
            s.draw('אב')
            draw_multicell(s, 'x', width=2)
            return 6

        for name, build in (('wide', wide), ('multicell', multicell)):
            with self.subTest(name):
                screens = []
                for trim in (True, False):
                    s = self.dscreen(self.SCP_RTL)
                    s.test_bidi_trim(trim)
                    k = build(s)
                    self.warm(s)
                    screens.append(s)
                self.ae(self.state(screens[0]), self.state(screens[1]))
                v2l = self.vmaps(screens[0])[0]
                self.assertIsNotNone(v2l)
                self.ae((v2l[:k], self.prtl(screens[0], 0)), (tuple(range(9, 9 - k, -1)), True))

    def blank_led(self, *seqs, text='abc אב', cols=10, lines=5, y=0, **kw):
        # a screen whose row y is a blank stamped row soft-wrapped into row y + 1 (ECH and EL clear the wrap bit, so the
        # wide char that does not fit in the last column sets it)
        s = self.dscreen(*seqs, cols=cols, lines=lines, **kw)
        s.cursor_position(y + 1, 1)
        s.draw('x')
        parse_bytes(s, b'\r\x1b[2K')
        s.cursor_position(y + 1, cols)
        s.draw('中' + text)
        self.assertTrue(s.linebuf.is_continued(y + 1))
        self.ae(str(s.linebuf.line(y)).strip(), '')
        self.assertTrue(s.test_bidi_line_record(y)[0])
        return s

    def fresh_after(self, build, op):
        # maps, levels and paragraph direction of a fresh screen replaying op, rendered once
        f = build()
        op(f)
        self.frame(f)
        return self.state(f), [self.prtl(f, y) for y in range(f.lines)]

    def assert_matches_replay(self, s, build, op):
        self.ae((self.state(s), [self.prtl(s, y) for y in range(s.lines)]), self.fresh_after(build, op))

    def test_direction_only_change_misses_hit(self):
        # CSI 2 SP k re-derives the record of the blank row leading the paragraph without dirtying anything: the paragraph must not hit
        def op(s):
            parse_bytes(s, self.SCP_RTL)

        with self.subTest('blank lead'):
            s = self.blank_led()
            self.warm(s)
            self.ae(self.frame(s)[0], 0)
            before = self.state(s)
            op(s)
            self.ae(self.dirty(s), [])
            self.ae(self.frame(s)[0], 1)
            self.assertNotEqual(self.state(s), before)
            self.assert_matches_replay(s, self.blank_led, op)
        with self.subTest('single blank row'):
            s = self.dscreen()
            s.draw('x')
            parse_bytes(s, b'\r\x1b[2K')
            self.warm(s)
            self.ae(self.prtl(s, 0), False)
            op(s)
            self.ae(self.frame(s)[0], 1)
            self.ae((self.prtl(s, 0), self.vmaps(s)[0]), (True, ID10[::-1]))

    def test_direction_only_change_misses_shortcut(self):
        # a same-class edit of a row below the blank lead takes the shortcut only when the lead's record is unchanged
        def build():
            return self.blank_led(text='אבג abc')

        def edit(s):
            self.put(s, 2, 8, 'd')  # b over d, after the wide char

        def edit_rtl(s):
            edit(s)
            parse_bytes(s, self.SCP_RTL)

        def latin():
            return self.blank_led(self.SCP_RTL, text='abc def')

        def latin_edit(s):
            self.put(s, 2, 7, 'x')

        # build, op, resolves of the frame after the op
        for name, make, op, resolves in (
            ('direction changes', build, edit_rtl, 1),
            ('no change', build, edit, 0),
            ('latin lead-only candidate', latin, latin_edit, 0),
        ):
            with self.subTest(name):
                s = make()
                self.warm(s)
                op(s)
                self.ae(self.dirty(s), [1])
                self.ae(self.frame(s)[0], resolves)
                self.assert_matches_replay(s, make, op)

    def test_direction_only_change_misses_after_follow(self):
        # the cache follows a full-screen index by renaming rows; the lead's record changing in the same frame must still miss
        def build():
            return self.blank_led(y=2, scrollback=20)

        def index(s):
            s.cursor_position(5, 1)
            parse_bytes(s, b'\x1bD')

        def index_rtl(s):
            s.cursor_position(5, 1)
            parse_bytes(s, b'\x1bD' + self.SCP_RTL)

        for name, make, op, resolves in (
            ('direction changes', build, index_rtl, 1),
            ('no change', build, index, 0),
            ('rtl base', lambda: self.blank_led(self.SCP_RTL, y=2, scrollback=20), index, 0),
        ):
            with self.subTest(name):
                s = make()
                self.warm(s)
                shifts = self.shifts(s)
                op(s)
                got = self.frame(s)[0]
                self.ae((got, self.shifts(s) - shifts), (resolves, 1))
                self.assert_matches_replay(s, make, op)

    def test_paused_frame_keeps_snapshot_direction(self):
        # a direction change sent during a pause shows once the pause ends, and so does a line written under it
        def op(s):
            parse_bytes(s, self.SCP_RTL)

        def op_write(s):
            op(s)
            self.put(s, 5, 1, 'שלום')

        for name, do in (('change', op), ('write', op_write)):
            with self.subTest(name):
                s = self.blank_led()
                self.warm(s)
                before = self.state(s)
                s.pause_rendering(True, 1000)
                do(s)
                self.frame(s)
                self.ae(self.state(s), before)
                s.pause_rendering(False)
                self.frame(s)
                self.assert_matches_replay(s, self.blank_led, do)
                self.assertNotEqual(self.state(s), before)

    def test_direction_toggle_keeps_records(self):
        # records are stamped, not live: toggling the controls re-resolves no paragraph whose first row has text
        text = A + ' abc'
        toggles = (self.AUTO, b'\x1b[?2501l', self.SCP_RTL, self.SCP_LTR, b'\x1b[0 k', self.AUTO, self.SCP_RTL)
        for first in ((), (self.AUTO,), (self.SCP_RTL,)):
            with self.subTest(first=first):
                s = self.dscreen(*first)
                s.draw(text)
                self.warm(s)
                want = self.state(s), [self.prtl(s, y) for y in range(s.lines)]
                for seq in toggles:
                    parse_bytes(s, seq)
                    self.ae(self.frame(s), (0, 0), seq)
                    self.ae((self.state(s), [self.prtl(s, y) for y in range(s.lines)]), want, seq)
        # an overwrite keeps the old base
        s = self.dscreen()
        s.draw('abc ' + A)
        self.warm(s)
        parse_bytes(s, self.AUTO + self.SCP_RTL)
        self.put(s, 1, 1, 'ב')
        self.frame(s)
        self.ae((self.prtl(s, 0), self.vmaps(s)[0]), (False, (0, 1, 2, 3, 6, 5, 4) + ID10[7:]))
        # a cleared line takes the new base, as does a line inserted and rewritten
        for name, clear in (('ED', b'\x1b[2J\x1b[H'), ('IL', b'\x1b[H\x1b[L')):
            with self.subTest(name):

                def op(s):
                    parse_bytes(s, self.AUTO + self.SCP_RTL + clear)
                    s.draw(text)

                def build():
                    s = self.dscreen()
                    s.draw(text)
                    return s

                s = build()
                self.warm(s)
                op(s)
                self.frame(s)
                self.ae(self.prtl(s, 0), True)
                self.assert_matches_replay(s, build, op)
        # EL with text left on the row keeps its record
        s = self.dscreen()
        s.draw(text)
        self.warm(s)
        want = self.vmaps(s)[0]
        parse_bytes(s, self.AUTO + self.SCP_RTL + b'\x1b[1;6H\x1b[K')
        self.frame(s)
        self.ae((self.prtl(s, 0), self.vmaps(s)[0]), (False, want))

    def test_rtl_paragraph_cache_triggers(self):
        # cache triggers on RTL paragraphs (auto on): each equals a fresh screen. Maps are right-aligned
        rev, rev5, mix = ID10[::-1], (4, 3, 2, 1, 0), (9, 8, 7, 4, 5, 6, 3, 2, 1, 0)
        long = 'אבגדהוזחטיכלמ'
        para = long + 'נ'
        base = (rev, mix, rev, rev, None)

        def scr(text, **kw):
            s = self.dscreen(self.AUTO, **kw)
            parse_bytes(s, text.encode())
            return s

        def basic():  # a Hebrew-first paragraph soft-wrapped onto rows 2-3, whose first row ends in a blank (map start)
            return scr('אבג\r\nאבג abc\r\nאבגדהוזחט אבג\x1b[1;1H')

        def alt():
            return scr('\x1b[?1049h\x1b[8h\x1b[?2501hאבג\r\nאבג abc\x1b[4;1H' + long + '\x1b[1;1H')

        def edge(text, **kw):
            s = scr(text, cols=5, scrollback=10, **kw)
            self.prerender(s)
            return s

        def edge_top():
            s = edge(para + '\r\nx1\r\nx2\r\nx3\r\nx4\r\n', pixel_scroll=False)
            s.scroll(3, True)
            return s

        def pause(s):
            s.pause_rendering(True, 1000)
            self.frame(s)
            s.pause_rendering(False)

        cases = (
            ('dirty row', basic, lambda s: parse_bytes(s, b'\x1b[4;4H\xd7\x93'), base, None),
            # a full-height index follows the cached paragraphs: no resolve
            ('index', basic, lambda s: parse_bytes(s, b'\x1b[5;1H\x1bD'), (mix, rev, rev, None, None), 0),
            ('reverse index', basic, lambda s: parse_bytes(s, b'\x1b[1;1H\x1bM'), (None, rev, mix, rev, rev), None),
            ('insert then delete line', alt, lambda s: parse_bytes(s, b'\x1b[L\x1b[M'), (rev, mix, None, rev, rev), None),
            ('view scroll, bottom edge', lambda: edge('x0\r\nx1\r\nx2\r\n' + para), lambda s: s.scroll(1, True), (None, None, None, rev5, rev5), None),
            ('view scroll, top edge', edge_top, lambda s: s.scroll(1, False), (rev5, rev5, None, None, None), None),
            ('resize with rewrap', lambda: scr(long), lambda s: s.resize(5, 13), (tuple(range(12, -1, -1)), None, None, None, None), None),
            # the paused frame and the live one after it both hit the warm cache
            ('pause', basic, pause, base, 0),
        )
        for name, build, trigger, expect, resolves in cases:
            with self.subTest(name):
                screens = []
                self.trigger_case(lambda: screens.append(build()) or screens[-1], trigger, expect, fresh=not name.startswith('view'), resolves=resolves)
                self.assertTrue(all(any(self.prtl(s, y) for y in range(s.lines)) for s in screens), 'no RTL row')
        s = basic()
        self.warm(s)
        self.ae((self.vmaps(s), [self.prtl(s, y) for y in range(4)]), (base, [True] * 4))

    def test_auto_first_strong_beats_scp(self):
        # with 2501 on, SCP 2 only sets the fallback: the first strong character decides, and a line with none takes it
        for seqs, row, want in (
            ((self.AUTO, self.SCP_RTL), 'abc ' + A, False),
            ((self.AUTO, self.SCP_RTL), A + ' abc', True),
            ((self.AUTO, self.SCP_RTL), '12 34', True),
            ((self.AUTO,), '12 34', False),
        ):
            with self.subTest(seqs=seqs, row=row):
                s = self.dscreen(*seqs)
                s.draw(row)
                self.frame(s)
                self.ae(self.prtl(s, 0), want)
        # no strong character gives base RTL: the ENs get level 2, N1 makes the space between them R, and L1 gives the trailing blanks the paragraph level
        s = self.dscreen(self.AUTO, self.SCP_RTL)
        s.draw('12 34')
        self.frame(s)
        self.ae((self.lv(s, 0), self.vmaps(s)[0]), ((2, 2, 1, 2, 2, 1, 1, 1, 1, 1), (9, 8, 7, 6, 5, 3, 4, 2, 0, 1)))
        s = self.dscreen(self.AUTO)
        s.draw('12 34')
        self.frame(s)
        self.ae(self.vmaps(s)[0], None)

    def test_control_change_reresolves(self):
        # records are stamped: a control change (CSI 8 l included) re-resolves no text-led paragraph and moves no map; a cleared and rewritten screen takes it
        rows = ['abc ' + A, A + ' abc']

        def build():
            s = self.dscreen(self.AUTO)
            self.fill(s, rows)
            return s

        s = build()
        self.warm(s)
        maps = self.state(s)
        for seq in (b'\x1b[8l', b'\x1b[8h', self.SCP_RTL, b'\x1b[0 k', self.AUTO):
            with self.subTest(seq=seq):
                parse_bytes(s, seq)
                self.ae(self.frame(s)[0], 0)
                self.ae(self.state(s), maps)

        # the cleared screen is written again under the new base
        def trigger(s):
            parse_bytes(s, b'\x1b[2J\x1b[H' + self.SCP_RTL)
            self.fill(s, rows)

        self.trigger_case(build, trigger)
        # the alternate screen (47 keeps its content) starts with its own controls
        s = build()
        parse_bytes(s, b'\x1b[?47h\x1b[8h')
        self.fill(s, rows[::-1])
        self.frame(s)
        self.warm(s)
        for seq in (b'\x1b[?47l', b'\x1b[?47h'):
            with self.subTest(seq=seq):
                parse_bytes(s, seq)
                self.assertGreater(self.frame(s)[0], 0)
                self.warm(s)

    DIFF_GROUPS = ('0123456789', 'אבגדהוזחט', 'abcdefghij', '()[]')
    DIFF_POOL = 'אבגדהוזחט' * 3 + 'abcdefghij' * 3 + '0123456789' * 2 + '  ' * 2 + '()!.'

    # extra (kinds, weights) of diff_op for streaming at the bottom, about 30% of the ops on top of the default ones
    STREAM_MIX = (('stream', 'streamk', 'su'), (20, 13, 10))

    # direction ops (SCP, 2501, ED, EL): the paragraph direction changes under a screen full of text
    DIRECTION_MIX = (('scp', 'auto', 'ed', 'el'), (8, 5, 3, 5))

    # leading bidi controls at the start of a row, drawn with the text or as a separate draw
    MARK_MIX = (('mark', 'markw'), (6, 4))

    def diff_op(self, rng, s, paused, first=False, mix=None):
        # (name, function of a screen): the arguments are fixed here so that a fresh screen replays the same operation
        L, C = s.lines, s.columns
        y, x = rng.randrange(L), rng.randrange(C)
        n = rng.randrange(1, 4)
        # a paused or scrolled-back screen gets back to the live bottom soon, where the shortcut applies
        if paused and rng.random() < 0.4:
            return 'unpause', lambda s: s.pause_rendering(False)
        if s.scrolled_by and rng.random() < 0.4:
            return 'fwd', lambda s: s.scroll(s.historybuf.count, False)
        kinds = ('same', 'change', 'wrap', 'wide', 'osc66', 'scale2', 'il', 'dl', 'index', 'region', 'back', 'fwd', 'pause', 'ech', 'ich', 'crlf')
        weights = (60, 6, 8, 2, 2, 1, 2, 2, 3, 2, 3, 1, 3, 2, 2, 2)
        if mix:
            kinds, weights = kinds + mix[0], weights + mix[1]
        kind = 'wrap' if first else rng.choices(kinds, weights)[0]

        def at(text):
            return lambda s: (s.cursor_position(y + 1, x + 1), s.draw(text))

        def csi(seq):
            return lambda s: (s.cursor_position(y + 1, 1), parse_bytes(s, seq))

        if kind in ('stream', 'streamk'):
            texts = [''.join(rng.choices(self.DIFF_POOL, k=rng.randrange(3, 2 * C))) for _ in range(1 if kind == 'stream' else rng.randrange(2, 4))]
            data = ''.join(text + '\r\n' for text in texts).encode()
            return kind, lambda s: (s.cursor_position(L, 1), parse_bytes(s, data))
        if kind == 'scp':
            seq = rng.choice((self.SCP_RTL, self.SCP_LTR, b'\x1b[0 k'))
            return 'scp', lambda s: parse_bytes(s, seq)
        if kind == 'auto':
            seq = rng.choice((self.AUTO, b'\x1b[?2501l'))
            return 'auto', lambda s: parse_bytes(s, seq)
        if kind == 'ed':
            return 'ed', csi(rng.choice((b'\x1b[J', b'\x1b[1J', b'\x1b[2J')))
        if kind == 'el':
            return 'el', csi(rng.choice((b'\x1b[K', b'\x1b[1K', b'\x1b[2K')))
        if kind in ('mark', 'markw'):
            mark = rng.choice((LRM, RLM, ALM))
            text = ''.join(rng.choices(self.DIFF_POOL, k=rng.randrange(0, C)))
            if kind == 'mark':
                return kind, lambda s: (s.cursor_position(y + 1, 1), s.draw(mark + text))
            return kind, lambda s: (s.cursor_position(y + 1, 1), s.draw(mark), parse_bytes(s, b'\x1b[m'), s.draw(text))
        if kind == 'su':
            seq = f'\x1b[{rng.randrange(1, L + 1)}S'.encode()
            return 'su', lambda s: parse_bytes(s, seq)
        if kind == 'same':
            # a cell that is the only one of its character in its neighbourhood (not half a wide cell), in a class group
            cells = []
            for yy in range(L):
                line = s.line(yy)
                for xx in range(1, C - 1):
                    ch = line[xx]
                    if len(ch) == 1 and line[xx - 1] != ch != line[xx + 1] and any(ch in g for g in self.DIFF_GROUPS):
                        cells.append((yy, xx, ch))
            if not cells:
                return 'noop', lambda s: None
            y, x, ch = rng.choice(cells)
            group = next(g for g in self.DIFF_GROUPS if ch in g)
            return 'same', at(ch if group == self.DIFF_GROUPS[3] else rng.choice(group))
        if kind == 'change':
            return 'change', at(rng.choice(self.DIFF_POOL))
        if kind == 'wrap':
            text = ''.join(rng.choices(self.DIFF_POOL, k=rng.randrange(12, 36)))
            return 'wrap', lambda s: (s.cursor_position(y + 1, 1), s.draw(text))
        if kind == 'wide':
            return 'wide', at('中')
        if kind == 'osc66':
            ch = rng.choice(self.DIFF_POOL)
            return 'osc66', lambda s: (s.cursor_position(y + 1, x + 1), parse_bytes(s, f'\x1b]66;w=2;{ch}\x07'.encode()))
        if kind == 'scale2':
            return 'scale2', lambda s: (s.cursor_position(y + 1, x + 1), draw_multicell(s, 'x', scale=2))
        if kind == 'il':
            return 'il', csi(b'\x1b[L')
        if kind == 'dl':
            return 'dl', csi(b'\x1b[M')
        if kind == 'index':
            return 'index', lambda s: (s.cursor_position(L, 1), parse_bytes(s, b'\x1bD'))
        if kind == 'region':
            return 'region', lambda s: (parse_bytes(s, b'\x1b[2;4r'), s.cursor_position(4, 1), parse_bytes(s, b'\x1bD\x1b[r'))
        if kind == 'back':
            return 'back', lambda s: s.scroll(n, True)
        if kind == 'fwd':
            return 'fwd', lambda s: s.scroll(n, False)
        if kind == 'pause':
            return ('unpause', lambda s: s.pause_rendering(False)) if paused else ('pause', lambda s: s.pause_rendering(True, 1000000))
        if kind == 'ech':
            return 'ech', lambda s: (s.cursor_position(y + 1, x + 1), s.erase_characters(n))
        if kind == 'ich':
            return 'ich', lambda s: (s.cursor_position(y + 1, x + 1), s.insert_characters(n))
        return 'crlf', lambda s: (s.cursor_position(y + 1, x + 1), parse_bytes(s, b'\r\n'))

    def differential(self, seed, count, mix=None, follows=None, rtl_bases=None, conversions=None):
        # follows (a list) gets a 1 for each frame that followed a scroll; rtl_bases gets a 1 for each live frame in which a paragraph
        # starting on screen has a row with an RTL direction although none of its rows holds a Hebrew letter, a direction that only a control can have given it
        rng = random.Random(seed)

        def new():
            return self.screen(cols=8, lines=5, scrollback=20)

        s, applied, shortcuts, paused, frozen = new(), [], 0, False, 0
        for i in range(count):
            name, op = self.diff_op(rng, s, paused, first=i < 3, mix=mix)
            scrolled = s.scrolled_by
            before = [tuple(s.test_bidi_line_record(r)) for r in range(s.lines)] if conversions is not None and name in ('mark', 'markw') else None
            op(s)
            if before is not None:
                conversions.append(sum(1 for r, (_, _, auto) in enumerate(before) if auto and (rec := tuple(s.test_bidi_line_record(r)))[0] and not rec[2]))
            if name in ('pause', 'unpause'):
                paused = name == 'pause'
                frozen = len(applied)
            else:
                applied.append(op)
                # scrolling the view unpauses, so the frame after it is live again; output that keeps a scrolled-back view anchored changes
                # scrolled_by without unpausing
                paused = paused and not (name in ('back', 'fwd') and s.scrolled_by != scrolled)
            dirty = s.linebuf.dirty_lines() if not paused and not s.scrolled_by else []
            shifted = s.test_bidi_shifts() if follows is not None else 0
            resolves = self.frame(s)[0]
            if follows is not None:
                follows.append(int(s.test_bidi_shifts() > shifted))
            # a dirty row inside a paragraph with maps that nothing resolved again: the shortcut kept them
            if not resolves and any(self.vmaps(s)[y] is not None for y in dirty):
                shortcuts += 1
            # a paused frame shows the snapshot taken at the pause, whatever the live screen does meanwhile
            f = new()
            for o in applied[:frozen] if paused else applied:
                o(f)
            self.frame(f)
            exempt = self.snapshot_top_rows(f) if paused else 0
            (vs, ls), (vf, lf) = self.state(s), self.state(f)
            ps, pf = [self.prtl(s, y) for y in range(s.lines)], [self.prtl(f, y) for y in range(f.lines)]
            self.ae((vs[exempt:], ls[exempt:], ps[exempt:]), (vf[exempt:], lf[exempt:], pf[exempt:]), f'seed {seed}, op {i} ({name})')
            if rtl_bases is not None:
                paras = []
                for y in range(s.lines):
                    if y and s.linebuf.is_continued(y):
                        paras[-1].append(y)
                    else:
                        paras.append([y])
                if s.historybuf.count and s.historybuf.endswith_wrap():
                    paras = paras[1:]
                live = not paused and not s.scrolled_by
                rtl_bases.append(
                    int(
                        live
                        and any(any(ps[y] for y in para) and not any('\u0590' <= c <= '\u05ff' for y in para for c in str(s.linebuf.line(y))) for para in paras)
                    )
                )
        return shortcuts

    def snapshot_top_rows(self, f):
        # screen rows of the top paragraph of a snapshot paused at the view of f, which a snapshot resolves without the lines above it
        sb, count, lines = f.scrolled_by, f.historybuf.count, f.lines

        def exists(v):
            return sb - 1 - v < count if v < sb else v - sb < lines

        def continued(v):
            # virtual row v is screen row v at the live bottom; the rows above it are history, and HistoryBuf.is_continued counts
            # from the oldest line where line() counts from the newest
            if v < sb:
                return f.historybuf.is_continued(count - sb + v)
            return f.linebuf.is_continued(v - sb) if v > sb else bool(count) and f.historybuf.endswith_wrap()

        # the snapshot starts one row above the screen with pixel scroll, and an empty row there has no paragraph
        top = -self.rr(f, 0)
        if not exists(top):
            top += 1
        if not continued(top):
            return 0
        end = top + 1
        while end < lines and exists(end) and continued(end):
            end += 1
        return max(end, 0)

    def test_shortcut_random_differential(self):
        # after every frame the maps and levels equal those of a fresh screen replaying the operations so far and rendering once
        seeds = int(os.environ.get('KITTY_BIDI_DIFF_SEEDS', 3))
        count = int(os.environ.get('KITTY_BIDI_DIFF_OPS', 150))
        counts = [self.differential(seed, count) for seed in range(seeds)]
        # summed because a single seed's count varies
        self.assertGreaterEqual(sum(counts), seeds * count // 15, counts)

    def test_follow_random_differential(self):
        # streaming ops among the shortcut differential's: the maps equal a fresh replay's after every frame
        seeds = int(os.environ.get('KITTY_BIDI_DIFF_SEEDS', 3))
        count = int(os.environ.get('KITTY_BIDI_DIFF_OPS', 150))
        follows = []
        for seed in range(seeds):
            self.differential(seed, count, mix=self.STREAM_MIX, follows=follows)
        # summed so that the test cannot pass without ever following
        self.assertGreaterEqual(sum(follows), seeds * count // 20, follows)

    def test_direction_random_differential(self):
        # direction ops, alone and among the streaming ones: the maps, levels and paragraph directions equal a fresh replay's after every frame
        seeds = int(os.environ.get('KITTY_BIDI_DIFF_SEEDS', 3))
        count = int(os.environ.get('KITTY_BIDI_DIFF_OPS', 150))
        both = tuple(a + b for a, b in zip(self.DIRECTION_MIX, self.STREAM_MIX))
        rtl_bases = []
        counts = [self.differential(seed, count, mix=self.DIRECTION_MIX, rtl_bases=rtl_bases) for seed in range(seeds)]
        self.assertGreaterEqual(sum(counts), seeds * count // 20, counts)
        for seed in range(seeds):
            self.differential(seed, count, mix=both, rtl_bases=rtl_bases)
        # summed so that the test cannot pass without ever running under an RTL base
        self.assertGreater(sum(rtl_bases), 0, rtl_bases)

    def test_mark_random_differential(self):
        # leading marks among the direction (and streaming) ops: the maps, levels and paragraph directions equal a fresh replay's after every frame
        seeds = int(os.environ.get('KITTY_BIDI_DIFF_SEEDS', 3))
        count = int(os.environ.get('KITTY_BIDI_DIFF_OPS', 150))
        rtl_bases, conversions = [], []
        for base in (self.DIRECTION_MIX, tuple(a + b for a, b in zip(self.DIRECTION_MIX, self.STREAM_MIX))):
            mix = tuple(a + b for a, b in zip(base, self.MARK_MIX))
            for seed in range(seeds):
                self.differential(seed, count, mix=mix, rtl_bases=rtl_bases, conversions=conversions)
        # summed so that the test cannot pass without a mark converting an auto row or an RTL base
        self.assertGreater(sum(conversions), 0, conversions)
        self.assertGreater(sum(rtl_bases), 0, rtl_bases)

    def marked(self, mark=RLM, text='abc def', split=False, **kw):
        s = self.dscreen(self.AUTO, **kw)
        if split:
            s.draw(mark)
            s.draw(text)
        else:
            s.draw(mark + text)
        return s

    def rec(self, s, y):
        return tuple(s.test_bidi_line_record(y))

    def test_leading_mark_paragraph(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.marked(RLM, 'abc def', split)
                self.ae((self.refresh(s, 0), self.prtl(s, 0), self.rec(s, 0)[1:]), (RTL_PLAIN, True, (True, False)))
                s = self.marked(LRM, A + ' x', split)
                self.ae((self.refresh(s, 0), self.prtl(s, 0)), ((2, 1, 0) + ID10[3:], False))
                s = self.marked('', A + ' x', split)
                self.ae((self.refresh(s, 0), self.prtl(s, 0), self.rec(s, 0)[2]), (ID10[::-1], True, True))
                s = self.marked(ALM, '12 34', split)
                f = self.dscreen(self.SCP_RTL)
                f.draw('12 34')
                self.frame(s)
                self.frame(f)
                self.ae((self.prtl(s, 0), self.state(s)), (True, self.state(f)))

    def test_leading_mark_erase_in_line(self):
        for split in (False, True):
            for el in ('0K', '1K', '2K'):
                with self.subTest(split=split, el=el):
                    s = self.marked(split=split)
                    self.ae(self.refresh(s, 0), RTL_PLAIN)
                    s.cursor_position(1, 1)
                    parse_bytes(s, f'\x1b[{el}'.encode())
                    self.ae(self.rec(s, 0)[1:], (True, False))
                    s.cursor_position(1, 1)
                    s.draw('abc def')
                    self.ae((self.refresh(s, 0), self.prtl(s, 0)), (RTL_PLAIN, True))
            for clear in ('\x1b[2J', '\x1b[L'):
                with self.subTest(split=split, clear=clear):
                    s = self.marked(split=split)
                    self.ae(self.refresh(s, 0), RTL_PLAIN)
                    s.cursor_position(1, 1)
                    parse_bytes(s, clear.encode())
                    s.cursor_position(1, 1)
                    s.draw('abc def')
                    v2l = self.refresh(s, 0)
                    self.ae((self.prtl(s, 0), v2l, self.rec(s, 0)[2]), (False, None, True))

    def test_leading_mark_history_and_reflow(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.marked(split=split, cols=10, lines=3)
                s.cursor_position(3, 1)
                s.index()
                self.ae(s.historybuf.count, 1)
                s.scroll(1, True)
                self.ae((self.refresh(s, 0), self.prtl(s, 0)), (RTL_PLAIN, True))
                s = self.marked(split=split, cols=10, lines=3)
                s.resize(3, 5)
                self.frame(s)
                f = self.marked(split=split, cols=5, lines=3)
                self.frame(f)
                self.ae(
                    (self.rec(s, 0)[1:], self.prtl(s, 0), self.prtl(s, 1), self.state(s)),
                    ((True, False), True, True, self.state(f)),
                )
                s.resize(3, 10)
                self.ae((self.refresh(s, 0), self.prtl(s, 0)), (RTL_PLAIN, True))

    def test_leading_mark_only_change_reresolves(self):
        for split in (False, True):
            with self.subTest(split=split):
                marked_f, plain_f = self.marked(split=split), self.marked('', split=split)
                self.frame(marked_f)
                self.frame(plain_f)
                marked_state, plain_state = self.state(marked_f), self.state(plain_f)
                self.assertNotEqual(marked_state, plain_state)
                for clear in ('\x1b[2J', '\x1b[L'):
                    s = self.marked(split=split)
                    self.warm(s)
                    ops = []
                    for mark in ('', RLM, ''):
                        s.cursor_position(1, 1)
                        parse_bytes(s, clear.encode())
                        s.cursor_position(1, 1)
                        if split:
                            s.draw(mark)
                            s.draw('abc def')
                        else:
                            s.draw(mark + 'abc def')
                        ops.append(mark)
                        resolves = self.frame(s)[0]
                        f = self.marked(split=split)
                        for m in ops:
                            f.cursor_position(1, 1)
                            parse_bytes(f, clear.encode())
                            f.cursor_position(1, 1)
                            if split:
                                f.draw(m)
                                f.draw('abc def')
                            else:
                                f.draw(m + 'abc def')
                        self.frame(f)
                        with self.subTest(clear=clear, mark=mark, step=len(ops)):
                            self.ae(self.state(s), self.state(f))
                            if clear == '\x1b[2J':
                                self.ae(self.state(s), marked_state if mark else plain_state)
                            if mark:
                                self.assertGreater(resolves, 0)
                        self.warm(s)

    def test_leading_mark_converts_clean_row(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.dscreen(self.AUTO)
                s.draw('abc def')
                self.warm(s)
                self.ae(self.refresh(s, 0), None)
                s.cursor_position(1, 1)
                s.draw(RLM)
                resolves = self.frame(s)[0]
                f = self.marked(split=split)
                self.frame(f)
                self.assertGreaterEqual(resolves, 1)
                self.ae(self.state(s), self.state(f))
                s = self.dscreen(self.AUTO)
                s.draw(NSI)
                self.warm(s)
                self.ae(s.test_bidi_line_record(0)[0], RTL)
                s.cursor_position(1, 1)
                s.draw(LRM)
                self.ae(self.rec(s, 0), (RTL, False, False))
                resolves = self.frame(s)[0]
                f = self.marked(LRM, NSI, split)
                self.frame(f)
                self.assertGreaterEqual(resolves, 1)
                self.ae((self.prtl(s, 0), self.state(s)), (False, self.state(f)))

    def test_leading_mark_continuation_rows(self):
        s = self.dscreen(self.AUTO, cols=5)
        s.draw('abcdefg')
        s.cursor_position(2, 1)
        s.draw(RLM)
        self.frame(s)
        self.ae(([self.rec(s, y)[2] for y in (0, 1)], [self.prtl(s, y) for y in (0, 1)]), ([True, True], [False, False]))

        def build(warm):
            s = self.dscreen(self.AUTO, cols=5, lines=2, scrollback=5)
            s.cursor_position(2, 1)
            s.draw('abcdefghijklm')
            self.assertGreaterEqual(s.historybuf.count, 1)
            self.assertTrue(s.historybuf.endswith_wrap())
            if warm:
                self.warm(s)
            s.cursor_position(1, 1)
            s.draw(RLM)
            return s

        s, f = build(True), build(False)
        self.ae((self.rec(s, 0)[1:], self.rec(s, 1)[2]), ((True, False), True))
        self.frame(s)
        self.frame(f)
        self.ae(([self.prtl(s, y) for y in (0, 1)], self.state(s)), ([False, False], self.state(f)))

    def test_leading_mark_overlay(self):
        s = self.dscreen(self.AUTO)
        s.draw('abc def')
        self.frame(s)
        s.test_draw_overlay_line(RLM + 'xy', 0, 0)
        self.frame(s)
        f = self.dscreen(self.AUTO)
        f.draw('abc def')
        self.frame(f)
        self.ae((s.test_bidi_line_record(0)[2], self.state(s)), (True, self.state(f)))

    def test_leading_mark_force_ltr(self):
        s = self.marked(force_ltr=True)
        self.ae((self.rec(s, 0)[1:], self.refresh(s, 0)), ((True, False), None))

    # Following a full-height index: the cache keeps the paragraphs of the lines that moved
    FOLLOW_ROWS = ['דהו', 'zxc', 'זחט', 'lkj']  # screen rows 0..3 of a 10x5 screen, the bottom row blank
    FOLLOW_TEXTS = ('אבג', 'xyz', 'אב12', 'abc', 'אבגדה', 'qrs', 'ב(12)', 'tuv', 'אבגד.', 'a b')

    def shifts(self, s):
        return s.test_bidi_shifts()

    def stream(self, s, *texts):
        # one line per text, from the bottom row: each CRLF scrolls the screen by one
        s.cursor_position(s.lines, 1)
        parse_bytes(s, ''.join(t + '\r\n' for t in texts).encode())

    def follow_screen(self, **opts):
        return self.filled(self.FOLLOW_ROWS, scrollback=20, **opts)

    def streamed_state(self, build, batches, op=None):
        # maps and levels of a fresh screen replaying the batches (and op after them), rendered once
        f = build()
        for texts in batches:
            self.stream(f, *texts)
        if op:
            op(f)
        self.frame(f)
        return self.state(f)

    def test_follow_stream_bottom(self):
        # one line per frame at the bottom: only the new line's paragraph resolves, whatever the pixel scroll row holds
        for pixel_scroll in (False, True):
            with self.subTest(pixel_scroll=pixel_scroll):

                def build():
                    return self.follow_screen(pixel_scroll=pixel_scroll)

                s = build()
                self.warm(s)
                done, shifted = [], self.shifts(s)
                for text in self.FOLLOW_TEXTS:
                    self.stream(s, text)
                    done.append((text,))
                    shifted += 1
                    self.ae(self.frame(s)[0], int(any('א' <= c <= 'ת' for c in text)), text)
                    self.ae(self.shifts(s), shifted, text)
                    self.ae(self.state(s), self.streamed_state(build, done), text)

    def test_follow_multi_line_frames(self):
        # k lines in one frame follow when k < lines; k >= lines replaces the whole screen and drops as before
        rtl = ['אבג', 'אב12', 'אבגדה', 'ב(12)', 'אבגד.', 'דהו', 'זחט']

        def build():
            return self.follow_screen(pixel_scroll=False)

        # (op, whether it follows, the RTL paragraphs on view after it, which all resolve when nothing is kept)
        cases = (
            ('3 lines', lambda s: self.stream(s, *rtl[:3]), True, 3),  # the 3 new ones; the Latin row moved up
            # lines - 1: only the old bottom row stays (now row 0, holding the first new line); no old content survives
            ('4 lines', lambda s: self.stream(s, *rtl[:4]), True, 4),
            ('5 lines', lambda s: self.stream(s, *rtl[:5]), False, 4),  # lines 2..5 on rows 0..3
            ('CSI 5 S', lambda s: parse_bytes(s, b'\x1b[5S'), False, 0),  # a blank screen
            ('7 lines', lambda s: self.stream(s, *rtl), False, 4),  # lines 4..7 on rows 0..3
        )
        for name, op, follows, resolves in cases:
            with self.subTest(name):
                s = build()
                self.warm(s)
                before = self.shifts(s)
                op(s)
                self.ae(self.frame(s)[0], resolves)
                self.ae(self.shifts(s), before + follows)
                self.ae(self.state(s), self.streamed_state(build, [], op))

    def test_follow_paused(self):
        # a paused frame follows the scrolls up to the pause only, an unpaused one the rest
        a, b = 'אבג', 'אב12'

        def build():
            return self.follow_screen()

        # lines streamed only while paused: the paused frame is the cached one
        s = build()
        self.warm(s)
        before = self.shifts(s)
        s.pause_rendering(True, 1000000)
        self.stream(s, a, b)
        self.ae((self.frame(s)[0], self.shifts(s)), (0, before))
        f = build()
        self.frame(f)
        exempt = self.snapshot_top_rows(f)
        self.ae(tuple(x[exempt:] for x in self.state(s)), tuple(x[exempt:] for x in self.state(f)))
        s.pause_rendering(False)
        self.ae((self.frame(s)[0], self.shifts(s)), (2, before + 1))
        self.ae(self.state(s), self.streamed_state(build, [(a, b)]))

        # a line before the pause is followed by the paused frame, a line after it by the unpaused one
        s = build()
        self.warm(s)
        before = self.shifts(s)
        self.stream(s, a)
        s.pause_rendering(True, 1000000)
        self.stream(s, b)
        self.ae((self.frame(s)[0], self.shifts(s)), (1, before + 1))
        up_to_pause = self.follow_screen()
        self.stream(up_to_pause, a)
        self.frame(up_to_pause)
        exempt = self.snapshot_top_rows(up_to_pause)
        self.ae(tuple(x[exempt:] for x in self.state(s)), tuple(x[exempt:] for x in self.state(up_to_pause)))
        s.pause_rendering(False)
        self.ae((self.frame(s)[0], self.shifts(s)), (1, before + 2))
        self.ae(self.state(s), self.streamed_state(build, [(a,), (b,)]))

    def test_follow_paused_scrolled_back(self):
        # a paused frame of a scrolled-back view follows with the view's offset: its renamed rows are looked up at the paused view
        lines = ['אבג', 'abאב', 'אב12', 'xyz', 'אבגדה', 'a', 'אבג abc', 'ב(12)', 'אב אב', 'ab', 'אבגדה1', 'xx']
        a, b = 'אב', 'אבג'

        def build():
            s = self.screen(cols=5, lines=6, scrollback=20, pixel_scroll=False)
            for text in lines:
                parse_bytes(s, text.encode() + b'\r\n')
            self.frame(s)
            s.scroll(2, True)
            return s

        def fresh(view, *texts):
            f = build()
            for text in texts:
                self.stream(f, text)
            f.scroll(view - f.scrolled_by, True)
            self.frame(f)
            return f

        s = build()
        self.warm(s)
        before = self.shifts(s)
        self.stream(s, a)
        self.ae(s.scrolled_by, 3)
        s.pause_rendering(True, 1000000)
        self.stream(s, b)
        self.ae(s.scrolled_by, 4)
        self.ae((self.frame(s)[0], self.shifts(s)), (0, before + 1))
        f = fresh(3, a)
        exempt = self.snapshot_top_rows(f)
        self.ae(tuple(x[exempt:] for x in self.state(s)), tuple(x[exempt:] for x in self.state(f)))
        s.pause_rendering(False)
        self.ae((self.frame(s)[0], self.shifts(s)), (1, before + 2))  # the paragraph of the line streamed while paused
        self.ae(self.state(s), self.state(fresh(4, a, b)))

    def test_follow_scrolled_back(self):
        # output while scrolled back anchors the view on its lines: the screen paragraphs in view are renamed and need no resolve
        lines = ['אבג', 'abאב', 'אב12', 'xyz', 'אבגדה', 'a', 'אבג abc', 'ב(12)', 'אב אב', 'ab', 'אבגדה1', 'xx']

        def build():
            s = self.screen(cols=5, lines=6, scrollback=20, pixel_scroll=False)
            for text in lines:
                parse_bytes(s, text.encode() + b'\r\n')
            return s

        s = build()
        self.frame(s)
        s.scroll(2, True)
        self.warm(s)
        streamed = ['אב', 'cd', 'אבג']
        before = self.shifts(s)
        for i, text in enumerate(streamed):
            self.stream(s, text)
            self.ae(s.scrolled_by, 3 + i)
            self.ae(self.frame(s)[0], 0, text)
            self.ae(self.shifts(s), before + i + 1, text)
        f = build()
        self.frame(f)
        for text in streamed:
            self.stream(f, text)
        f.scroll(s.scrolled_by, True)
        self.frame(f)
        self.ae(self.state(s), self.state(f))
        self.assertTrue(any(self.vmaps(s)))

    def test_follow_alt_screen(self):
        # the alternate screen follows too: its pushed rows are discarded rather than renamed into the history
        def build():
            s = self.screen(cols=10, lines=5, scrollback=20)
            parse_bytes(s, b'\x1b[?1049h\x1b[8h')
            self.fill(s, self.FOLLOW_ROWS)
            return s

        s = build()
        self.assertTrue(s.is_using_alternate_linebuf())
        self.warm(s)
        done, shifted = [], self.shifts(s)
        for text in ('אבג', 'אב12', 'אבגדה', 'ב(12)', 'אבגד.'):
            self.stream(s, text)
            done.append((text,))
            shifted += 1
            self.ae(self.frame(s)[0], 1, text)
            self.ae(self.shifts(s), shifted, text)
            self.ae(self.state(s), self.streamed_state(build, done), text)
        self.ae(s.historybuf.count, 0)

    def test_follow_straddler_across_top(self):
        # a paragraph pushed into the history by its first rows keeps its entries only if every pushed row still matches them
        rtl, latin = 'אבגד.12 34abcde', 'abcdefghijklmno'  # 15 cells: 3 rows of 5
        wide = 'abcde' + 'abcא ' + ' 12 x' + 'xyzab'  # 20 cells: 4 rows of 5, the second pushed row decides the levels

        def build(lines, text):
            s = self.screen(cols=5, lines=lines, scrollback=20, pixel_scroll=False)
            s.draw(text)
            s.cursor_position(lines, 1)
            return s

        # (name, lines, paragraph, edit (row, column, text) before the scroll, lines streamed, resolves)
        cases = (
            ('unchanged', 4, rtl, None, 1, 0),
            ('same-class edit', 4, rtl, (1, 1, 'ב'), 1, 0),
            ('other-class edit', 4, rtl, (1, 1, 'a'), 1, 1),
            ('unresolved paragraph gains RTL', 4, latin, (1, 1, 'א'), 1, 1),
            ('second pushed row unchanged', 5, wide, (2, 4, 'א'), 2, 0),
            ('second pushed row same-class edit', 5, wide, (2, 4, 'ב'), 2, 0),
            ('second pushed row other-class edit', 5, wide, (2, 4, 'b'), 2, 0),  # the paragraph is left-to-right: nothing to resolve
        )
        for name, lines, text, edit, k, resolves in cases:
            with self.subTest(name):

                def scroll(s):
                    if edit:
                        self.put(s, *edit)
                    self.stream(s, *['xy'] * k)

                s = build(lines, text)
                continued = [s.linebuf.is_continued(y) for y in range(1, lines)]
                self.assertEqual(continued, [True] * (len(text) // 5 - 1) + [False] * (lines - len(text) // 5))
                self.warm(s)
                before = self.shifts(s)
                scroll(s)
                self.ae((s.historybuf.count, s.historybuf.endswith_wrap(), s.linebuf.is_continued(1)), (k, True, len(text) // 5 > k + 1))
                self.ae((self.frame(s)[0], self.shifts(s)), (resolves, before + 1))
                f = build(lines, text)
                scroll(f)
                self.frame(f)
                self.ae(self.state(s), self.state(f))

    def test_follow_cleared_history(self):
        # CSI ? 3 J empties the history without a generation bump, so the pushed rows of a later scroll can be gone from it.
        # It also blanks the screen rows, so the straddling paragraph is dropped on the signature check, not kept.
        def build():
            s = self.screen(cols=5, lines=4, scrollback=20, pixel_scroll=False)
            s.draw('אבגד.12 34abcde')
            s.cursor_position(4, 1)
            return s

        for frame_between in (True, False):
            with self.subTest(frame_between=frame_between):
                s = build()
                self.warm(s)
                before = self.shifts(s)
                self.stream(s, 'xy')
                if frame_between:
                    self.frame(s)
                    self.ae((s.historybuf.count, self.shifts(s)), (1, before + 1))
                parse_bytes(s, b'\x1b[?3J')
                self.ae(s.historybuf.count, 0)
                self.stream(s, 'zw')
                # without the frame in between, 2 rows were pushed and the older one is gone
                self.ae(s.historybuf.count, 1)
                self.frame(s)
                self.ae(self.shifts(s), before + 1 + frame_between)
                f = build()
                self.stream(f, 'xy')
                parse_bytes(f, b'\x1b[?3J')
                self.stream(f, 'zw')
                self.frame(f)
                self.ae(self.state(s), self.state(f))

    def test_follow_straddler_kept_across_clear_scrollback(self):
        # clear_scrollback empties the history but keeps the screen rows, so the straddling paragraph's rows still match their
        # entries, one of which has a para_first naming a cleared history line. Nothing resolves, as in a fresh replay: the
        # rows left on screen start in the middle of the paragraph, and the kept entries show as renders a replay would redo.
        def build():
            s = self.screen(cols=5, lines=4, scrollback=20, pixel_scroll=False)
            s.draw('אבגד.12 34abcde')
            s.cursor_position(4, 1)
            return s

        for frame_between in (True, False):
            with self.subTest(frame_between=frame_between):
                s = build()
                self.warm(s)
                before = self.shifts(s)
                self.stream(s, 'xy')
                if frame_between:
                    self.frame(s)
                    self.ae((s.historybuf.count, self.shifts(s)), (1, before + 1))
                s.clear_scrollback()
                self.ae((s.historybuf.count, str(s.line(0))), (0, '12 34'))
                self.stream(s, 'zw')
                # without the frame in between, 2 rows were pushed and the older one is gone
                self.ae(s.historybuf.count, 1)
                resolves, renders = self.frame(s)
                # the scroll before the clear is followed in its own frame, the one after it in this frame
                self.ae((resolves, self.shifts(s)), (0, before + 1 + frame_between))
                f = build()
                self.stream(f, 'xy')
                f.clear_scrollback()
                self.stream(f, 'zw')
                fresh_resolves, fresh_renders = self.frame(f)
                self.ae(fresh_resolves, 0)
                self.assertLess(renders, fresh_renders)
                self.ae(self.state(s), self.state(f))

    def test_follow_other_moves_drop(self):
        # every other way to move or clear rows still drops the screen entries, even with a followable scroll in the same interval
        def build():
            return self.filled(['דהו', 'זחט', 'אבג', 'דהז'], scrollback=20, pixel_scroll=False)

        def build_alt():
            s = self.screen(scrollback=20, pixel_scroll=False)
            parse_bytes(s, b'\x1b[?1049h\x1b[8h')
            self.fill(s, ['דהו', 'זחט', 'אבג', 'דהז'])
            return s

        def build_prompt():
            s = self.screen(cols=10, lines=4, scrollback=30, pixel_scroll=False)
            s.draw('before\r\n')
            parse_bytes(s, b'\x1b]133;A\a')
            s.draw('$ p1\r\n')
            parse_bytes(s, b'\x1b]133;C\a')
            s.draw('o0 אב\r\nאבג o1\r\n')
            parse_bytes(s, b'\x1b]133;A\a')
            s.draw('$ l0\r\nאבגדהוזחטיכלמ\r\nabc אבג\r\nl3\r\nl4')
            return s

        def at(y, seq):
            return lambda s: (s.cursor_position(y, 1), parse_bytes(s, seq))

        def scroll_until_prompt(s):
            s.cursor_position(2, 1)
            s.scroll_until_cursor_prompt(False)
            # one scroll that pushes nothing after the streamed one that does: fewer pushes than scrolls, 2 scrolls < 5 lines
            self.ae(s.historybuf.count, 1)

        def erase_prompt(s):
            count = s.historybuf.count
            self.assertTrue(s.erase_last_command())
            self.assertLess(s.historybuf.count, count)

        # the streamed line is 'אבד' on row 3; the RTL paragraphs on view after the op all resolve, as the entries are dropped
        cases = (
            ('region index', build, lambda s: (parse_bytes(s, b'\x1b[2;4r'), at(4, b'\x1bD\x1b[r')(s)), 3),  # rows 1..3 scroll: 3 left
            # the alternate screen pushes nothing, so only the counters tell the region scroll from a full-height one
            ('region index on the alternate screen', build_alt, lambda s: (parse_bytes(s, b'\x1b[2;4r'), at(4, b'\x1bD\x1b[r')(s)), 3),
            ('insert line', build, at(2, b'\x1b[L'), 4),
            ('delete line', build, at(2, b'\x1b[M'), 3),
            ('reverse index', build, at(1, b'\x1bM'), 4),  # the bottom row is lost
            ('erase to end of display', build, at(2, b'\x1b[J'), 1),  # only row 0 is left
            ('scroll until prompt', build, scroll_until_prompt, 3),
            ('prompt erase', build_prompt, erase_prompt, 2),  # the streamed line overwrote 'l4': 'abc אבג' and 'אבד' on view
            ('erase into scrollback', build, at(1, b'\x1b[22J'), 0),  # a blank screen
        )
        for name, make, op, resolves in cases:
            with self.subTest(name):
                s = make()
                self.warm(s)
                before = self.shifts(s)
                self.stream(s, 'אבד')
                op(s)
                got = self.frame(s)[0]
                self.ae(self.shifts(s), before)
                f = make()
                self.stream(f, 'אבד')
                op(f)
                self.frame(f)
                self.ae(self.state(s), self.state(f))
                self.ae(got, resolves)

    def test_follow_after_reset(self):
        # RIS unstamps both buffers, so the alternate buffer's scrolls are not counted until it is stamped again; then
        # full-height scrolls follow and a region scroll drops the entries, which on the alternate screen (nothing is
        # pushed) only the counters decide
        def build():
            s = self.follow_screen(pixel_scroll=False)
            self.warm(s)
            parse_bytes(s, b'\x1bc\x1b[?1049h')
            self.stream(s, 'xyz', 'abc', 'qrs')
            self.frame(s)
            parse_bytes(s, b'\x1b[8h')
            self.fill(s, self.FOLLOW_ROWS)
            return s

        s = build()
        self.assertTrue(s.is_using_alternate_linebuf())
        self.warm(s)
        done, shifted = [], self.shifts(s)
        for text in self.FOLLOW_TEXTS[:4]:
            self.stream(s, text)
            done.append((text,))
            shifted += 1
            self.ae(self.frame(s)[0], int(any('א' <= c <= 'ת' for c in text)), text)
            self.ae(self.shifts(s), shifted, text)
            self.ae(self.state(s), self.streamed_state(build, done), text)

        def region_index(s):
            parse_bytes(s, b'\x1b[2;4r')
            s.cursor_position(4, 1)
            parse_bytes(s, b'\x1bD\x1b[r')

        region_index(s)
        got = self.frame(s)[0]
        self.ae(self.shifts(s), shifted)
        f = build()
        for texts in done:
            self.stream(f, *texts)
        region_index(f)
        self.ae(got, self.frame(f)[0])
        self.ae(self.state(s), self.state(f))
        self.ae(s.historybuf.count, 0)

    def test_follow_counter_wrap(self):
        # the scroll counter wraps from UINT64_MAX to 0 between two followed frames
        def build():
            return self.follow_screen()

        s = build()
        s.test_bidi_set_scrolls(0xFFFFFFFFFFFFFFFF)
        self.warm(s)
        done, shifted = [], self.shifts(s)
        for text in ('אבג', 'אב12', 'אבגדה'):
            self.stream(s, text)
            done.append((text,))
            shifted += 1
            self.ae((self.frame(s)[0], self.shifts(s)), (1, shifted), text)
            self.ae(self.state(s), self.streamed_state(build, done), text)

    # Cursor and IME

    def test_cursor_visual_in_bidi_rows(self):
        # The beam goes to the left edge of an RTL run it follows; block, underline and hollow stay on the cell's own column unless blank
        L, R = False, True
        # (name, auto, text, levels prefix, v2l, ((visual_x args), want)..., [(x, beam column, mirrored, block/underline/hollow column)])
        cases = (
            ('rtl letters, blank after', R, A, None, ID10[::-1], (((0, 0), 9), ((3, 0), 6)), [(0, 9, R, 9), (3, 6, R, 6)]),
            ('latin after hebrew in rtl', R, 'אaב', (1, 2, 1), ID10[::-1], (), [(1, 8, R), (2, 7, R)]),
            ('latin after latin in rtl', R, 'אab', (1, 2, 2), None, (((2, 0), 8),), [(2, 8, L)]),
            ('ltr paragraph', L, 'abcאב', None, (0, 1, 2, 4, 3) + ID10[5:], (), [(0, 0, L), (1, 1, L), (2, 2, L), (3, 4, R, 4), (4, 3, R)]),
            ('wide first, n1 level 1', R, '😀' + A, (1, 1), None, (((0, 0, 2), 8),), [(0, 9, R, 8)]),
            ('wide before the blank', R, 'אב😀', (1, 1, 1, 1), None, (((2, 0, 2), 6),), [(4, 5, R, 5)]),
            ('blank after hebrew run', L, 'ls שלום', None, (0, 1, 2, 6, 5, 4, 3) + ID10[7:], (((6, 0), 3),), [(7, 2, R, 2)]),
            ('blank after latin', L, 'ls abc', None, None, (), [(6, 6, L, 6)]),
            ('blank after digits in rtl run', L, A + ' 12', (1, 1, 1, 1, 2, 2), None, (((6, 0), 6),), [(6, 2, L, 2)]),
            ('run at the left edge', L, A, None, (2, 1, 0) + ID10[3:], (), [(3, 0, L, 0)]),
            ('block over latin after run', L, 'שלום' + 'd', None, (3, 2, 1, 0, 4) + ID10[5:], (), [(4, 0, L, 4)]),
            ('block over latin after run, prefix', L, 'ab' + 'שלום' + 'd', None, (0, 1, 5, 4, 3, 2, 6) + ID10[7:], (), [(6, 1, R, 6)]),
            ('block over blank after run', L, 'ab' + 'שלום' + ' d', None, None, (), [(6, 1, R, 1)]),
            ('pending wrap, hebrew last', L, 'abcdef' + 'אבגד', None, (0, 1, 2, 3, 4, 5, 9, 8, 7, 6), (), [(10, 5, R, 5)]),
            ('pending wrap, latin last', L, 'אבגד' + 'abcdef', None, None, (), [(10, 10, L, 10)]),
            ('pending wrap, no rtl', L, 'abcdefghij', None, None, (), [(10, 10, L, 10)]),
        )
        for name, auto, text, levels, v2l, vx, cursors in cases:
            with self.subTest(name):
                s = self.screen()
                if auto:
                    parse_bytes(s, b'\x1b[?2501h')
                s.draw(text)
                got = self.refresh(s, 0)
                if v2l:  # None: not asserted
                    self.ae(got, v2l)
                if levels:
                    self.ae(self.levels(s, 0)[: len(levels)], levels)
                self.ae([s.test_bidi_visual_x(*args) for args, _ in vx], [want for _, want in vx])
                for x, beam, mirrored, *other in cursors:
                    beam_got, *others = [s.test_cursor_visual(x, 0, k) for k in (CURSOR_BEAM, CURSOR_BLOCK, CURSOR_UNDERLINE, CURSOR_HOLLOW)]
                    self.ae(beam_got, (beam, mirrored))
                    if other:
                        self.ae(others, [(other[0], False)] * 3)

    def test_cursor_after_rtl_char_paused_pixel_scroll(self):
        # The paused snapshot has a leading row with pixel scroll: a block read from row y instead of y + 1 sees the row above
        s = self.screen(pixel_scroll=True)
        parse_bytes(s, f'abcdefgh\r\nls {A}'.encode())
        self.frame(s)
        self.ae(s.test_cursor_render(CURSOR_BLOCK), (2, False))
        parse_bytes(s, b'\x1b[?2026h')
        self.frame(s)
        self.ae((s.test_cursor_render(CURSOR_BLOCK), s.test_cursor_visual(6, 1, CURSOR_BLOCK)), ((2, False), (2, False)))

    def test_beam_mirror_flag(self):
        # The beam is mirrored exactly when it sits at the left edge of a cell it follows; the other shapes never are
        F, T = False, True
        ltr_v2l = MIXED_V2L
        rtl_v2l = (9, 8, 7, 6, 5, 4, 3, 0, 1, 2)
        # (name, rtl base, row, erased row, v2l, ((x, (beam column, mirrored), (block column, mirrored))..))
        cases = (
            # x=7: the cursor gap, the empty cells join the run
            ('ltr base', F, 0, F, ltr_v2l, ((0, (0, F), (0, F)), (3, (5, T), (5, F)), (4, (4, T), (4, F)), (6, (2, T), (2, F)), (7, (2, T), (2, F)))),
            ('rtl base', T, 0, F, rtl_v2l, ((0, (7, F), (7, F)), (3, (6, T), (6, F)), (4, (5, T), (5, F)), (6, (3, T), (3, F)), (7, (2, T), (2, F)))),
            ('rtl base, erased row', T, 1, T, ID10[::-1], ((0, (9, T), (9, F)),)),
        )

        def build(rtl, row, erased, **opts):
            s = self.screen(**opts)
            if rtl:
                parse_bytes(s, b'\x1b[2 k')
            s.draw(MIXED)
            if erased:
                s.cursor_position(row + 1, 1)
                parse_bytes(s, b'ab\x1b[2K')
            return s

        for name, rtl, row, erased, v2l, xs in cases:
            s = build(rtl, row, erased)
            self.ae(self.refresh(s, row), v2l, name)
            for variant, opts in (('live', {}), ('pixel scroll', {'pixel_scroll': True}), ('no pixel scroll', {'pixel_scroll': False}), ('paused', {})):
                with self.subTest(name, variant=variant):
                    s = build(rtl, row, erased, **opts)
                    for x, beam, block in xs:
                        s.cursor_position(row + 1, x + 1)
                        test_screen_update_cell_data(s)
                        if variant == 'paused':
                            s.pause_rendering(True, 1000)
                            test_screen_update_cell_data(s)
                        self.ae(s.test_cursor_visual(x, row, CURSOR_BEAM), beam)
                        self.ae(s.test_cursor_render(CURSOR_BEAM), beam)
                        self.ae(s.test_cursor_visual(x, row, CURSOR_BLOCK), block)
                        self.ae(s.test_cursor_render(CURSOR_BLOCK), block)
                        for shape, want in ((CURSOR_BEAM, beam), (CURSOR_BLOCK, block)):
                            s.test_cursor_render_info(shape)
                            test_screen_update_cell_data(s)
                            self.ae(s.test_cursor_render_info(), want)
                        for shape in (CURSOR_UNDERLINE, CURSOR_HOLLOW):
                            self.ae(s.test_cursor_visual(x, row, shape), block)
                        if variant == 'paused':
                            s.pause_rendering(False)
        # no map means no mirroring: force_ltr, and a screen that never sent CSI 8 h. Options are global, so each screen is made just before use.
        for name, make in (
            ('force_ltr', lambda: self.screen(force_ltr=True)),
            ('no CSI 8 h', lambda: self.create_screen(cols=10, lines=5, scrollback=5, options={})),
        ):
            with self.subTest(name):
                s = make()
                s.draw(MIXED)
                test_screen_update_cell_data(s)
                for x in (0, 3, 4, 6, 7):
                    for shape in (CURSOR_BEAM, CURSOR_BLOCK):
                        self.ae(s.test_cursor_visual(x, 0, shape), (x, False))

    def assert_cursor_at(self, s, x, beam, block, msg=''):
        self.assert_cursor_at_row(s, 0, x, beam, block, msg)

    def assert_cursor_at_row(self, s, y, x, beam, block, msg=''):
        # the drawn cursor with it at logical x of screen row y: beam is (column, mirrored), the other shapes share the block's column and are never mirrored
        if (s.cursor.y, s.cursor.x) != (y, x):
            s.cursor_position(y + 1, x + 1)
        self.ae((s.cursor.y, s.cursor.x), (y, x), msg)
        test_screen_update_cell_data(s)
        self.ae((s.test_cursor_visual(x, y, CURSOR_BEAM), s.test_cursor_render(CURSOR_BEAM)), (beam, beam), msg)
        for shape in (CURSOR_BLOCK, CURSOR_UNDERLINE, CURSOR_HOLLOW):
            self.ae(s.test_cursor_visual(x, y, shape), (block, False), msg)
        for shape in (CURSOR_BLOCK, CURSOR_UNDERLINE):
            self.ae(s.test_cursor_render(shape), (block, False), msg)
        self.ae(s.test_cursor_render_x(), block, msg)

    def test_cursor_follows_run(self):
        # At the end of the text the cursor sits where the next char of the run it types into goes: left of an RTL run in an
        # LTR paragraph, right of an LTR run in an RTL one. Typed spaces after a letter are skipped to find that run.
        F, T = False, True
        AUTO, FORCE_RTL = b'\x1b[?2501h', b'\x1b[2 k'
        # (name, escape sequences, text, columns, cursor x, beam (column, mirrored), column of the other shapes)
        cases = (
            # LTR paragraph: the typed space after the run is at the paragraph level, the cursor still follows the run
            ('hebrew and space', b'', 'ab אני ', 10, 7, (2, T), 2),
            ('hebrew and two spaces', b'', 'ab אני  ', 10, 8, (2, T), 2),
            ('hebrew at the left edge', b'', 'אני ', 10, 4, (0, F), 0),
            # RTL paragraph, LTR run: the right edge of abc, where the next letter goes
            ('latin after hebrew', AUTO, 'שלום abc', 10, 8, (5, F), 5),
            ('latin and space after hebrew', AUTO, 'שלום abc ', 10, 9, (5, F), 5),
            ('latin at the pending wrap', AUTO, 'שלום' + 'abcdef', 10, 10, (6, F), 6),
            # drawn at the right edge of abc, the last column, the beam on that edge
            ('clamped to the last column', FORCE_RTL, 'abc', 10, 3, (9, T), 9),
            # unchanged
            ('hebrew', b'', 'ab אני', 10, 6, (2, T), 2),
            ('inside the run', b'', 'ab אני', 10, 4, (4, T), 4),
            ('after latin text', b'', 'ab אני abc  ', 14, 12, (12, F), 12),
            ('blank between words', b'', 'אני   abc', 10, 4, (4, F), 4),
            ('after an empty cell', b'', 'ab אני', 10, 8, (2, T), 2),  # the empty cells join the run
            ('latin only', b'', 'abc  ', 10, 5, (5, F), 5),
            ('rtl paragraph, hebrew', AUTO, 'שלום', 10, 4, (5, T), 5),
            ('rtl paragraph, hebrew and space', AUTO, 'שלום ', 10, 5, (4, T), 4),
            # after a number or neutral the next char's place is what the probe char gives the gap
            ('digits and space after hebrew', b'', 'שלום 123 ', 12, 9, (0, F), 0),  # the space joins the run
            ('rtl paragraph, digits and space', AUTO, 'שלום 123 ', 12, 9, (2, T), 2),
            ('wide latin after hebrew', AUTO, 'שלום 中', 10, 7, (5, F), 5),
            # the PDI closes the isolate, so the next char is outside it
            ('closed isolate', b'', 'a⁧12 34⁩', 10, 6, (6, F), 6),
            ('latin digits in an rli and space', b'', 'x⁧abc 12 ', 16, 8, (8, F), 8),
            ('percent after digits and space', b'', 'שלום 50% ', 12, 9, (0, F), 0),  # the space joins the run
            ('latin digits after latin, rtl', AUTO, 'שלום abc 123 ', 16, 13, (11, F), 11),  # the space joins the level 2 run
            ('digits and space after hebrew, rtl in rli', AUTO, 'ש⁦123 ', 12, 5, (11, F), 11),
            ('degree after digit and space', b'', 'שלום 5° ', 12, 8, (0, F), 0),
            ('shekel after digit and space', b'', 'שלום 5₪ ', 12, 8, (0, F), 0),
            ('arabic digits and space', b'', 'مرحبا ١٢ ', 14, 9, (0, F), 0),
            ('closed isolate and space', b'', 'a⁧12 34⁩ ', 10, 7, (7, F), 7),
            ('latin in an rli and space', b'', 'x⁧abc ', 10, 5, (5, F), 5),
            # neutrals before the typed space wait for the next char: they join the run with it
            ('comma and space after hebrew', b'', 'שלום, ', 12, 6, (0, F), 0),
            ('full stop and space after hebrew', b'', 'שלום. ', 12, 6, (0, F), 0),
            ('comma and space after digits', b'', 'שלום 12, ', 14, 9, (0, F), 0),
            ('comma and space after latin', b'', 'abc, ', 10, 5, (5, F), 5),
        )
        for name, seqs, text, cols, x, beam, block in cases:
            with self.subTest(name):
                s = self.screen(cols=cols)
                parse_bytes(s, seqs)
                s.draw(text)
                self.assert_cursor_at(s, x, beam, block)

    PUA = b'\xf3\xb0\x88\xba'  # U+F023A, bidi class L
    # the outer terminal's stream for a prompt: tmux ends the plain prompt's row with EL; with an FSI it writes the space
    # too, then moves the cursor with CUP, and fish appends the typed text
    PROMPT = {'A': PUA + b'\x1b[1;3H', 'B': PUA + b' \xe2\x81\xa8\x1b[1;3H'}

    def replayed(self, prompt, text, cols=40, prefix=None):
        s = self.screen(cols=cols, lines=2)
        parse_bytes(s, (self.AUTO if prefix is None else prefix) + self.PROMPT[prompt] + text)
        test_screen_update_cell_data(s)
        return s

    def test_prompt_isolate_replay(self):
        # the bytes tmux sent to the outer terminal for a prompt starting with U+F023A, A without and B with an FSI after it
        tail = tuple(range(31, 40))
        cases = (
            # (text, A: v2l and levels, B: v2l and levels): A is today's layout, with the ? and english at the right
            (
                'מה המצב יחתולים?',
                ((0, 1) + tuple(range(16, 1, -1)) + (17,) + tuple(range(18, 40)), (0, 0) + (1,) * 15 + (0,)),
                ((0, 1) + tuple(range(17, 1, -1)) + tuple(range(18, 40)), (0, 0) + (1,) * 16),
            ),
            (
                'מילה ראשונה english מילה שניה',
                (
                    (0, 1) + tuple(range(12, 1, -1)) + tuple(range(13, 22)) + tuple(range(30, 21, -1)) + tail,
                    (0, 0) + (1,) * 11 + (0,) * 9 + (1,) * 9,
                ),
                (
                    (0, 1, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21) + tuple(range(14, 21)) + (13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2) + tail,
                    (0, 0) + (1,) * 12 + (2,) * 7 + (1,) * 10,
                ),
            ),
            # the cursor: the typed space is at the paragraph level, and in B, with the FSI, it joins the word's run
            # the typed space joins the run in A as in B
            ('אני ', ((0, 1, 5, 4, 3, 2), (0, 0, 1, 1, 1, 1)), ((0, 1, 5, 4, 3, 2), (0, 0, 1, 1, 1, 1))),
        )
        for text, *layouts in cases:
            for prompt, (v2l, levels) in zip('AB', layouts):
                with self.subTest(text=text, prompt=prompt):
                    s = self.replayed(prompt, text.encode())
                    self.ae(self.v2l(s, 0)[: len(v2l)], v2l)
                    self.ae(self.levels(s, 0)[: len(levels)], levels)

    def test_prompt_isolate_replay_cursor(self):
        # after the typed word and its space the cursor is left of the word, where the next letter goes
        typed = 'אני '.encode()
        # fish writes the first letter of each word twice and the space as space, backspace, space
        idiom = b'\xd7\x90\x08\xd7\x90\xd7\xa0\xd7\x99 \x08 '
        for prompt in 'AB':
            for name, data in (('plain', typed), ('idiom', idiom)):
                with self.subTest(prompt=prompt, typed=name):
                    s = self.replayed(prompt, data)
                    self.ae(self.v2l(s, 0)[:6], (0, 1, 5, 4, 3, 2))  # the typed space joins the run
                    self.assert_cursor_at(s, 6, (1, True), 1)
        s = self.replayed('B', 'שלום 12 '.encode(), cols=14)
        self.assert_cursor_at(s, 10, (1, True), 1)  # the space after the digits joins the run
        # no typed text: the cursor is on the cell after the prompt, which has no map
        s = self.replayed('B', b'')
        self.assertIsNone(self.v2l(s, 0))
        self.assert_cursor_at(s, 2, (2, False), 2)

    def test_typed_space_in_prompt_isolate(self):
        # Spaces typed after a letter inside the prompt's unclosed FSI take that letter's level, so they show beside the word at once,
        # and the cursor sits after the last of them. A row has 24 cells, so the RTL paragraph is right-aligned and its v2l is checked
        # on the last x + 1 visual columns. None for v2l and levels means the row has no map: the identity layout at the paragraph level.
        F, T = False, True
        RTL = b'\x1b[2 k'
        # the isolate cap rows below hardcode their pair counts for this cap
        self.ae(BIDI_MAX_ISOLATES, 32)
        # (name, prompt, paragraph prefix, right-aligned, text, cursor x, v2l, levels, beam (column, mirrored), column of the other shapes)
        # v2l and levels cover the cells up to and including the cursor's
        cases = (
            ('hebrew and space', 'B', None, F, 'אני ', 6, (0, 1, 5, 4, 3, 2, 6), (0, 0, 1, 1, 1, 1, 0), (1, T), 1),
            ('hebrew and two spaces', 'B', None, F, 'אני  ', 7, (0, 1, 6, 5, 4, 3, 2, 7), (0, 0, 1, 1, 1, 1, 1, 0), (1, T), 1),
            ('hebrew', 'B', None, F, 'אני', 5, (0, 1, 4, 3, 2, 5), (0, 0, 1, 1, 1, 0), (1, T), 1),
            ('latin and space', 'B', None, F, 'hello ', 8, None, None, (8, F), 8),
            (
                'hebrew, latin and space',
                'B',
                None,
                F,
                'שלום abc ',
                11,
                (0, 1, 7, 8, 9, 10, 6, 5, 4, 3, 2, 11),
                (0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 0),
                (6, F),
                6,
            ),
            (
                'rtl paragraph, hebrew, latin and space',
                'B',
                RTL,
                T,
                'שלום abc ',
                11,
                (11, 7, 8, 9, 10, 6, 5, 4, 3, 2, 1, 0),
                (2, 1, 3, 3, 3, 3, 3, 4, 4, 4, 4, 1),
                (17, F),
                17,
            ),
            ('rtl paragraph, latin', 'B', RTL, T, 'abc', 5, (5, 2, 3, 4, 1, 0), (2, 1, 2, 2, 2, 1), (22, F), 22),
            ('rtl paragraph, latin and space', 'B', RTL, T, 'abc ', 6, (6, 2, 3, 4, 5, 1, 0), (2, 1, 2, 2, 2, 2, 1), (22, F), 22),
            (
                'rtl paragraph, latin and two spaces',
                'B',
                RTL,
                T,
                'abc  ',
                7,
                (7, 2, 3, 4, 5, 6, 1, 0),
                (2, 1, 2, 2, 2, 2, 2, 1),
                (22, F),
                22,
            ),
            # without the FSI the cursor's gap still takes the run's level
            ('no isolate, hebrew and space', 'A', None, F, 'אני ', 6, (0, 1, 5, 4, 3, 2, 6), (0, 0, 1, 1, 1, 1, 0), (1, T), 1),  # the space joins the run
            # empty cells, a space after a number and a space over the isolate cap join the run; a closed isolate is left alone
            (
                'cursor moved past empty cells',
                'B',
                None,
                F,
                'אני\x1b[3C',
                8,
                (0, 1, 7, 6, 5, 4, 3, 2, 8),
                (0, 0, 1, 1, 1, 1, 1, 1, 0),
                (1, T),
                1,  # the empty cells join the run
            ),
            # the PDI joins the cell of the first space, and L1 resets the spaces and the PDI at the end to the paragraph level
            ('closed isolate', 'B', None, F, 'אני \u2069 ', 7, (0, 1, 4, 3, 2, 5, 6, 7), (0, 0, 1, 1, 1, 0, 0, 0), (7, F), 7),
            (
                'number and space',
                'B',
                None,
                F,
                'שלום 12 ',
                10,
                (0, 1, 9, 7, 8, 6, 5, 4, 3, 2, 10),
                (0, 0, 1, 1, 1, 1, 1, 2, 2, 1, 0),
                (1, T),
                1,  # the space joins the run
            ),
            # a cell holds two controls: the FSI and 15 LRI PDI pairs are 31 isolates, under BIDI_MAX_ISOLATES, so they are fed and
            # with 16 pairs they are 33, no isolate is fed, and the cursor's gap puts the space beside the word
            (
                'under the isolate cap',
                'B',
                None,
                F,
                'א\u2066\u2069' * (BIDI_MAX_ISOLATES // 2 - 1) + 'י ',
                19,
                (0, 1) + tuple(range(18, 1, -1)) + (19,),
                (0, 0) + (1,) * 17 + (0,),
                (1, T),
                1,
            ),
            (
                'over the isolate cap',
                'B',
                None,
                F,
                'א\u2066\u2069' * (BIDI_MAX_ISOLATES // 2) + 'י ',
                20,
                (0, 1) + tuple(range(19, 1, -1)) + (20,),
                (0, 0) + (1,) * 18 + (0,),
                (1, T),
                1,  # the space joins the run
            ),
            # readline erases a letter with backspace, space, backspace: the space it writes is a typed blank after a letter, so it
            # joins the run and shows where the erased letter was
            ('erase idiom', 'B', None, F, 'אני\x08 \x08', 4, (0, 1, 4, 3, 2), (0, 0, 1, 1, 1), (2, T), 2),
            ('erase idiom, no isolate', 'A', None, F, 'אני\x08 \x08', 4, (0, 1, 3, 2, 4), (0, 0, 1, 1, 0), (1, T), 1),
        )
        for name, prompt, prefix, rtl, text, x, v2l, levels, beam, block in cases:
            with self.subTest(name):
                s = self.replayed(prompt, text.encode(), cols=24, prefix=prefix)
                self.ae(s.cursor.x, x)
                got_v2l, got_levels = self.v2l(s, 0), self.levels(s, 0)
                if v2l is None:
                    self.assertIsNone(got_v2l)
                else:
                    self.ae(got_v2l[-len(v2l) :] if rtl else got_v2l[: len(v2l)], v2l)
                if levels is None:
                    self.assertIsNone(got_levels)
                else:
                    self.ae(got_levels[: len(levels)], levels)
                self.assert_cursor_at(s, x, beam, block)

    def test_cursor_follows_run_lands(self):
        # The cursor drawn after the text is where the next char lands: type the char the probe rule predicts at the cursor (a
        # Hebrew letter after an odd level, a digit after an even one, an Arabic-Indic digit after one) and its column is the
        # cursor's (the beam's, edge aside).
        F, T = False, True
        AUTO = b'\x1b[?2501h'

        def row(text, cols, seqs=b''):
            s = self.screen(cols=cols)
            parse_bytes(s, seqs)
            s.draw(text)
            return s

        def prompt(text, cols):
            return self.replayed('B', text.encode(), cols=cols)

        # (name, screen with the cursor after the text, typed char, beam (column, mirrored) before typing, column it lands at)
        cases = (
            ('ltr, hebrew and space', row('ab אני ', 10), 'א', (2, T), 3),
            ('digits and space after hebrew', row('שלום 123 ', 12), '0', (0, F), 0),
            ('percent after digits and space', row('שלום 50% ', 12), '0', (0, F), 0),
            ('degree after digit and space', row('שלום 5° ', 12), '0', (0, F), 0),
            ('shekel after digit and space', row('שלום 5₪ ', 12), '0', (0, F), 0),
            ('latin digits in an rli and space', row('x⁧abc 12 ', 16), '0', (8, F), 8),
            ('latin in an rli and space', row('x⁧abc ', 10), '0', (5, F), 5),
            ('digits in a closed rli and space', row('a⁧12 34⁩ ', 10), '0', (7, F), 7),
            ('latin digits after latin, rtl', row('שלום abc 123 ', 16, AUTO), '0', (11, F), 10),
            ('digits in an lri, rtl', row('ש⁦123 ', 12, AUTO), '0', (11, F), 10),
            ('arabic digits and space', row('مرحبا ١٢ ', 14), '٠', (0, F), 0),
            ('prompt, digits and space', prompt('שלום 12 ', 14), '0', (1, T), 2),
            ('prompt, neutral and space', prompt('שלום! ', 14), 'א', (1, T), 2),
            ('wrapped ltr', row('abcd שלום 123 ', 10), '0', (0, F), 0),
            ('wrapped rtl', row('שלום abcd 12 ', 10, AUTO), '0', (9, T), 9),
        )
        for name, s, ch, beam, col in cases:
            with self.subTest(name):
                x, y = s.cursor.x, s.cursor.y
                test_screen_update_cell_data(s)
                self.ae(s.test_cursor_render(CURSOR_BEAM), beam)
                s.draw(ch)
                test_screen_update_cell_data(s)
                self.ae(s.test_bidi_visual_x(x, y), col)

    def test_cursor_follows_run_prompt_and_wrapped(self):
        # a prompt isolate or a paragraph over two rows: the gap's level decides the cursor
        F, T = False, True
        AUTO = b'\x1b[?2501h'
        # (name, text, columns, cursor x, beam (column, mirrored), column of the other shapes)
        for name, text, cols, x, beam, block in (
            ('digits and space', 'שלום 12 ', 14, 10, (1, T), 1),
            ('hebrew and space', 'אני ', 40, 6, (1, T), 1),
            ('neutral and space', 'שלום! ', 14, 8, (1, T), 1),
        ):
            with self.subTest('prompt B', name=name):
                s = self.replayed('B', text.encode(), cols=cols)
                self.assert_cursor_at(s, x, beam, block)
        # (name, sequences, text, cursor row, cursor x, beam, block, levels of the cursor row up to the cursor)
        for name, seqs, text, y, x, beam, block, levels in (
            ('wrapped ltr', b'', 'abcd שלום 123 ', 1, 4, (0, F), 0, (2, 2, 2, 1)),
            ('wrapped rtl', AUTO, 'שלום abcd 12 ', 1, 3, (9, T), 9, (2, 2, 2)),
        ):
            with self.subTest(name):
                s = self.screen()
                parse_bytes(s, seqs)
                s.draw(text)
                self.assert_cursor_at_row(s, y, x, beam, block)
                self.ae(self.levels(s, y)[: len(levels)], levels)

    def test_typed_gap_only_at_cursor(self):
        # The gap is laid out only on the cursor's row, past its text. Everything else keeps the layout it had.
        F = False
        plain = (0, 1, 2, 5, 4, 3, 6) + ID(7, 10)
        plain_levels = (0, 0, 0, 1, 1, 1, 0, 0, 0, 0)
        s = self.screen()
        s.draw('ab אני ')
        s.cursor_position(3, 1)
        test_screen_update_cell_data(s)
        self.ae((self.v2l(s, 0), self.levels(s, 0)), (plain, plain_levels), 'cursor on another row')
        for x in (0, 4, 6):  # before the text end: not after a blank
            with self.subTest(x=x):
                s.cursor_position(1, x + 1)
                test_screen_update_cell_data(s)
                self.ae((self.v2l(s, 0), self.levels(s, 0)), (plain, plain_levels))
        # no RTL: no map
        s = self.screen()
        s.draw('hello ')
        self.assert_cursor_at(s, 6, (6, F), 6)
        self.assertIsNone(self.v2l(s, 0))
        # the gap goes away with the cursor and leaves nothing behind
        s = self.screen()
        s.draw('שלום ')
        test_screen_update_cell_data(s)
        self.ae(self.v2l(s, 0)[:5], (4, 3, 2, 1, 0))  # the blank is left of the word while the cursor is in its gap
        s.cursor_position(2, 1)
        test_screen_update_cell_data(s)
        self.ae(self.v2l(s, 0)[:5], (3, 2, 1, 0, 4))
        f = self.screen()
        f.draw('שלום ')
        f.cursor_position(2, 1)
        self.same_as(s, f)

    def test_claude_code_typed_gap(self):
        # Claude Code 2.1.326 keeps the terminal cursor at its input position and never writes the typed space: the blank is an
        # empty cell, and the space moves the cursor one cell past the word with CSI 16;8H. The row starts with `❯ ` at row 16.
        prompt = b'\x1b[16;1H\xe2\x9d\xaf '
        word = b'\x1b[?25l\x1b[H\r\x1b[2C\x1b[15B' + 'הסדר'.encode() + b'\x1b[K\x1b[20;1H\x1b[16;7H\x1b[?25h'
        space = b'\x1b[?25l\x1b[16;8H\x1b[?25h'
        more = b'\x1b[?25l\x1b[H\r\x1b[7C\x1b[15B' + 'לא'.encode() + b'\x1b[20;1H\x1b[16;10H\x1b[?25h'
        T = True
        y = 15

        def build(seqs, *steps):
            s = self.create_screen(cols=20, lines=20, scrollback=5, options={})
            parse_bytes(s, b'\x1b[8h' + seqs + prompt)
            for step in steps:
                parse_bytes(s, step)
            test_screen_update_cell_data(s)
            return s

        def check(s, v2l, levels, x, beam, block, msg):
            self.ae(self.v2l(s, y), v2l, msg)
            self.ae(self.levels(s, y), levels, msg)
            self.assert_cursor_at_row(s, y, x, beam, block, msg)

        # LTR row (bidi on, no 2501): the empty cell before the cursor joins the word's run
        steps = (
            ('word', (word,), (0, 1, 5, 4, 3, 2) + ID(6, 20), (0, 0, 1, 1, 1, 1) + (0,) * 14, 6, (1, T), 1),
            # the gap is shown left of the word (cell 6 at the run's left edge) and the cursor stays where the next char lands
            ('space', (word, space), (0, 1, 6, 5, 4, 3, 2) + ID(7, 20), (0, 0, 1, 1, 1, 1, 1) + (0,) * 13, 7, (1, T), 1),
            # the next word joins the run in one piece
            ('next word', (word, space, more), (0, 1, 8, 7, 6, 5, 4, 3, 2) + ID(9, 20), (0, 0) + (1,) * 7 + (0,) * 11, 9, (1, T), 1),
        )
        for name, seqs, v2l, levels, x, beam, block in steps:
            with self.subTest('ltr', step=name):
                check(build(b'', *seqs), v2l, levels, x, beam, block, name)
        with self.subTest('ltr', step='next char lands'):
            s = build(b'', word, space)
            s.draw('ל')  # at x=7, where the cursor is
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_visual_x(7, y), 2)  # at the left edge of the run, where the cursor was drawn
        # 2501: the row resolves RTL (the ❯ is a neutral, ה the first strong letter). The gap is already left of the word:
        # all three steps are unchanged
        rtl = tuple(range(19, -1, -1))
        for name, seqs, x, col in (('word', (word,), 6, 13), ('space', (word, space), 7, 12), ('next word', (word, space, more), 9, 10)):
            with self.subTest('2501', step=name):
                check(build(b'\x1b[?2501h', *seqs), rtl, (1,) * 20, x, (col, T), col, name)
        with self.subTest('2501', step='next char lands'):
            s = build(b'\x1b[?2501h', word, space)
            s.draw('ל')
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_visual_x(7, y), 12)

    def test_tmux_pane_typed_gap(self):
        # tmux side by side panes: the pane's FSI rides on the glyph cell, the prompt's on the space, the PDI closing the prompt
        # on the typed space (cell 5), then blanks and the pane's PDI on a space at the pane's end
        row = '\U000f023a⁨ ⁨אני ⁩' + ' ' * 10 + ' ⁩'
        F, T = False, True
        for seqs in (b'', b'\x1b[?2501h'):  # the glyph is class L, so the paragraph is LTR with and without 2501
            s = self.screen(cols=24)
            parse_bytes(s, seqs)
            s.draw(row)
            with self.subTest('cursor after the typed space', seqs=seqs):
                s.cursor_position(1, 7)
                test_screen_update_cell_data(s)
                # the typed space is level 3 beside the word, not reset to 0 by L1 before the PDI
                self.ae(self.v2l(s, 0)[:7], (0, 1, 5, 4, 3, 2, 6))
                self.ae(self.levels(s, 0)[:7], (0, 2, 3, 3, 3, 3, 0))
                self.assert_cursor_at(s, 6, (1, T), 1)
            with self.subTest('blank after the closing PDI', seqs=seqs):
                # the probe lands inside the pane's isolate and turns it RTL, moving cells before the gap: it is rejected and the
                # layout and cursor are unchanged
                s.cursor_position(1, 8)
                test_screen_update_cell_data(s)
                self.ae(self.v2l(s, 0)[:7], (0, 1, 4, 3, 2, 5, 6))
                self.ae(self.levels(s, 0)[:7], (0, 2, 3, 3, 3, 0, 0))
                self.assert_cursor_at(s, 7, (7, F), 7)

    def test_typed_gap_cache(self):
        # The cursor's gap is part of the paragraph cache key: moving the cursor into or out of a gap resolves only the cursor's
        # paragraph, and no layout made for one cursor state is served for another
        rows = ['אני ', 'גדה ', 'abc']
        gap = (3, 2, 1, 0, 4)  # row 0 with its gap shown
        plain = (2, 1, 0, 3, 4)

        def build(first='אני ', cursor=(1, 5)):
            f = self.filled([first] + rows[1:])
            f.cursor_position(*cursor)
            return f

        s = build()
        self.warm(s)
        self.ae((self.v2l(s, 0)[:5], self.v2l(s, 1)[:5]), (gap, plain))
        # out of the gap, and in again: one resolve each, the cursor's paragraph only
        s.cursor_position(3, 1)
        self.ae(self.frame(s)[0], 1)
        self.ae(self.v2l(s, 0)[:5], plain)
        self.same_as(s, build(cursor=(3, 1)))
        s.cursor_position(1, 5)
        self.ae(self.frame(s)[0], 1)
        self.ae(self.v2l(s, 0)[:5], gap)
        self.same_as(s, build())
        # a same-class edit (one Hebrew letter for another) after leaving the gap shortcuts, and equals a fresh screen's layout
        s.cursor_position(3, 1)
        self.frame(s)
        self.put(s, 1, 1, 'ב')
        s.cursor_position(3, 1)
        self.ae(self.frame(s)[0], 0)
        self.same_as(s, build('בני ', (3, 1)))
        # the same edit while the gap is applied cannot shortcut: the layout holds retyped blanks
        s = build()
        self.warm(s)
        self.put(s, 1, 1, 'ב')
        s.cursor_position(1, 5)
        self.ae(self.frame(s)[0], 1)
        self.ae(self.v2l(s, 0)[:5], gap)
        self.same_as(s, build('בני '))
        # cursor moves between positions that are no gap, on other paragraphs' rows: no resolve
        s = build(cursor=(3, 1))
        self.warm(s)
        for y, x in ((3, 2), (3, 4), (2, 3), (2, 2), (3, 3)):
            with self.subTest(y=y, x=x):
                s.cursor_position(y, x)
                self.ae(self.frame(s)[0], 0)
        self.ae((self.v2l(s, 0)[:5], self.v2l(s, 1)[:5]), (plain, plain))

    def test_typed_gap_rejected_probe_leaves_no_trace(self):
        # the tmux row with the cursor on the blank after its closing PDI: the probe is rejected, and no frame after it differs
        # from a fresh screen's, whatever the cursor does next
        def build(first='א', x=7):
            s = self.screen(cols=24)
            s.draw('\U000f023a⁨ ⁨' + first + 'ני ⁩' + ' ' * 10 + ' ⁩')
            s.cursor_position(1, x + 1)
            return s

        s = build()
        self.warm(s)
        today = ((0, 1, 4, 3, 2, 5, 6), (0, 2, 3, 3, 3, 0, 0))
        self.ae((tuple(self.v2l(s, 0)[:7]), self.levels(s, 0)[:7]), today)
        for name, x in (('same-class edit', 7), ('cursor into the gap', 6), ('cursor out again', 7), ('cursor far away', 20)):
            with self.subTest(name):
                if name == 'same-class edit':
                    self.put(s, 1, 3, 'ב')
                s.cursor_position(1, x + 1)
                buf = self.frame_buf(s)[1]
                self.assert_same_as_fresh(s, buf, build('ב', x), name)
        s.cursor_position(1, 8)
        test_screen_update_cell_data(s)
        self.ae((tuple(self.v2l(s, 0)[:7]), self.levels(s, 0)[:7]), today)

    def test_typed_gap_paused(self):
        # A paused frame lays out the gap on the snapshot row of the cursor only: that row is cursor.y + scrolled_by + pixel_scroll,
        # not cursor.y + pixel_scroll. The history line above the cursor's line has a blank tail too and keeps its layout.
        for pixel_scroll in (True, False):
            with self.subTest(pixel_scroll=pixel_scroll):
                s = self.screen(cols=10, lines=3, scrollback=10, pixel_scroll=pixel_scroll)
                parse_bytes(s, 'אב  \r\n\r\n\r\n'.encode())
                s.cursor_position(1, 1)
                s.draw('אני ')
                test_screen_update_cell_data(s)
                s.scroll(1, True)
                self.ae((s.historybuf.count, s.scrolled_by, s.cursor.y, s.cursor.x), (1, 1, 0, 4))
                parse_bytes(s, b'\x1b[?2026h')
                test_screen_update_cell_data(s)
                first = int(pixel_scroll)  # the render row of the history line; the cursor's line is the next
                self.ae(tuple(s.test_bidi_v2l(first)[:5]), (1, 0, 2, 3, 4))
                self.ae(tuple(s.test_bidi_v2l(first + 1)[:5]), (3, 2, 1, 0, 4))  # the gap is shown in the cursor's line

    def test_typed_gap_cursor_gap_changed(self):
        # the trigger for a cursor-only move: true when the move enters, leaves or changes a gap, false otherwise
        def moved(s, y, x):
            s.cursor_position(y + 1, x + 1)
            return s.test_bidi_cursor_gap_changed()

        def settle(s):
            test_screen_update_cell_data(s)
            self.assertFalse(s.test_bidi_cursor_gap_changed(), 'a frame with the cursor unchanged')

        # rows: 0 RTL word and blank, 1 Latin, 2 unstamped, 3 another RTL paragraph
        s = self.create_screen(cols=10, lines=6, scrollback=5, options={})
        s.cursor_position(3, 1)
        s.draw('אבג')  # written before CSI 8 h
        parse_bytes(s, b'\x1b[8h')
        self.put(s, 1, 1, 'אני ')
        self.put(s, 2, 1, 'abc')
        self.put(s, 4, 1, 'גדה ')
        s.cursor_position(5, 1)
        settle(s)
        for name, y, x, want in (
            ('into a gap', 0, 4, True),
            ('out to a latin row', 1, 3, True),
            ('into the gap again', 0, 4, True),
            ('out to an unstamped row', 2, 3, True),
            ('into the gap once more', 0, 4, True),
            ('out to another paragraph', 3, 2, True),
            ('a move that stays out of the gap', 3, 1, False),
        ):
            with self.subTest(name):
                self.ae(moved(s, y, x), want)
                settle(s)
        # a Latin row with prompt isolates is never active: no upload for a cursor-only space
        with self.subTest('latin row with isolates'):
            s = self.replayed('B', 'hello '.encode(), cols=24)
            settle(s)
            for x in (2, 8, 5, 8):
                self.assertFalse(moved(s, 0, x))
                settle(s)
        # RTL text on the row above the cursor's, digits only on the cursor's row: the digits are level 2, so the row is active
        with self.subTest('wrapped paragraph'):
            s = self.screen()
            parse_bytes(s, b'\x1b[?2501h')
            s.draw('שלום abcd 12 ')
            s.cursor_position(4, 1)
            settle(s)
            self.assertTrue(moved(s, 1, 3))
            settle(s)
            self.assertTrue(moved(s, 3, 0))
            settle(s)
        with self.subTest('paused'):
            s = self.screen()
            s.draw('אני ')
            s.cursor_position(3, 1)
            settle(s)
            s.pause_rendering(True, 1000)
            test_screen_update_cell_data(s)
            self.assertFalse(moved(s, 0, 4))
        with self.subTest('force_ltr'):
            s = self.screen(force_ltr=True)
            s.draw('אני ')
            s.cursor_position(3, 1)
            test_screen_update_cell_data(s)
            self.assertFalse(moved(s, 0, 4))

    PROMPT_ROW = PUA + b' \xe2\x81\xa8'  # the prompt `PUA SP FSI`: the FSI joins the space's cell and is never closed
    CONTINUE = b'\r\n\x1b[K\x1b[2C'  # fish's Shift+Enter: the next row, erased and indented by two cells
    OSC133_A, OSC133_B, OSC133_C = b'\x1b]133;A;click_events=1\x1b\\', b'\x1b]133;B\x1b\\', b'\x1b]133;C\x1b\\'

    def fish_repaint(self, text, col=2):
        # fish repaints typed text char by char: CR, CUF to the char's column, the char. A word's first letter and every space is
        # written a second time, over itself after a backspace. The cursor ends at the column after the text.
        out, word_start = b'', True
        for ch in text:
            out += f'\r\x1b[{col}C{ch}'.encode()
            if word_start or ch == ' ':
                out += f'\r\x1b[{col + 1}C\x08{ch}'.encode()
            word_start = ch == ' '
            col += 1
        return out + f'\r\x1b[{col}C'.encode()

    def shell_rows(self, form, rows):
        # the bytes for a prompt row with rows[0] typed after it and each later row a continuation line indented by two cells.
        # plain: the text drawn as is. fish: the repaint idiom. tmux: every row placed with CUP, the indent cells never written.
        first, *rest = rows
        if form == 'tmux':
            return b'\x1b[1;1H' + self.PROMPT_ROW + first.encode() + b''.join(f'\x1b[{i + 2};3H{row}'.encode() for i, row in enumerate(rest))
        if form == 'fish':
            return self.PROMPT_ROW + b'\x1b[K' + self.fish_repaint(first) + b''.join(self.CONTINUE + self.fish_repaint(row) for row in rest)
        return self.PROMPT_ROW + first.encode() + b''.join(self.CONTINUE + row.encode() for row in rest)

    def replay_bytes(self, data, cols=40, lines=6):
        s = self.screen(cols=cols, lines=lines)
        parse_bytes(s, self.AUTO + data)
        test_screen_update_cell_data(s)
        return s

    def test_continuation_rows_in_prompt_isolate(self):
        # A row below the prompt's row is a hard break, but the prompt's FSI is still open, so the rows are one paragraph under the
        # prompt's LTR base (U+F023A is class L) and the row lies inside the FSI's isolate, which takes its direction from the first
        # strong letter after it. The indent cells are blanks inside the isolate, a blank between two letters follows N1, and L1 sets
        # the blanks at the end of a row to the paragraph level. Layouts are the UAX#9 result for the joined text with each row as a
        # line (checked with libfribidi), not what kitty printed, except for typed blanks after a letter in the last row, which keep
        # the letter's level by kitty's own rule instead of L1's.
        F, T = False, True
        n = 40
        ident = tuple(range(n))

        def v(*head):
            return head + ident[len(head) :]

        def lv(*head):
            return head + (0,) * (n - len(head))

        H1, H2, H3 = 'שלום', 'שורה שניה', 'עוד שורה'
        # row 1 under a first strong 'h': an LTR isolate at level 2; under a Hebrew first letter: an RTL one, the word at level 1
        english = (ident, lv(0, 0, 2, 2, 2, 2, 2))
        hebrew = (v(0, 1, 5, 4, 3, 2), lv(0, 0, 1, 1, 1, 1))
        # (name, rows, (v2l, levels) of each row, (cursor row, cursor x, beam, block column))
        # The cursor is where the next letter of the run goes. A Hebrew run at level 3 starting at column 2 grows leftwards in
        # place, so its next letter takes column 2 and the beam is on that column's left edge, drawn mirrored in column 1.
        cases = (
            (
                'english, hebrew',
                ('hello', H2),
                (english, (v(0, 1, *range(10, 1, -1)), lv(2, 2, *(3,) * 9))),
                (1, 11, (1, T), 1),
            ),
            # the isolate is RTL: indent and text are one run at level 1 (N1 between two R), so the indent lands right of the text.
            # The run starts at column 0, the beam on its left edge, which is the left edge of the row.
            (
                'hebrew, hebrew',
                (H1, H2),
                (hebrew, (v(*range(10, -1, -1)), lv(*(1,) * 11))),
                (1, 11, (0, F), 0),
            ),
            # the English at level 2 inside the RTL isolate's level 1; the blanks between the Hebrew and the English are R (N2)
            (
                'hebrew, english',
                (H1, 'hello'),
                (hebrew, (v(2, 3, 4, 5, 6, 1, 0), lv(1, 1, 2, 2, 2, 2, 2))),
                (1, 7, (5, F), 5),
            ),
            # the second Hebrew row's indent lies between two Hebrew letters, so it joins their run at level 3. The first Hebrew
            # row's trailing blanks are at the paragraph level, as at the end of any line
            (
                'english, hebrew, hebrew',
                ('hello', H2, H3),
                (english, (v(0, 1, *range(10, 1, -1)), lv(2, 2, *(3,) * 9)), (v(*range(9, -1, -1)), lv(*(3,) * 10))),
                (2, 10, (0, F), 0),
            ),
            # the typed space after the letter takes the letter's level (kitty's rule), so the run grows over it
            (
                'english, hebrew and a typed space',
                ('hello', H2 + ' '),
                (english, (v(0, 1, *range(11, 1, -1)), lv(2, 2, *(3,) * 10))),
                (1, 12, (1, T), 1),
            ),
            # An empty line of the command is a row never written: it joins like any other. As a line of blanks it is all at the
            # paragraph level (L1). Under 'h' the blanks from 'o' to the Hebrew are between L and R, so take the isolate's L (N2):
            # the third row is laid out as the second row of 'english, hebrew'
            (
                'english, empty line, hebrew',
                ('hello', '', H2),
                (english, (ident, lv()), (v(0, 1, *range(10, 1, -1)), lv(2, 2, *(3,) * 9))),
                (2, 11, (1, T), 1),
            ),
            # under a Hebrew first letter the blanks from the first Hebrew word to the next are between two R, so R at level 1 (N1):
            # the third row is laid out as the second row of 'hebrew, hebrew'
            (
                'hebrew, empty line, hebrew',
                (H1, '', H2),
                (hebrew, (ident, lv()), (v(*range(10, -1, -1)), lv(*(1,) * 11))),
                (2, 11, (0, F), 0),
            ),
        )
        for name, rows, layouts, (cy, cx, beam, block) in cases:
            for form in ('plain', 'fish', 'tmux'):
                with self.subTest(name, form=form):
                    s = self.replay_bytes(self.shell_rows(form, rows))
                    self.ae((s.cursor.y, s.cursor.x), (cy, cx))
                    for y, (v2l, levels) in enumerate(layouts):
                        self.assert_obs(self.obs(s, y), v2l, levels, False, f'row {y + 1}')
                    self.assert_cursor_at_row(s, cy, cx, beam, block)

    def test_blank_rows_below_prompt_isolate(self):
        # A row without text is passed through whether it is stamped (written, then erased) or not: blank rows below the last
        # text row join nothing, so the typed space keeps its letter's level as on a single row, and a blank row between two
        # text rows of a group joins them
        T = True
        n = 40
        ident = tuple(range(n))

        def v(*head):
            return head + ident[len(head) :]

        def lv(*head):
            return head + (0,) * (n - len(head))

        H2 = 'שורה שניה'
        english = (ident, lv(0, 0, 2, 2, 2, 2, 2))

        def write_and_erase(form, y):
            # text written on row y and erased again, the cursor put back: plain erases the line, tmux erases to its end from column 0
            erase = b'\x1b[2K' if form == 'plain' else f'\x1b[{y + 1};1H\x1b[K'.encode()
            return b'\x1b7' + f'\x1b[{y + 1};1Hxyz'.encode() + erase + b'\x1b8'

        # (name, rows, blank row, stamped, (v2l, levels) of each row, (cursor row, cursor x, beam, block column))
        cases = (
            ('typed space', ('אני ',), 1, True, ((v(0, 1, 5, 4, 3, 2), lv(0, 0, 1, 1, 1, 1)),), (0, 6, (1, T), 1)),
            ('typed space, unstamped row below', ('אני ',), 1, False, ((v(0, 1, 5, 4, 3, 2), lv(0, 0, 1, 1, 1, 1)),), (0, 6, (1, T), 1)),
            (
                'typed space on a continuation row',
                ('hello', H2 + ' '),
                2,
                True,
                (english, (v(0, 1, *range(11, 1, -1)), lv(2, 2, *(3,) * 10))),
                (1, 12, (1, T), 1),
            ),
            (
                'typed space on a continuation row, unstamped row below',
                ('hello', H2 + ' '),
                2,
                False,
                (english, (v(0, 1, *range(11, 1, -1)), lv(2, 2, *(3,) * 10))),
                (1, 12, (1, T), 1),
            ),
            (
                'blank row between two rows',
                ('hello', '', H2),
                1,
                True,
                (english, (ident, lv()), (v(0, 1, *range(10, 1, -1)), lv(2, 2, *(3,) * 9))),
                (2, 11, (1, T), 1),
            ),
        )
        for name, rows, blank, stamped, layouts, (cy, cx, beam, block) in cases:
            for form in ('plain', 'tmux'):
                with self.subTest(name, form=form):
                    data = self.shell_rows(form, rows) + (write_and_erase(form, blank) if stamped else b'')
                    s = self.replay_bytes(data)
                    self.ae(str(s.line(blank)), '')
                    self.ae(bool(s.test_bidi_line_record(blank)[0]), stamped)
                    self.ae((s.cursor.y, s.cursor.x), (cy, cx))
                    for y, (v2l, levels) in enumerate(layouts):
                        self.assert_obs(self.obs(s, y), v2l, levels, False, f'row {y + 1}')
                    self.assert_cursor_at_row(s, cy, cx, beam, block)

    def test_output_rows_after_prompt_isolate(self):
        # What stays as it is: a row that is not a continuation of an open isolate is its own paragraph, auto-RTL when it has only
        # Hebrew, so right-aligned with every cell at level 1
        n = 40
        rtl_row = (tuple(range(n - 1, -1, -1)), (1,) * n, True)
        ltr_row = (tuple(range(n)), None, False)
        H1, H2, H3 = 'שלום', 'שורה שניה', 'עוד שורה'
        PDI = '⁩'.encode()
        # a row after an explicit record: LTR under 2501 off, the Hebrew at level 1 and the indent at 0
        head = tuple(range(2)) + tuple(range(10, 1, -1)) + tuple(range(11, n))
        ltr_hebrew = (head, (0, 0) + (1,) * 9 + (0,) * (n - 11), False)
        prompt = self.PROMPT_ROW + b'hello'
        cases = (
            ('output after OSC 133 C', prompt + b'\r\n' + self.OSC133_C + H1.encode() + b'\r\n' + H3.encode(), (ltr_row, rtl_row, rtl_row)),
            (
                'output after OSC 133 A, B and C',
                self.OSC133_A + self.PROMPT_ROW + self.OSC133_B + b'hello\r\n' + self.OSC133_C + H1.encode(),
                (ltr_row, rtl_row),
            ),
            ('output row after a PDI', prompt + PDI + self.CONTINUE + H2.encode(), (ltr_row, rtl_row)),
            ('output row at column 0 after a PDI', prompt + PDI + b'\r\n' + H1.encode(), (ltr_row, rtl_row)),
            # its own LTR paragraph from 'w', the Hebrew at level 1; joined into a still open isolate it would be at level 3
            (
                'english and hebrew output after a PDI',
                prompt + PDI + self.CONTINUE + b'world ' + H1.encode(),
                (ltr_row, (tuple(range(8)) + (11, 10, 9, 8) + tuple(range(12, n)), (0,) * 8 + (1,) * 4 + (0,) * (n - 12), False)),
            ),
            # an empty row joins nothing by itself: the output row after it is still cut by the PDI or the mark
            ('output row after a PDI and an empty row', prompt + PDI + self.CONTINUE + self.CONTINUE + H2.encode(), (ltr_row, ltr_row, rtl_row)),
            ('output after an empty row and OSC 133 C', prompt + b'\r\n\r\n' + self.OSC133_C + H1.encode(), (ltr_row, ltr_row, rtl_row)),
            ('output after an empty row marked by OSC 133 C', prompt + b'\r\n' + self.OSC133_C + b'\r\n' + H1.encode(), (ltr_row, ltr_row, rtl_row)),
            (
                'hebrew first row closed by a PDI',
                self.PROMPT_ROW + H1.encode() + PDI + b'\r\n' + H3.encode(),
                ((tuple(range(2)) + (5, 4, 3, 2) + tuple(range(6, n)), (0, 0, 1, 1, 1, 1) + (0,) * (n - 6), False), rtl_row),
            ),
            (
                'three rows closed by a PDI',
                prompt + PDI + self.CONTINUE + H2.encode() + self.CONTINUE + H3.encode(),
                (ltr_row, rtl_row, rtl_row),
            ),
            ('row under another record, 2501 off', prompt + b'\r\n\x1b[?2501l\x1b[K\x1b[2C' + H2.encode(), (ltr_row, ltr_hebrew)),
            ('row under another record, RTL', prompt + b'\r\n\x1b[2 k\x1b[K\x1b[2C' + H2.encode(), (ltr_row, rtl_row)),
        )
        for name, data, layouts in cases:
            with self.subTest(name):
                s = self.replay_bytes(data)
                for y, (v2l, levels, rtl) in enumerate(layouts):
                    self.assert_obs(self.obs(s, y), v2l, levels, rtl, f'row {y + 1}')

    def test_continuation_stops_at_output_start(self):
        # with the shell's marks, the rows between the prompt (A) and the output (C) join, the output after C does not
        n = 40
        ident = tuple(range(n))
        data = self.OSC133_A + self.PROMPT_ROW + self.OSC133_B + b'hello' + self.CONTINUE + 'שורה שניה'.encode() + b'\r\n' + self.OSC133_C + 'עוד שורה'.encode()
        s = self.replay_bytes(data)
        with self.subTest('rows between the marks join'):
            self.assert_obs(self.obs(s, 0), ident, (0, 0, 2, 2, 2, 2, 2) + (0,) * (n - 7), False)
            self.assert_obs(self.obs(s, 1), (0, 1) + tuple(range(10, 1, -1)) + ident[11:], (2, 2) + (3,) * 9 + (0,) * (n - 11), False)
        with self.subTest('the output does not'):
            self.assert_obs(self.obs(s, 2), ident[::-1], (1,) * n, True)

    FSI, PDI = '⁨', '⁩'

    def obs(self, s, y):
        # (v2l, levels, paragraph rtl) of screen row y, None for a row without a map; the screens here have no pixel scroll
        r = self.rr(s, y)
        return s.test_bidi_v2l(r), self.lv(s, y), s.test_bidi_paragraph_rtl(r)

    def assert_obs(self, got, v2l, levels, rtl, msg=''):
        # a row without a map is the identity at the paragraph level, so its levels are only checked when it has one
        v2l_got, levels_got, rtl_got = got
        self.ae(tuple(range(len(v2l))) if v2l_got is None else v2l_got, v2l, msg)
        if levels_got is not None and levels is not None:
            self.ae(levels_got, levels, msg)
        self.ae(bool(rtl_got), rtl, msg)

    def view_obs(self, s):
        # {absolute line: obs} of the rows on view; absolute line 0 is the oldest history row
        test_screen_update_cell_data(s)
        top = s.historybuf.count - s.scrolled_by
        return {top + y: self.obs(s, y) for y in range(s.lines)}

    def sweep(self, s, positions):
        # {absolute line: {obs: [scrolled_by]}} over the scroll positions, back at the bottom afterwards
        seen = {}
        for sb in positions:
            s.scroll(s.historybuf.count, False)
            if sb:
                s.scroll(sb, True)
            self.ae(s.scrolled_by, min(sb, s.historybuf.count))
            for line, ob in self.view_obs(s).items():
                seen.setdefault(line, {}).setdefault(ob, []).append(sb)
        s.scroll(s.historybuf.count, False)
        return seen

    def settled(self, seen):
        # the one obs of each line, failing when a line is laid out differently at two scroll positions
        for line, by_obs in seen.items():
            self.ae(len(by_obs), 1, f'line {line} differs by scroll position: {dict(enumerate(by_obs.values()))}')
        return {line: next(iter(by_obs)) for line, by_obs in seen.items()}

    def test_continuation_group_relaid_when_the_row_above_is_edited(self):
        # The group's extent is part of the cache key: a row below the edited one is clean, but its paragraph changes with the edit
        n = 40
        ident = tuple(range(n))
        H2 = 'שורה שניה'
        joined = (0, 1) + tuple(range(10, 1, -1)) + ident[11:], (2, 2) + (3,) * 9 + (0,) * (n - 11), False
        own = ident[::-1], (1,) * n, True
        row1 = ident, None, False
        for form in ('plain', 'fish', 'tmux'):
            with self.subTest(form=form):
                s = self.replay_bytes(self.shell_rows(form, ('hello', H2)))
                self.assert_obs(self.obs(s, 1), *joined, 'before')
                # a plain space over the cell of the space and the FSI: row 2 is not written
                parse_bytes(s, b'\x1b[1;2H ')
                test_screen_update_cell_data(s)
                self.assert_obs(self.obs(s, 0), *row1, 'FSI removed, row 1')
                self.assert_obs(self.obs(s, 1), *own, 'FSI removed, row 2')
                parse_bytes(s, b'\x1b[1;2H \xe2\x81\xa8')
                test_screen_update_cell_data(s)
                self.assert_obs(self.obs(s, 1), *joined, 'FSI back')

    def test_continuation_group_scrolled_into_history(self):
        # The opener moves into the history by linefeeds; the rows still on screen keep their layout, and unchanged frames resolve nothing
        n = 40
        ident = tuple(range(n))
        H2, H3 = 'שורה שניה', 'עוד שורה'
        first = ident, (0, 0, 2, 2, 2, 2, 2) + (0,) * (n - 7), False
        second = (0, 1) + tuple(range(10, 1, -1)) + ident[11:], (2, 2) + (3,) * 9 + (0,) * (n - 11), False
        third = tuple(range(9, -1, -1)) + ident[10:], (3,) * 10 + (0,) * (n - 10), False
        for form in ('plain', 'tmux'):
            with self.subTest(form=form):
                s = self.screen(cols=n, lines=4, scrollback=20, pixel_scroll=False)
                parse_bytes(s, self.AUTO + self.shell_rows(form, ('hello', H2, H3)))
                for linefeeds in range(5):
                    if linefeeds:
                        parse_bytes(s, b'\r\n')
                    self.frame(s)
                    top = s.historybuf.count
                    self.ae(top, max(0, linefeeds - 1), 'linefeeds scrolled the rows into the history')
                    seen = self.view_obs(s)
                    for line, want in ((0, first), (1, second), (2, third)):
                        if line in seen:
                            self.assert_obs(seen[line], *want, f'{linefeeds} linefeeds, line {line}')
                    self.ae(self.frame(s), (0, 0), 'an unchanged frame resolves nothing')
                # every position of the view sees the same layout of the three rows
                settled = self.settled(self.sweep(s, range(s.historybuf.count + 1)))
                for line, want in ((0, first), (1, second), (2, third)):
                    self.assert_obs(settled[line], *want, f'line {line}')
        with self.subTest('opener evicted from the history'):
            # a ring of three history rows: after three linefeeds it holds the opener and the two rows, after four the opener is gone
            # and the rows revert to a paragraph each, the Hebrew ones auto-RTL
            own = ident[::-1], (1,) * n, True
            for linefeeds, want in ((3, (first, second, third)), (4, (own, own))):
                with self.subTest(linefeeds=linefeeds):
                    s = self.screen(cols=n, lines=3, scrollback=3, pixel_scroll=False)
                    parse_bytes(s, self.AUTO + self.shell_rows('plain', ('hello', H2, H3)) + b'\r\n' * linefeeds)
                    self.ae(s.historybuf.count, 3)
                    s.scroll(3, True)
                    seen = self.view_obs(s)
                    for line, ob in enumerate(want):
                        self.assert_obs(seen[line], *ob, f'{linefeeds} linefeeds, line {line}')

    def test_pdi_typed_on_a_continuation_row_ends_the_group(self):
        n = 40
        ident = tuple(range(n))
        H2, H3 = 'שורה שניה', 'עוד שורה'
        # the PDI joins the cell of the last letter, which keeps its level
        second = (0, 1) + tuple(range(10, 1, -1)) + ident[11:], (2, 2) + (3,) * 9 + (0,) * (n - 11), False
        third = ident[::-1], (1,) * n, True
        for form in ('plain', 'tmux'):
            with self.subTest(form=form):
                s = self.replay_bytes(self.shell_rows(form, ('hello', H2 + self.PDI, H3)))
                self.assert_obs(self.obs(s, 1), *second, 'row 2')
                self.assert_obs(self.obs(s, 2), *third, 'row 3')

    def group_screen(self, continuation_rows):
        # three plain rows, a prompt row with a continuation_rows Hebrew rows under its FSI, a prompt closing its isolate on its own
        # row, then twelve Hebrew rows. 20 columns, 10 lines: all but the last rows are in the history.
        s = self.screen(cols=20, lines=10, scrollback=2000, pixel_scroll=False)
        parse_bytes(s, self.AUTO + b'one\r\ntwo\r\nthree\r\n' + self.PROMPT_ROW + b'hello')
        parse_bytes(s, (b'\r\n\x1b[2C' + 'שורה'.encode()) * continuation_rows)
        parse_bytes(s, b'\r\n' + self.PROMPT_ROW + b'x' + self.PDI.encode())
        parse_bytes(s, (b'\r\n' + 'שורה'.encode()) * 12)
        return s

    def test_group_extent_independent_of_scroll_position(self):
        # A group is capped at BIDI_MAX_PARAGRAPH_ROWS rows from its opener, whatever row the walker starts from: every row of the
        # group has the same layout at every scroll position, across the 128 and 256 row marks of the window
        n = 20
        ident = tuple(range(n))
        cap = BIDI_MAX_PARAGRAPH_ROWS
        first_cont = (0, 1, 5, 4, 3, 2) + ident[6:], (2, 2, 3, 3, 3, 3) + (0,) * (n - 6), False
        later_cont = (5, 4, 3, 2, 1, 0) + ident[6:], (3,) * 6 + (0,) * (n - 6), False
        standalone = ident[::-1], (1,) * n, True
        prompt = 3  # the absolute line of the prompt row
        for rows in (cap // 2 + 2, cap + 44):
            with self.subTest(continuation_rows=rows):
                s = self.group_screen(rows)
                settled = self.settled(self.sweep(s, range(s.historybuf.count + 1)))
                self.ae(len(settled), s.historybuf.count + s.lines)
                self.assert_obs(settled[prompt], ident, (0, 0, 2, 2, 2, 2, 2) + (0,) * (n - 7), False)
                self.assert_obs(settled[prompt + 1], *first_cont, 'first continuation row')
                # the group is the prompt row and cap - 1 rows below it
                last_in_group = prompt + min(rows, cap - 1)
                for line in range(prompt + 2, last_in_group + 1):
                    self.assert_obs(settled[line], *later_cont, f'line {line}')
                # the rows after the cap are standalone auto-RTL paragraphs, as are the output rows after the closed isolate
                for line in range(last_in_group + 1, prompt + rows + 1):
                    self.assert_obs(settled[line], *standalone, f'line {line} is past the cap')
                closed = prompt + rows + 1
                for line in range(closed + 1, closed + 13):
                    self.assert_obs(settled[line], *standalone, f'output line {line}')

    def test_wrapped_prompt_below_open_group_starts_its_own_group(self):
        # The second prompt wraps and has its FSI on the chain's second row: the chain holds an opener, so no row of it joins the
        # group above, however the view cuts the chain
        n = 20
        ident = tuple(range(n))
        s = self.screen(cols=n, lines=4, scrollback=50, pixel_scroll=False)
        parse_bytes(s, self.AUTO + self.PROMPT_ROW + b'hello')
        parse_bytes(s, (b'\r\n\x1b[2C' + 'שורה'.encode()) * 3)
        parse_bytes(s, b'\r\n' + b'p' * n + b' \xe2\x81\xa8' + 'שלום'.encode() + b'\r\n\x1b[2C' + 'שורה'.encode())
        parse_bytes(s, b'\r\nabc' * 6)
        settled = self.settled(self.sweep(s, range(s.historybuf.count + 1)))
        group = (0, 1, 5, 4, 3, 2) + ident[6:], (2, 2, 3, 3, 3, 3) + (0,) * (n - 6), False
        self.assert_obs(settled[1], *group, 'first continuation row of the first group')
        for line in (2, 3):
            self.assert_obs(settled[line], (5, 4, 3, 2, 1, 0) + ident[6:], (3,) * 6 + (0,) * (n - 6), False, f'line {line}')
        self.assert_obs(settled[4], ident, None, False, 'first row of the wrapped chain')
        # the wrapped chain's FSI isolate starts at its Hebrew word, which is RTL; the indent row is inside it
        self.assert_obs(settled[5], (0, 4, 3, 2, 1) + ident[5:], (0, 1, 1, 1, 1) + (0,) * (n - 5), False, 'second row of the wrapped chain')
        self.assert_obs(settled[6], (5, 4, 3, 2, 1, 0) + ident[6:], (1,) * 6 + (0,) * (n - 6), False, 'continuation of the wrapped chain')
        for line in range(7, 13):
            self.assert_obs(settled[line], ident, None, False, f'line {line}')

    def test_history_without_marks_with_many_prompts(self):
        # More prompt and output cycles than BIDI_MAX_ISOLATES, no OSC 133 marks (as through tmux, but the rows written in turn, not
        # placed by CUP). Every fourth command closes its isolate with a PDI after the command, every fourth from the third with a
        # PDI at column 0 of the output row (what a shell printing it when the command runs sends), the others leave it open. The
        # groups do not chain across prompts, so the live prompt keeps the layout it has alone: its FSI is fed and its typed
        # space is kept.
        cycles = BIDI_MAX_PARAGRAPH_ROWS // 3 + BIDI_MAX_ISOLATES
        n = 24
        ident = tuple(range(n))
        s = self.screen(cols=n, lines=8, scrollback=500, pixel_scroll=False)
        parse_bytes(s, self.AUTO)
        for i in range(cycles):
            parse_bytes(s, self.PROMPT_ROW + b'ls' + (self.PDI.encode() if i % 4 == 0 else b'') + b'\r\n')
            parse_bytes(s, (self.PDI.encode() if i % 4 == 2 else b'') + 'שלום עולם'.encode() + b'\r\nfile.txt\r\n')
        parse_bytes(s, self.PROMPT_ROW + 'אני '.encode())
        self.assertGreater(s.historybuf.count, BIDI_MAX_PARAGRAPH_ROWS)
        y = s.cursor.y
        self.ae(s.cursor.x, 6)
        test_screen_update_cell_data(s)
        self.ae((s.test_bidi_v2l(y)[:7], self.lv(s, y)[:7]), ((0, 1, 5, 4, 3, 2, 6), (0, 0, 1, 1, 1, 1, 0)))
        self.assert_cursor_at_row(s, y, 6, (1, True), 1)
        for _ in range(2):
            self.ae(self.frame(s), (0, 0), 'an unchanged frame resolves and renders nothing')
        settled = self.settled(self.sweep(s, range(s.historybuf.count + 1)))
        standalone = ident[::-1], (1,) * n, True
        for i in range(cycles):
            self.assert_obs(settled[3 * i + 2], ident, None, False, f'cycle {i}, file.txt')
            if i % 4 in (0, 2):
                self.assert_obs(settled[3 * i + 1], *standalone, f'cycle {i}, output after a closed isolate')
            else:
                # an isolate left open: the output joins it until the next prompt's FSI starts another group (a shell that
                # closes the isolate when the command runs would get the layout above, as the PDI at column 0 does)
                self.assert_obs(settled[3 * i + 1], (8, 7, 6, 5, 4, 3, 2, 1, 0) + ident[9:], (3,) * 9 + (0,) * (n - 9), False, f'cycle {i}, output')

    def test_paused_frame_keeps_the_group_layout_it_can_see(self):
        n = 20
        ident = tuple(range(n))
        s = self.screen(cols=n, lines=6, scrollback=50, pixel_scroll=False)
        parse_bytes(s, self.AUTO + self.PROMPT_ROW + b'hello')
        for text in ('שלום', 'abc', 'עולם'):
            parse_bytes(s, b'\r\n\x1b[2C' + text.encode())
        english = ident, (2, 2, 2, 2, 2) + (0,) * (n - 5), False
        group = [
            (ident, (0, 0, 2, 2, 2, 2, 2) + (0,) * (n - 7), False),
            ((0, 1, 5, 4, 3, 2) + ident[6:], (2, 2, 3, 3, 3, 3) + (0,) * (n - 6), False),
            english,
            ((0, 1, 5, 4, 3, 2) + ident[6:], (2, 2, 3, 3, 3, 3) + (0,) * (n - 6), False),
        ]
        live = self.view_obs(s)
        for y, want in enumerate(group):
            self.assert_obs(live[y], *want, f'live row {y}')
        s.pause_rendering(True, 100000)
        paused = self.view_obs(s)
        self.ae(paused, live, 'the paused frame sees the opener')
        s.pause_rendering(False)
        self.ae(self.view_obs(s), live)

    def test_paused_frame_cut_through_a_group_reverts_to_rows(self):
        # The snapshot's top row is the group's third row: its opener is not in the snapshot, so each row is a paragraph of its own,
        # Hebrew rows auto-RTL and an English row after a Hebrew row LTR; the live rows keep the group
        n = 20
        ident = tuple(range(n))
        s = self.screen(cols=n, lines=4, scrollback=50, pixel_scroll=False)
        parse_bytes(s, self.AUTO + self.PROMPT_ROW + b'hello')
        for text in ('שלום', 'abc', 'עולם', 'עוד', 'def'):
            parse_bytes(s, b'\r\n\x1b[2C' + text.encode())
        self.ae(s.historybuf.count, 2)
        english = ident, (2, 2, 2, 2, 2) + (0,) * (n - 5), False
        live = [
            english,
            ((0, 1, 5, 4, 3, 2) + ident[6:], (2, 2, 3, 3, 3, 3) + (0,) * (n - 6), False),
            ((4, 3, 2, 1, 0) + ident[5:], (3,) * 5 + (0,) * (n - 5), False),
            english,
        ]
        own = [(ident, None, False), (ident[::-1], (1,) * n, True), (ident[::-1], (1,) * n, True), (ident, None, False)]
        for y, want in enumerate(live):
            self.assert_obs(self.view_obs(s)[2 + y], *want, f'live row {y}')
        s.pause_rendering(True, 100000)
        paused = self.view_obs(s)
        for y, want in enumerate(own):
            self.assert_obs(paused[2 + y], *want, f'paused row {y}')
        s.pause_rendering(False)
        for y, want in enumerate(live):
            self.assert_obs(self.view_obs(s)[2 + y], *want, f'unpaused row {y}')

    def test_english_isolate_rows_do_not_call_fribidi(self):
        # An isolate opened by the prompt and English rows below it have nothing to reorder, so no frame resolves anything
        for rows in (('hello', 'world'), ('hello', 'world', 'again'), ('hello world', '')):
            for form in ('plain', 'tmux'):
                with self.subTest(rows=rows, form=form):
                    s = self.replay_bytes(self.shell_rows(form, rows))
                    self.ae(s.test_bidi_counts()[0], 0)
                    parse_bytes(s, b'\x1b[2;3Hx' + self.PDI.encode() + b'\r\nabc')
                    self.ae(self.frame(s)[0], 0)
        with self.subTest('control'):
            s = self.replay_bytes(self.shell_rows('plain', ('hello', 'שלום')))
            self.assertGreater(s.test_bidi_counts()[0], 0)

    def test_continuation_under_a_hebrew_first_line(self):
        # Row 1 holds the FSI and a Hebrew word only. P2 skips the isolate, so row 1's paragraph is LTR, the isolate is RTL, and
        # row 2 inside it is right-to-left from its indent on.
        n = 40
        ident = tuple(range(n))
        for form in ('plain', 'tmux'):
            with self.subTest(form=form):
                data = self.shell_rows(form, ('שלום', 'שורה שניה')).replace(self.PROMPT_ROW, b' \xe2\x81\xa8')
                s = self.replay_bytes(data)
                self.assert_obs(self.obs(s, 0), (0, 4, 3, 2, 1) + ident[5:], (0, 1, 1, 1, 1) + (0,) * (n - 5), False, 'row 1')
                self.assert_obs(self.obs(s, 1), tuple(range(10, -1, -1)) + ident[11:], (1,) * 11 + (0,) * (n - 11), False, 'row 2')

    def test_beam_mirror_drawn_shape(self):
        # An unfocused cursor drawn as a block is not mirrored even when the cursor shape is a beam
        s = self.screen(cursor_shape_unfocused=CURSOR_BLOCK)
        s.draw(MIXED)
        s.cursor_position(1, 4)
        test_screen_update_cell_data(s)
        self.ae(s.test_cursor_render(CURSOR_BEAM, True), (5, True))
        self.ae(s.test_cursor_render(CURSOR_BEAM, False), (5, False))
        for focused in (False, True):
            s.test_cursor_render_info(CURSOR_BEAM, focused)
            test_screen_update_cell_data(s)
            self.ae(s.test_cursor_render_info(), (5, focused))

    def preedit_reference(self, text, x, preedit='xy'):
        # Rows a screen that never sent CSI 8 h uploads for text, cursor at x, without and with the pre-edit
        f = self.create_screen(cols=10, lines=5, scrollback=5, options={})
        draw_coloured(f, text)
        f.cursor_position(1, x + 1)
        plain = self.uploaded(f, 0)
        self.assertTrue(any(self.sprites(plain)))
        f.test_update_overlay_text(preedit)
        return plain, self.uploaded(f, 0)

    def test_cache_overlay_row(self):
        # an overlay line on a Hebrew row, then removed: the row keeps its map and every frame resolves nothing,
        # except in the gap
        gap_v2l = (0, 1, 2, 7, 6, 5, 4, 3, 8, 9)
        # the overlay frame dirties the row, and while the gap applies the row is unsigned, so a dirty frame resolves
        for x, want in (
            (7, [(0, MIXED_V2L)] * 4),  # not past empty cells: the cursor has no gap
            (9, [(1, gap_v2l), (0, gap_v2l), (0, gap_v2l), (1, gap_v2l)]),
        ):
            with self.subTest(x=x):
                s = self.screen()
                s.draw(MIXED)
                s.cursor_position(1, x)
                self.warm(s)
                seen = []
                for overlay, n in (('xy', 3), ('', 1)):
                    s.test_update_overlay_text(overlay)
                    seen += [(self.frame(s)[0], self.v2l(s, 0)) for _ in range(n)]
                self.ae(seen, want)

    def test_preedit_rtl_paragraph(self):
        # The cursor in the blank at x 7 is left of the text; the pre-edit of n cells ends there and extends left (IME anchor at 3 - n)
        plain_ref, preedit_ref = self.preedit_reference(A + ' ' + A, 7)
        s = self.screen()
        parse_bytes(s, b'\x1b[?2501h')
        draw_coloured(s, A + ' ' + A)
        row0, v2l0 = self.uploaded(s, 0), self.v2l(s, 0)

        def caret():
            return s.test_ime_anchor_x(), s.test_cursor_render(CURSOR_BEAM)

        self.ae((v2l0, s.test_cursor_visual(7, 0, CURSOR_BEAM), s.test_ime_anchor_x()), (ID10[::-1], (2, True), 3))
        self.warm(s)
        resolves = s.test_bidi_counts()[0]
        s.test_update_overlay_text('xy')
        self.ae(caret(), (1, (3, False)))
        s.test_update_overlay_text('xyz')
        self.ae(caret(), (0, (3, False)))
        s.test_update_overlay_text('xy')
        row = self.uploaded(s, 0)
        # whole cells are compared: both screens intern the same colours in the same order, so the style indices match
        self.ae((self.v2l(s, 0), row[:1] + row[3:], row[1:3]), (v2l0, row0[:1] + row0[3:], preedit_ref[7:9]))
        self.ae(caret(), (1, (3, False)))
        for _ in range(3):
            self.frame(s)
        self.ae(s.test_bidi_counts()[0], resolves)
        s.test_update_overlay_text('')
        self.ae((self.uploaded(s, 0), self.v2l(s, 0)), (row0, v2l0))
        # a stamped row keeps its record, so it stays reordered after the inverted-direction mode is reset
        parse_bytes(s, b'\x1b[8l')
        self.ae((self.sprites(self.uploaded(s, 0)), self.v2l(s, 0)), (self.sprites(row0), ID10[::-1]))

    def test_preedit_in_other_rows(self):
        # LTR cell: the cursor on b follows a Latin letter, the pre-edit block starts at column 1 and extends right
        _, preedit_ref = self.preedit_reference('abc' + A, 1)
        s = self.screen()
        draw_coloured(s, 'abc' + A)
        s.cursor_position(1, 2)
        row0 = self.uploaded(s, 0)
        self.ae((self.v2l(s, 0), s.test_cursor_visual(1, 0, CURSOR_BEAM)), (MIXED_V2L, (1, False)))
        s.test_update_overlay_text('xy')
        self.ae((s.test_ime_anchor_x(), s.test_cursor_render(CURSOR_BEAM)), (1, (3, False)))
        row = self.uploaded(s, 0)
        self.ae((self.v2l(s, 0), row[:1] + row[3:], row[1:3]), (MIXED_V2L, row0[:1] + row0[3:], preedit_ref[1:3]))
        # the pre-edit leaves the line record alone
        s = self.screen()
        parse_bytes(s, b'\x1b[?2501h')
        self.fill(s, ['abc def'])
        self.frame(s)
        r0 = s.test_bidi_line_record(0)
        for text in ('\u200fxy', ''):
            s.test_update_overlay_text(text)
            self.frame(s)
        f = self.screen()
        parse_bytes(f, b'\x1b[?2501h')
        f.draw('abc def')
        self.frame(f)
        self.ae((s.test_bidi_line_record(0), self.vmaps(s)), (r0, self.vmaps(f)))
        # A Latin row has no map: the pre-edit row is uploaded as a screen without bidi uploads it
        _, preedit_ref = self.preedit_reference('abc def', 7)
        s = self.screen()
        parse_bytes(s, b'\x1b[?2501h')
        draw_coloured(s, 'abc def')
        self.frame(s)
        s.test_update_overlay_text('xy')
        self.ae((s.test_ime_anchor_x(), s.test_cursor_render(CURSOR_BEAM)), (7, (9, False)))
        self.ae((self.uploaded(s, 0), self.v2l(s, 0)), (preedit_ref, None))

    def test_ime_anchor_x(self):
        # the IME anchor is the visual column of the cursor, or of the pre-edit's start
        s = self.mixed()
        for x, want in ((0, 0), (3, 6), (6, 3), (8, 3)):  # x=8: the cursor gap, the empty cells join the run
            s.cursor_position(1, x + 1)
            test_screen_update_cell_data(s)
            self.ae(s.test_ime_anchor_x(), want, x)
        s = self.screen()
        parse_bytes(s, b'\x1b[2 k')
        s.draw(MIXED)
        s.cursor_position(2, 1)
        parse_bytes(s, b'ab\x1b[2K')
        self.ae(self.refresh(s, 1), ID10[::-1])
        s.cursor_position(2, 1)
        self.ae(s.test_ime_anchor_x(), 9)  # beam column 9, mirrored, +1 clamped to the last column
        # no map: the anchor is the cursor, or the overlay's start
        s = self.create_screen(cols=10, lines=5, scrollback=5, options={})
        s.draw(MIXED)
        s.cursor_position(1, 8)
        test_screen_update_cell_data(s)
        self.ae(s.test_ime_anchor_x(), 7)
        s.test_update_overlay_text('xy')
        test_screen_update_cell_data(s)
        self.ae(s.test_ime_anchor_x(), 7)
        s.cursor_position(1, 10)
        test_screen_update_cell_data(s)
        self.ae(s.test_ime_anchor_x(), 8)  # right-aligned: min(9, 10 - 2)
        # the pre-edit follows the cursor, drawn left of the hebrew word the typed text ends with
        s = self.screen()
        s.draw('ab אני ')
        test_screen_update_cell_data(s)
        self.ae(s.test_ime_anchor_x(), 3)  # beam column 2, mirrored, +1
        s = self.mixed()
        s.cursor_position(1, 4)
        test_screen_update_cell_data(s)
        s.pause_rendering(True, 1000)
        test_screen_update_cell_data(s)
        self.ae(s.test_ime_anchor_x(), 3)  # the live logical x
        s.cursor_position(1, 8)
        self.ae(s.test_ime_anchor_x(), 7)  # the live logical x, not the paused cursor's

    def test_preedit_block_clamped_left(self):
        # a pre-edit longer than the room left of the cursor starts at the left edge
        s = self.screen()
        parse_bytes(s, b'\x1b[?2501h')
        draw_coloured(s, A + ' ' + A)
        _, preedit_ref = self.preedit_reference(A + ' ' + A, 7, 'xyzw')  # the block is right-aligned there, at columns 6-9
        row0 = self.uploaded(s, 0)
        s.test_update_overlay_text('xyzw')
        row = self.uploaded(s, 0)
        self.ae((s.test_cursor_render(CURSOR_BEAM), s.test_ime_anchor_x()), ((4, False), 0))
        self.ae(row[:4], preedit_ref[6:10])
        self.ae((row[4:], self.v2l(s, 0)), (row0[4:], ID10[::-1]))

    # Isolate controls written at column 0, where no cell precedes them: kept as leading controls of the cell written next,
    # fed to FriBidi before its base char

    def test_leading_isolate_at_column_0(self):
        # FSI, then Hebrew and English: one first-strong isolate, RTL from its first letter. The paragraph stays LTR
        # (the FSI is skipped by P2), so cells after the text are at level 0 and the English stays in reading order
        n = 40
        tail = tuple(range(10, n))
        data = '⁨שלום world'.encode()
        for name, chunks in (('one write', (data,)), ('split after the control', ('⁨'.encode(), 'שלום world'.encode()))):
            with self.subTest(name):
                s = self.screen(cols=n, lines=2)
                parse_bytes(s, self.AUTO)
                for chunk in chunks:
                    parse_bytes(s, chunk)
                test_screen_update_cell_data(s)
                self.assert_obs(self.obs(s, 0), (5, 6, 7, 8, 9, 4, 3, 2, 1, 0) + tail, (1,) * 5 + (2,) * 5 + (0,) * (n - 10), False)
        with self.subTest('without the control, the auto direction takes over the row'):
            s = self.screen(cols=n, lines=2)
            parse_bytes(s, self.AUTO + 'שלום world'.encode())
            test_screen_update_cell_data(s)
            self.assert_obs(self.obs(s, 0), tuple(range(n - 1, 9, -1)) + (5, 6, 7, 8, 9, 4, 3, 2, 1, 0), None, True)

    def test_leading_isolate_two_panes(self):
        # A row of two panes as the tmux fork writes it with the initiator at column 0: the leftmost pane's FSI first, its text,
        # the PDI on the pane's last cell, then the border cell carrying the right pane's initiator. The paragraph is fixed LTR.
        # The left pane's first cell is inside its isolate: the pane is laid out right to left from its first letter, whatever
        # the right pane holds
        n = 20
        left = 'שלום worl' + 'd'
        for right, right_v2l in (('hello wor', tuple(range(11, 20))), ('שלום עולם', (19, 18, 17, 16, 15, 14, 13, 12, 11))):
            with self.subTest(right=right):
                data = b'\x1b[?2501l\x1b[1;1H' + '⁨'.encode() + left.encode() + '⁩'.encode() + '│⁨'.encode() + right.encode() + '⁩'.encode()
                s = self.replay_bytes(data, cols=n, lines=2)
                v2l, levels, _ = self.obs(s, 0)
                self.ae(v2l[:10], (5, 6, 7, 8, 9, 4, 3, 2, 1, 0))
                self.ae(levels[:10], (1,) * 5 + (2,) * 5)
                self.ae(v2l[10], 10)
                self.ae(v2l[11:], right_v2l)
        with self.subTest('an initiator after the first cell leaves it outside the isolate'):
            # the initiator written after the first cell, as tmux had to: ש is not in the isolate
            data = b'\x1b[?2501l\x1b[1;1H' + ('ש⁨' + left[1:] + '⁩│').encode()
            s = self.replay_bytes(data, cols=n, lines=2)
            self.ae(self.obs(s, 0)[0][:10], (0, 5, 6, 7, 8, 9, 4, 3, 2, 1))

    def test_leading_pdi_ends_prompt_group(self):
        # A PDI printed when the command runs lands at column 0 of the first output row. It closes the prompt's isolate and
        # starts the output's own paragraph, auto-RTL when it is Hebrew; without it the rows join the open isolate (#53)
        n = 40
        H1 = 'שלום'
        standalone = (tuple(range(n - 1, -1, -1)), (1,) * n, True)
        joined = ((3, 2, 1, 0) + tuple(range(4, n)), (3,) * 4 + (0,) * (n - 4), False)
        prompt = self.PROMPT_ROW + b'hello\r\n'
        PDI = self.PDI.encode()
        for name, chunks, lines, row, expect in (
            ('PDI, then the output', (prompt + PDI + H1.encode(),), 6, 1, standalone),
            ('PDI and the output in separate writes', (prompt + PDI, H1.encode()), 6, 1, standalone),
            ('PDI, then a second output row', (prompt + PDI + H1.encode() + b'\r\n' + H1.encode(),), 6, 2, standalone),
            ('PDI on the bottom row, which scrolls to make room', (prompt + b'\r\n' + PDI + H1.encode(),), 3, 2, standalone),
            ('no PDI keeps the old behaviour', (prompt + H1.encode(),), 6, 1, joined),
            ('the cursor leaves the row before any text', (prompt + PDI + b'\r\n' + H1.encode(),), 6, 2, joined),
            ('the cursor leaves the bottom row before any text, scrolling', (prompt + PDI + b'\r\n' + H1.encode(),), 2, 1, joined),
        ):
            with self.subTest(name):
                s = self.screen(cols=n, lines=lines)
                parse_bytes(s, self.AUTO)
                for chunk in chunks:
                    parse_bytes(s, chunk)
                test_screen_update_cell_data(s)
                self.assert_obs(self.obs(s, row), *expect)

    def lead_cell(self, s, y=0, x=0):
        # the chars of a cell, the isolate controls shown by name
        return ''.join(ch if ch not in ISOLATES else 'LRI RLI FSI PDI'.split()[ISOLATES.index(ch)] for ch in s.cpu_cells(y)[x]['text'])

    def test_leading_isolate_lifetime(self):
        # What happens to controls kept for column 0 when the row or the cell changes before or after the text arrives. Inside an
        # RLI the digits of '12 34' are laid out '34 12', so a surviving control shows in the layout
        R, PDI, N = '⁧', '⁩', '12 34'
        n = 12
        with_control, plain = self.maps((R + N).encode(), n, 1)[0], self.maps(N.encode(), n, 1)[0]
        self.assertNotEqual(with_control, plain)
        # (name, text, chars of cell 0, the layout the row has: 'control' as under the control, 'plain' as without, None unchecked)
        cases = (
            ('control then text', R + N, '1RLI', 'control'),
            ('text overwritten at column 0 without a control', R + N + '\x1b[1;1H' + N, '1', 'plain'),
            ('text overwritten with a control again', R + N + '\x1b[1;1H' + R + N, '1RLI', 'control'),
            ('line erased after', R + N + '\x1b[2K', '', None),
            ('cell erased before the text', 'x\x1b[1;1H' + R + '\x1b[K' + N, '1', 'plain'),
            ('row erased before the text', 'x\r' + R + '\x1b[2K' + N, '1', 'plain'),
            ('blank row erased before the text', R + '\x1b[2K' + N, '1', 'plain'),
            ('blank cell erased with ECH before the text', R + '\x1b[X' + N, '1', 'plain'),
            ('cursor moved away and back to the untouched cell', 'x\r' + R + '\x1b[1;5H\x1b[1;1H' + N, '1RLI', 'control'),
            ('a cell written elsewhere first', R + '\x1b[1;6Hx\x1b[1;1H' + N, '1', None),
            ('control repeated', R + R + N, '1RLIRLI', None),
            ('wide first cell takes none', R + '中' + N, '中', None),
            ('combining mark after the control', R + 'a\u0301', 'aRLI\u0301', None),
            ('a PDI after the cell closes the opener before it', R + '1' + PDI + '2 34', '1', 'plain'),
            ('a PDI after a matched pair is dropped', '\u2066\u2069' + '1' + PDI + '2 34', '1LRIPDI', None),
            ('an opener after the cell is dropped', R + '1' + '\u2066' + '2 34', '1RLI', None),
            ('lrm at column 0 keeps the g8 handling', LRM + N, '1', None),
            ('an embedding stays inert', '\u202b' + N, '1', None),
        )
        for name, text, chars, same in cases:
            with self.subTest(name):
                s = self.screen(cols=n, lines=3)
                parse_bytes(s, text.encode())
                self.ae(self.lead_cell(s), chars)
                if same is not None:
                    test_screen_update_cell_data(s)
                    self.ae((self.v2l(s, 0), self.levels(s, 0)), with_control if same == 'control' else plain)
        with self.subTest('text export puts the leading controls before the base char, and replaying it keeps them'):
            s = self.screen(cols=n, lines=3)
            parse_bytes(s, (R + N).encode())
            self.ae(str(s.line(0)), R + N)
            self.ae(s.line(0).as_ansi().replace('\x1b[m', ''), R + N)
            again = self.screen(cols=n, lines=3)
            parse_bytes(again, s.line(0).as_ansi().encode())
            test_screen_update_cell_data(s)
            test_screen_update_cell_data(again)
            self.ae((self.lead_cell(again), self.obs(again, 0)), (self.lead_cell(s), self.obs(s, 0)))
        with self.subTest('scrolled away before the text arrives'):
            s = self.screen(cols=n, lines=3)
            parse_bytes(s, b'a\r\nb\r\nx\r' + R.encode() + b'\n\x1b[3;1H' + N.encode())
            self.ae(self.lead_cell(s, 2), '1')
        for name, mode in (('', b''), (' in insert mode, where no write check runs first', b'\x1b[4h')):
            with self.subTest('controls kept for another row are dropped when a new control arrives' + name):
                s = self.screen(cols=n, lines=3)
                parse_bytes(s, R.encode() + b'\x1b[2;1H' + mode + R.encode() + N.encode())
                self.ae((self.lead_cell(s, 0), self.lead_cell(s, 1)), ('', '1RLI'))
        with self.subTest('alternate screen'):
            s = self.screen(cols=n, lines=3)
            parse_bytes(s, R.encode() + b'\x1b[?1049h' + N.encode())
            self.ae(self.lead_cell(s), '1')
        with self.subTest('bidi mode off: kept for isolate regions'):
            # With bidi mode off the row's isolate regions still lay out, so the control is kept for them
            s = self.create_screen(cols=n, lines=3, options={})
            parse_bytes(s, (R + N).encode())
            self.ae(self.lead_cell(s), '1RLI')
        with self.subTest('soft wrapped continuation keeps the control with the cell before'):
            s = self.screen(cols=4, lines=3)
            parse_bytes(s, ('אבגד' + R + 'ה').encode())
            self.ae((self.lead_cell(s, 0, 3), self.lead_cell(s, 1, 0)), ('דRLI', 'ה'))
        with self.subTest('the control follows its cell when characters are inserted'):
            s = self.screen(cols=n, lines=3)
            parse_bytes(s, (R + N).encode() + b'\x1b[1;1H\x1b[2@')
            self.ae(self.lead_cell(s, 0, 2), '1RLI')
        with self.subTest('a narrow default emoji with VS16 after the control is widened and drops the controls, as a wide cell does'):
            s = self.screen(cols=n, lines=3)
            parse_bytes(s, (R + '\u2600\ufe0f').encode())
            self.ae((self.lead_cell(s), s.cursor.x, str(s.line(0))[:2]), ('\u2600\ufe0f', 2, '\u2600\ufe0f'))
        with self.subTest('SARA AM widens a cell with leading controls, in one write and in a later one'):
            for chunks in ((R + 'ก\u0e33',), (R + 'ก', '\u0e33')):
                s = self.screen(cols=n, lines=3)
                for chunk in chunks:
                    parse_bytes(s, chunk.encode())
                self.ae((self.lead_cell(s, 0, 0), self.lead_cell(s, 0, 1), s.cursor.x), ('ก\u0e33', '', 2), chunks)
        with self.subTest('a wide char moved past a multiline multicell is not the cell the controls were kept for'):
            s = self.screen(cols=n, lines=3)
            parse_bytes(s, b'\x1b[2;1Hz\x1b[1;2H\x1b]66;s=2;X\x07\x1b[2;1H' + R.encode() + '中'.encode())
            self.ae(self.lead_cell(s, 1), 'z')
        with self.subTest('controls kept on the bottom row do not follow a scroll over a blank row'):
            s = self.screen(cols=n, lines=2)
            parse_bytes(s, b'x\r\n' + R.encode() + b'\n' + N.encode())
            self.ae(self.lead_cell(s, 1), '1')

    def test_leading_isolate_cap(self):
        # A leading control counts toward BIDI_MAX_ISOLATES like any other: RLI 12 34 PDI and 15 empty isolates, one over the cap
        # when a PDI is added, and a cell keeps at most BIDI_MAX_LEAD leading controls
        from kitty.fast_data_types import BIDI_MAX_LEAD

        body = '⁧12 34⁩ ' + 'b⁦⁩' * ((BIDI_MAX_ISOLATES - 2) // 2)
        for name, text, expect in (
            ('at the cap', body, (3, 4, 2, 0, 1)),
            ('over the cap', body + '⁩', (0, 1, 2, 3, 4)),
        ):
            with self.subTest(name):
                got = self.maps(text.encode(), 60, 1)[0][0]
                self.ae(tuple(range(5)) if got is None else got[:5], expect)
        with self.subTest('leading controls kept per cell'):
            s = self.screen(cols=12, lines=2)
            parse_bytes(s, ('⁦' * (BIDI_MAX_LEAD + 3) + 'a').encode())
            self.ae(len(s.cpu_cells(0)[0]['text']), 1 + BIDI_MAX_LEAD)

    # Isolate regions

    def alt(self, cols, lines=3):
        # the alternate screen, BDSM never sent
        s = self.create_screen(cols=cols, lines=lines, scrollback=0, options={})
        parse_bytes(s, b'\x1b[?1049h\x1b[H')
        return s

    def region_maps(self, s, y=0):
        # (v2l, levels) of screen row y after a frame
        test_screen_update_cell_data(s)
        v2l = s.test_bidi_v2l(self.rr(s, y))
        if v2l is not None:
            self.ae(sorted(v2l), list(range(s.columns)), 'v2l must be a bijection over the columns')
        return v2l, self.lv(s, y)

    def assert_region(self, s, y, v2l, levels, msg=''):
        self.ae(self.region_maps(s, y), (v2l, levels), msg)

    def assert_no_region(self, s, y=0, msg=''):
        self.ae(self.region_maps(s, y), (None, None), msg)

    def cursor_at(self, s, x, shape, y=0):
        return s.test_cursor_visual(x, y, shape)

    def test_region_vim_gutter(self):
        s = self.alt(20)
        parse_bytes(s, ('   12 ' + FSI + 'שלום world' + PDI).encode())
        self.assert_region(s, 0, REGION_GUTTER_V2L, REGION_GUTTER_LEVELS)

    def test_region_vim_split(self):
        s = self.alt(20)
        parse_bytes(s, ('1 ' + FSI + 'אבג d' + PDI + '  │2 ' + FSI + 'שלום' + PDI).encode())
        self.assert_region(
            s, 0, (0, 1, 6, 5, 4, 3, 2, 7, 8, 9, 10, 11, 15, 14, 13, 12, 16, 17, 18, 19), (0, 0, 1, 1, 1, 1, 2, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0)
        )

    def test_region_claude_code_no_pdi(self):
        s = self.alt(20)
        parse_bytes(s, CLAUDE_NO_PDI)
        self.assert_no_region(s)
        self.ae(self.cursor_at(s, 14, CURSOR_BEAM), (14, False))
        self.assertTrue(s.test_bidi_state()[4], 'the walker ran')

    def test_region_claude_code_field_end(self):
        s = self.alt(20)
        parse_bytes(s, CLAUDE_FIELD_END)
        self.assert_region(s, 0, (0, 1) + tuple(range(18, 1, -1)) + (19,), (0, 0) + (1,) * 17 + (0,))
        self.ae(self.cursor_at(s, 14, CURSOR_BEAM), (6, True))
        self.ae(self.cursor_at(s, 14, CURSOR_BLOCK)[0], 6)
        self.ae(s.line(0)[18], ' ' + PDI)

    def test_region_lazygit_box(self):
        s = self.alt(12)
        for y, row in enumerate(('┌' + '─' * 10 + '┐', '│' + FSI + 'שלום ab' + PDI + '   │', '└' + '─' * 10 + '┘')):
            parse_bytes(s, f'\x1b[{y + 1};1H{row}'.encode())
        test_screen_update_cell_data(s)
        self.assert_region(s, 1, (0, 6, 7, 5, 4, 3, 2, 1, 8, 9, 10, 11), (0, 1, 1, 1, 1, 1, 2, 2, 0, 0, 0, 0))
        self.assert_no_region(s, 0)
        self.assert_no_region(s, 2)

    def test_region_no_isolates_identity(self):
        s = self.alt(10)
        parse_bytes(s, 'שלום world'.encode())
        self.assert_no_region(s)
        self.assertFalse(s.test_bidi_state()[4])

    def test_region_nesting(self):
        s = self.alt(12)
        parse_bytes(s, ('x ' + FSI + 'אב ' + LRI + 'cd' + PDI + PDI).encode())
        self.assert_region(s, 0, (0, 1, 5, 6, 4, 3, 2) + ID(7, 12), (0, 0, 1, 1, 1, 2, 2) + (0,) * 5)
        # the nested RLI is fed: inert, b and ג would share the LRI's level 2 instead of an RTL run at 1 with ג at 2
        s = self.alt(12)
        parse_bytes(s, ('x ' + LRI + 'a ' + RLI + 'b ג' + PDI + PDI).encode())
        self.assert_region(s, 0, (0, 1, 2, 3, 6, 5, 4) + ID(7, 12), (0, 0, 0, 0, 2, 1, 1) + (0,) * 5)
        # the FSI's first strong skips the nested LRI, so its base is RTL from ש, not LTR from a
        s = self.alt(12)
        parse_bytes(s, ('x ' + FSI + LRI + 'ab' + PDI + 'שלום' + PDI).encode())
        self.assert_region(s, 0, (0, 1, 7, 6, 5, 4, 2, 3) + ID(8, 12), (0, 0, 2, 2, 1, 1, 1, 1) + (0,) * 4)
        self.ae(self.cursor_at(s, 4, CURSOR_BEAM), (5, True))

    def test_region_unmatched_pdi(self):
        s = self.alt(10)
        parse_bytes(s, ('a' + PDI + ' ' + FSI + 'אב' + PDI).encode())
        self.assert_region(s, 0, (0, 1, 3, 2) + ID(4, 10), (0, 0, 1, 1) + (0,) * 6)

    def test_region_after_unmatched_initiator(self):
        # A PDI closes the nearest open initiator, so an unmatched one does not swallow the pair after it: גד stays in place
        s = self.alt(10)
        parse_bytes(s, ('x' + FSI + 'גד ' + FSI + 'אב' + PDI).encode())
        self.assert_region(s, 0, (0, 1, 2, 3, 5, 4) + ID(6, 10), (0, 0, 0, 0, 1, 1) + (0,) * 4)

    def test_region_initiator_at_column_0(self):
        text = 'שלום x'
        for name, chunks in (
            ('one write', (FSI + text + PDI,)),
            ('initiator alone', (FSI, text + PDI)),
            ('sgr between', (FSI + '\x1b[31m' + text + PDI,)),
        ):
            with self.subTest(name):
                s = self.alt(10)
                parse_bytes(s, b'\x1b[2;1H')
                for chunk in chunks:
                    parse_bytes(s, chunk.encode())
                self.assert_region(s, 1, (5, 4, 3, 2, 1, 0) + ID(6, 10), (1, 1, 1, 1, 1, 2) + (0,) * 4)

    def test_region_column_0_one_cell_then_region(self):
        # The closing-PDI rule of leading controls cancels a one-cell isolate written at column 0 (a cell's leading controls hold no PDI), so cell 0
        # stays at level 0, unlike the same isolate at another column
        s = self.alt(10)
        parse_bytes(s, (FSI + 'א' + PDI + ' ' + FSI + 'בג' + PDI).encode())
        self.assert_region(s, 0, (0, 1, 3, 2) + ID(4, 10), (0, 0, 1, 1) + (0,) * 6)
        self.ae(s.line(0)[0], 'א')

    def test_region_column_0_continuation_row(self):
        s = self.alt(5)
        parse_bytes(s, b'abcdef')
        parse_bytes(s, ('\r' + FSI + 'אב' + PDI).encode())
        self.assert_region(s, 1, (1, 0, 2, 3, 4), (1, 1, 0, 0, 0))
        self.assert_no_region(s, 0)
        self.ae(s.line(0)[4], 'e')

    def test_region_over_isolate_cap(self):
        def t(k):
            return 'x ' + FSI + LRI + 'ab' + PDI + 'ש' + (LRI + PDI) * 8 + 'ל' + (LRI + PDI) * (k - 8) + 'ו' + PDI

        def nested(k):
            return t(k).count(LRI) + t(k).count(PDI) - 1

        # the region's nested controls (all but its own FSI and PDI): fed at the cap, inert one pair over it
        self.ae((nested(15), nested(16)), (2 + 2 * 15, 2 + 2 * 16))
        self.assertTrue(nested(15) <= BIDI_MAX_ISOLATES < nested(16))
        with self.subTest('at the cap: fed, ab is an LTR isolate at 2 inside the RTL region'):
            s = self.alt(12)
            parse_bytes(s, t(15).encode())
            self.assert_region(s, 0, (0, 1, 6, 5, 4, 2, 3) + ID(7, 12), (0, 0, 2, 2, 1, 1, 1) + (0,) * 5)
            self.ae(self.cursor_at(s, 2, CURSOR_BEAM), (5, False))
        with self.subTest('over the cap: inert, ab stays at the paragraph level outside the reordered run'):
            s = self.alt(12)
            parse_bytes(s, t(16).encode())
            self.assert_region(s, 0, (0, 1, 2, 3, 6, 5, 4) + ID(7, 12), (0, 0, 0, 0, 1, 1, 1) + (0,) * 5)
            self.ae(self.cursor_at(s, 2, CURSOR_BEAM), (2, False))

    def test_region_adjacent_share_cell(self):
        s = self.alt(10)
        parse_bytes(s, ('x ' + FSI + 'אב' + PDI + FSI + 'גד' + PDI).encode())
        self.assert_region(s, 0, (0, 1, 3, 2, 5, 4) + ID(6, 10), (0, 0, 1, 1, 1, 1) + (0,) * 4)

    def test_region_memo_and_redraw(self):
        def fresh():
            s = self.alt(20)
            parse_bytes(s, REGION_GUTTER)
            self.assert_region(s, 0, REGION_GUTTER_V2L, REGION_GUTTER_LEVELS)
            return s

        with self.subTest('overwrite a cell inside'):
            s = fresh()
            # a Latin z re-resolves the region: z alone at level 2 shows the row was laid out again, not served from the memo
            parse_bytes(s, b'\x1b[1;9Hz')
            self.assert_region(s, 0, REGION_GUTTER_V2L, (0,) * 6 + (1, 1, 2, 1, 1, 2, 2, 2, 2, 2) + (0,) * 4)
        with self.subTest('overwrite the cell holding the PDI'):
            s = fresh()
            parse_bytes(s, b'\x1b[1;16He')
            self.assert_no_region(s)
        with self.subTest('overwrite the row without controls'):
            s = fresh()
            self.ae(s.test_bidi_line_record(0), (0, False, True))
            parse_bytes(s, ('\x1b[1;1H' + 'abc' + ' ' * 17).encode())
            self.assert_no_region(s)
            self.ae(s.test_bidi_line_record(0), (0, False, False))
        with self.subTest('overwrite the cell holding the initiator'):
            s = fresh()
            parse_bytes(s, b'\x1b[1;6H ')
            self.assert_no_region(s)

    def test_region_bdsm_on_unchanged(self):
        s = self.alt(20)
        parse_bytes(s, b'\x1b[8h' + CLAUDE_FIELD_END)
        self.assert_region(s, 0, (0, 1) + tuple(range(19, 1, -1)), (0, 0) + (1,) * 18)

    def test_region_full_screen_scroll(self):
        s = self.alt(20)
        for y, n in enumerate((12, 13, 14)):
            parse_bytes(s, f'\x1b[{y + 1};1H   {n} '.encode() + (FSI + 'שלום world' + PDI).encode())
        test_screen_update_cell_data(s)
        parse_bytes(s, b'\x1b[S')
        self.ae(self.frame(s)[0], 0)
        self.assert_region(s, 0, REGION_GUTTER_V2L, REGION_GUTTER_LEVELS)
        self.assert_region(s, 1, REGION_GUTTER_V2L, REGION_GUTTER_LEVELS)
        self.assert_no_region(s, 2)

    def test_region_cursor(self):
        with self.subTest('a: inside and outside a region'):
            s = self.alt(20)
            parse_bytes(s, REGION_GUTTER)
            test_screen_update_cell_data(s)
            self.ae(self.cursor_at(s, 17, CURSOR_BEAM), (17, False))
            self.ae(self.cursor_at(s, 16, CURSOR_BEAM), (16, False))
            self.ae(self.cursor_at(s, 6, CURSOR_BEAM), (15, True))
            self.ae(self.cursor_at(s, 6, CURSOR_BLOCK)[0], 15)
        with self.subTest('b: all-rtl region, cursor right after it'):
            s = self.alt(10)
            parse_bytes(s, ('x ' + FSI + 'שלום' + PDI).encode())
            self.assert_region(s, 0, (0, 1, 5, 4, 3, 2) + ID(6, 10), (0, 0) + (1, 1, 1, 1) + (0,) * 4)
            self.ae(self.cursor_at(s, 6, CURSOR_BEAM), (6, False))
            self.ae(self.cursor_at(s, 6, CURSOR_BLOCK)[0], 6)
        with self.subTest('c: region base differs from the row base'):
            s = self.alt(14)
            parse_bytes(s, ('x ' + RLI + 'אב cd' + '\x1b[1;13H' + PDI).encode())
            self.assert_region(s, 0, (0, 1, 11, 10, 9, 8, 7, 5, 6, 4, 3, 2, 12, 13), (0, 0, 1, 1, 1, 2, 2, 1, 1, 1, 1, 1, 0, 0))
            self.ae(self.cursor_at(s, 7, CURSOR_BEAM), (9, False))
            self.ae(self.cursor_at(s, 7, CURSOR_BLOCK)[0], 9)

    def test_region_main_screen_bdsm_off(self):
        s = self.create_screen(cols=20, lines=3, scrollback=0, options={})
        parse_bytes(s, REGION_GUTTER)
        self.assert_region(s, 0, REGION_GUTTER_V2L, REGION_GUTTER_LEVELS)

    def test_region_ime_overlay_leaves_no_trace(self):
        # pre-edit text holding isolate controls is drawn with BDSM off, and must leave the real rows as they were
        s = self.create_screen(cols=20, lines=3, scrollback=0, options={})
        parse_bytes(s, b'ab\r\n\r\n')
        before = [str(s.line(y)) for y in range(3)]
        for y, x in ((0, 2), (1, 0)):
            with self.subTest(cursor=(x, y)):
                s.cursor.x, s.cursor.y = x, y
                s.test_update_overlay_text(FSI + 'שלום' + PDI)
                test_screen_update_cell_data(s)
                s.test_update_overlay_text('')
                test_screen_update_cell_data(s)
                self.ae([str(s.line(r)) for r in range(3)], before)
                s.cursor.x, s.cursor.y = 0, 1
                parse_bytes(s, b'\x1b[2;1Hz')
                self.ae(str(s.line(1)).rstrip(), 'z')
                self.assertNotIn(FSI, str(s.line(1)))
                parse_bytes(s, b'\x1b[2;1H\x1b[K')
        self.ae(str(s.line(0)).rstrip(), 'ab')

        def pending_lead(overlay):
            s = self.create_screen(cols=20, lines=3, scrollback=0, options={})
            parse_bytes(s, ('\x1b[8h\r' + RLI).encode())
            if overlay:
                s.test_update_overlay_text('x')
                test_screen_update_cell_data(s)
                s.test_update_overlay_text('')
                test_screen_update_cell_data(s)
            parse_bytes(s, ('ab אב' + PDI).encode())
            return str(s.line(0))

        with self.subTest('a: the overlay keeps the pending lead'):
            # the RLI written at column 0 still becomes cell 0's leading control after the overlay comes and goes
            self.ae((pending_lead(True), pending_lead(False)[:2]), (pending_lead(False), RLI + 'a'))
        with self.subTest('b: a lone FSI pre-edit leaves no control behind'):
            s = self.create_screen(cols=20, lines=3, scrollback=0, options={})
            s.cursor.x, s.cursor.y = 0, 1
            s.test_update_overlay_text(FSI)
            test_screen_update_cell_data(s)
            s.test_update_overlay_text('')
            test_screen_update_cell_data(s)
            parse_bytes(s, b'\x1b[2;1Hz')
            self.ae(str(s.line(1)).rstrip(), 'z')
            self.assertNotIn(FSI, str(s.line(1)))

    def test_region_r7_copy(self):
        s = self.alt(20)
        parse_bytes(s, CLAUDE_FIELD_END)
        # str(line) keeps zero-width chars and prints an empty cell as a space: cells 17, 18 and 19
        self.ae(str(s.line(0))[-4:], '  ' + PDI + '│')

    def test_region_gained_after_frame(self):
        with self.subTest('a: a second region row after a frame with one'):
            s = self.alt(12)
            parse_bytes(s, ('x ' + FSI + 'אב' + PDI).encode())
            test_screen_update_cell_data(s)
            parse_bytes(s, ('\x1b[2;1Hy ' + FSI + 'גד' + PDI).encode())
            # the row written after the frame is laid out too, not only the rows of the first region frame
            self.assert_region(s, 1, (0, 1, 3, 2) + ID(4, 12), (0, 0, 1, 1) + (0,) * 8)
        with self.subTest('b: the first region row after a frame without isolates'):
            s = self.alt(12)
            parse_bytes(s, b'plain')
            test_screen_update_cell_data(s)
            parse_bytes(s, ('\x1b[2;1Hy ' + FSI + 'גד' + PDI).encode())
            # a screen whose frames had no isolates starts laying out regions once one is written
            self.assert_region(s, 1, (0, 1, 3, 2) + ID(4, 12), (0, 0, 1, 1) + (0,) * 8)

    def test_region_cursor_more(self):
        with self.subTest('a: outside every region, before and between regions'):
            s = self.alt(20)
            parse_bytes(s, ('1 ' + FSI + 'אבג d' + PDI + '  │2 ' + FSI + 'שלום' + PDI).encode())
            test_screen_update_cell_data(s)
            # cells outside the regions keep their own column and are not mirrored
            self.ae([self.cursor_at(s, x, CURSOR_BEAM) for x in (0, 1, 8, 9, 11)], [(x, False) for x in (0, 1, 8, 9, 11)])
        with self.subTest('b: main screen, BDSM never sent, a region on row 1'):
            s = self.create_screen(cols=12, lines=3, options={})
            parse_bytes(s, ('\x1b[2;1Hx ' + FSI + 'שלום' + PDI).encode())
            self.assert_region(s, 1, (0, 1, 5, 4, 3, 2) + ID(6, 12), (0, 0, 1, 1, 1, 1) + (0,) * 6)
            # the cursor on row 1 uses row 1's region: ש (logical 2) shows at visual 5 under an RTL base
            self.ae(self.cursor_at(s, 2, CURSOR_BEAM, y=1), (5, True))

    def test_region_flags_survive_resize_pause_reset(self):
        with self.subTest('a: RIS clears the flags'):
            s = self.alt(12)
            parse_bytes(s, ('x ' + FSI + 'שלום' + PDI).encode())
            self.ae(s.test_bidi_regions(), (False, True))
            parse_bytes(s, b'\x1bc')
            # RIS is the only reset of the flag
            self.ae(s.test_bidi_regions(), (False, False))
        with self.subTest('b: resize with rewrap keeps the flag'):
            s = self.create_screen(cols=12, lines=3, options={})
            parse_bytes(s, ('x ' + FSI + 'שלום' + PDI).encode())
            test_screen_update_cell_data(s)
            s.resize(3, 16)
            # the rewrapped linebuf carries the flag, so its rows are still laid out as regions
            self.ae(s.test_bidi_regions()[0], True)
            self.assert_region(s, 0, (0, 1, 5, 4, 3, 2) + ID(6, 16), (0, 0, 1, 1, 1, 1) + (0,) * 10)
            parse_bytes(s, ('\x1b[2;1Hy ' + FSI + 'גד' + PDI).encode())
            self.assert_region(s, 1, (0, 1, 3, 2) + ID(4, 16), (0, 0, 1, 1) + (0,) * 12)
        with self.subTest('c: the paused snapshot carries the flag'):
            # no live frame runs before the pause, so only the snapshot's own copy of the flag gives the paused frame a region
            s = self.alt(12)
            parse_bytes(s, ('x ' + FSI + 'שלום' + PDI).encode())
            s.pause_rendering(True, 1000)
            self.assert_region(s, 0, (0, 1, 5, 4, 3, 2) + ID(6, 12), (0, 0, 1, 1, 1, 1) + (0,) * 6)
            s.pause_rendering(False)
            self.assert_region(s, 0, (0, 1, 5, 4, 3, 2) + ID(6, 12), (0, 0, 1, 1, 1, 1) + (0,) * 6)

    def test_region_column_0_wide_first_char(self):
        # Known limitation, not the intended layout: a wide cell cannot hold leading controls, so the FSI is dropped and the
        # region is lost
        s = self.alt(12)
        parse_bytes(s, (FSI + '中 שלום' + PDI).encode())
        self.assert_no_region(s)
        self.ae(s.cpu_cells(0)[0]['text'], '中')

    def test_region_column_0_wide_cluster_intact(self):
        # A control kept for column 0 must not split the cluster of a wide first char
        def cursor_x(text):
            s = self.alt(10)
            parse_bytes(s, text.encode())
            return s.cursor.x

        with self.subTest('a: VS15 still narrows the emoji'):
            self.ae(cursor_x(RLI + '😀︎z' + PDI), cursor_x('😀︎z' + PDI))
        with self.subTest('b: ZWJ sequence'):
            s = self.alt(10)
            parse_bytes(s, (RLI + '👨‍👩').encode())
            self.ae(s.cpu_cells(0)[0]['text'], '👨‍👩')

    def test_region_osc66_trigger(self):
        # Controls inside OSC 66 cells form a region as plain writes do; b is L at level 2 in the RTL region
        s = self.alt(10)
        parse_bytes(s, ('\x1b]66;w=1;a' + FSI + '\x07' + 'שלום' + '\x1b]66;w=1;b' + PDI + '\x07').encode())
        self.assert_region(s, 0, (0, 5, 4, 3, 2, 1) + ID(6, 10), (0, 1, 1, 1, 1, 2) + (0,) * 4)

    def test_region_rules_skip_stamped_rows(self):
        s = self.alt(12)
        parse_bytes(s, ('\x1b[8h' + 'ab' + RLI + 'שלום').encode())
        parse_bytes(s, ('\x1b[8l\x1b[1;9H' + PDI + 'xyz').encode())
        # a stamped row drops a control written with BDSM off after an empty cell instead of keeping it as a region's PDI
        self.assertNotIn(PDI, str(s.line(0)))
        parse_bytes(s, ('\x1b[1;1H' + FSI + 'q').encode())
        # nor does it keep one at column 0 as the next cell's leading control
        self.ae(s.line(0)[0], 'q')

    def test_region_memo_survives_record_0(self):
        s = self.alt(5)
        parse_bytes(s, ('x' + FSI + 'אב' + PDI + 'cd').encode())
        test_screen_update_cell_data(s)
        parse_bytes(s, b'e')
        test_screen_update_cell_data(s)
        parse_bytes(s, ('\b' + RLI + '́').encode())
        # stamping row 1's first cell for bidi must not wipe row 0's region memo
        self.assert_region(s, 0, (0, 2, 1, 3, 4), (0, 1, 1, 0, 0))
        self.ae(s.test_bidi_line_record(0), (0, False, True))
