#!/usr/bin/env python
# License: GPLv3

import os
import random
import shutil
import struct
import subprocess
from collections import namedtuple

from kitty.fast_data_types import get_options, test_screen_update_cell_data
from kitty.fonts.render import setup_for_testing

from .base import BaseTest, draw_multicell, parse_bytes

A = 'אבג'  # three Hebrew letters, bidi class R
ID10 = tuple(range(10))
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
        # a bidi control written normally is a combining char that never reaches FriBidi; one in an OSC 66 cell resolves as ! does
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

    def test_preedit_row_bypass(self):
        # the pre-edit row has no map and is uploaded in logical order, then has its map again once the overlay closes
        s = self.screen()
        draw_coloured(s, 'abc' + A)
        logical = [s.cell_style_idx(0, x) for x in range(6)]
        visual = [logical[v] for v in (0, 1, 2, 5, 4, 3)]
        self.ae((self.refresh(s, 0), styles(self.uploaded(s, 0))[:6]), ((0, 1, 2, 5, 4, 3) + ID10[6:], visual))
        s.test_update_overlay_text('xy')
        self.ae((self.refresh(s, 0), styles(self.uploaded(s, 0))[:6]), (None, logical))
        s.test_update_overlay_text('')
        self.ae((self.refresh(s, 0), styles(self.uploaded(s, 0))[:6]), ((0, 1, 2, 5, 4, 3) + ID10[6:], visual))
        self.assert_shaped(s, 0, self.fresh_sprites('abc' + A, cols=10))

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
            (A + ' 12', 6, 6),
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

    def test_cache_placeholder_and_preedit_rows_never_hit(self):
        # a paragraph with an image placeholder row is never resolved, whatever entries its rows had; the pre-edit row's
        # paragraph resolves in every frame while the overlay is shown
        s = self.paragraph('אבגדה', '(12ב')
        self.warm(s)
        self.ae((self.frame(s)[0], self.vmaps(s)[0] is not None), (0, True))  # the unchanged paragraph is cached
        self.put(s, 2, 1, 'ab' + chr(0x10EEEE))
        self.assertTrue(s.linebuf.is_continued(1))
        for _ in range(3):
            self.ae((self.frame(s)[0], self.vmaps(s)[:2]), (0, (None, None)))
        s = self.paragraph('אבגדה', '(12ב')
        self.warm(s)
        s.test_update_overlay_text('xy')
        self.ae([self.frame(s)[0] > 0 for _ in range(3)], [True] * 3)
        s.test_update_overlay_text('')
        self.assertGreater(self.frame(s)[0], 0)
        self.ae(self.frame(s)[0], 0)
        f = self.paragraph('אבגדה', '(12ב')
        self.frame(f)
        self.ae(self.state(s), self.state(f))

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
        }
        for seqs in ((), (self.SCP_RTL,), (self.AUTO,)):
            for name, build in cases.items():
                with self.subTest(name, seqs=seqs):
                    screens = []
                    for trim in (True, False):
                        s = self.dscreen(*seqs)
                        s.test_bidi_trim(trim)
                        build(s)
                        screens.append(s)
                    for s in screens:
                        self.warm(s)
                    rtl = bool(seqs) and (self.SCP_RTL in seqs or name != 'LTR start')
                    self.ae([self.prtl(s, 0) for s in screens], [rtl, rtl])
                    self.ae(self.state(screens[0]), self.state(screens[1]))
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
        # the blank tail goes first, reversed, ahead of the reordered rest; trimming it changes neither maps nor levels
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

    DIFF_GROUPS = ('0123456789', 'אבגדהוזחט', 'abcdefghij', '()[]')
    DIFF_POOL = 'אבגדהוזחט' * 3 + 'abcdefghij' * 3 + '0123456789' * 2 + '  ' * 2 + '()!.'

    # extra (kinds, weights) of diff_op for streaming at the bottom, about 30% of the ops on top of the default ones
    STREAM_MIX = (('stream', 'streamk', 'su'), (20, 13, 10))

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

    def differential(self, seed, count, mix=None, follows=None):
        # follows (a list) gets a 1 for each frame that followed a scroll
        rng = random.Random(seed)

        def new():
            return self.screen(cols=8, lines=5, scrollback=20)

        s, applied, shortcuts, paused, frozen = new(), [], 0, False, 0
        for i in range(count):
            name, op = self.diff_op(rng, s, paused, first=i < 3, mix=mix)
            scrolled = s.scrolled_by
            op(s)
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
            self.ae((vs[exempt:], ls[exempt:]), (vf[exempt:], lf[exempt:]), f'seed {seed}, op {i} ({name})')
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
