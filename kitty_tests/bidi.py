#!/usr/bin/env python
# License: GPLv3

import os
import struct
import tempfile
from collections import namedtuple

from kitty.fast_data_types import CURSOR_BEAM, CURSOR_BLOCK, CURSOR_HOLLOW, CURSOR_UNDERLINE, test_screen_update_cell_data
from kitty.fonts.render import setup_for_testing, shape_string
from kitty.options.parse import parse_conf_item
from kitty.options.types import defaults

from .base import BaseTest, draw_multicell, parse_bytes
from .fonts import testing_font_data

A = 'אבג'  # three Hebrew letters, bidi class R
ID10 = tuple(range(10))
# a row whose map is not its own inverse: R R WS EN EN, N1 makes the space R
NSI = 'אב 12'
NSI_LEVELS = (1, 1, 1, 2, 2)
NSI_V2L = (3, 4, 2, 1, 0)
NSI_L2V = (4, 3, 2, 0, 1)
GPUCellData = namedtuple('GPUCellData', 'fg bg decoration_fg sprite_idx attrs')
GPU_CELL = struct.Struct('=5I')  # GPUCell in kitty/line.h, 20 bytes


def gpu_row(s, buf, y):
    # the buffer has an extra leading render row when pixel scroll is on
    row_size = s.columns * GPU_CELL.size
    start = (y + len(buf) // row_size - s.lines) * row_size
    return [GPUCellData(*c) for c in GPU_CELL.iter_unpack(buf[start : start + row_size])]


def draw_coloured(s, text):
    # each character gets its own 256-colour fg; returns the GPUCell.fg values, (index << 8) | 1
    for i, ch in enumerate(text):
        parse_bytes(s, f'\x1b[38;5;{i + 1}m{ch}'.encode())
    parse_bytes(s, b'\x1b[m')
    return [((i + 1) << 8) | 1 for i in range(len(text))]


class TestBidi(BaseTest):
    def screen(self, cols=10, lines=5, scrollback=5, **opts):
        o = {'bidi_reorder': True}
        o.update(opts)
        return self.create_screen(cols=cols, lines=lines, scrollback=scrollback, options=o)

    def refresh(self, s, y):
        s.test_bidi_refresh()
        v2l = s.test_bidi_v2l(y)
        if v2l is not None:
            self.ae(sorted(v2l), list(range(s.columns)), 'v2l must be a bijection over the columns')
        return v2l

    def setUp(self):
        self.enterContext(setup_for_testing())

    def uploaded(self, s, y):
        # GPUCells of screen row y after rendering a frame
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

    def dirty(self, s):
        return s.linebuf.dirty_lines()

    def frame(self, s):
        before = s.test_bidi_resolve_count()
        test_screen_update_cell_data(s)
        return s.test_bidi_resolve_count() - before

    def warm(self, s):
        for i in range(4):
            if self.frame(s) == 0:
                return
        self.ae(self.frame(s), 0, 'the screen never settled')

    def maps(self, s):
        return tuple(s.test_bidi_v2l(y) for y in range(s.lines))

    def fill(self, s, rows):
        for y, text in enumerate(rows):
            s.cursor_position(y + 1, 1)
            s.draw(text)
        s.cursor_position(1, 1)

    def prerender(self, s):
        # history lines stay dirty until first shown: show them all, come back
        for up in (True, False):
            s.scroll(s.historybuf.count, up)
            test_screen_update_cell_data(s)

    def plain_maps(self, rows, cols=10, lines=0):
        # the maps of a screen filled with rows, with nothing pushed into the history
        f = self.screen(cols=cols, lines=lines or len(rows))
        self.fill(f, rows)
        self.ae(f.historybuf.count, 0)
        self.frame(f)
        return self.maps(f)

    def trigger_case(self, build, trigger, expect=None, fresh=True):
        # maps after the trigger frame, equal to a fresh screen's (fresh=False: a first frame changes the view) and to expect when given
        s = build()
        self.frame(s)
        self.warm(s)
        trigger(s)
        self.assertGreater(self.frame(s), 0, 'the trigger must make the paragraph resolve again')
        got = self.maps(s)
        if fresh:
            f = build()
            trigger(f)
            self.frame(f)
            self.ae(got, self.maps(f))
        if expect is not None:
            self.ae(got, expect)
        return got

    def mixed(self, **opts):
        s = self.screen(**opts)
        s.draw('abc' + A)
        return s

    def nsi(self):
        s = self.screen(cols=5)
        s.draw(NSI)
        self.ae(self.refresh(s, 0), NSI_V2L)
        return s

    def selected_columns(self, s, start, end):
        # the visual columns lit on row 0 when logical cells start..end are selected; no other row may be lit
        s.clear_selection()
        s.start_selection(start, 0)
        s.update_selection(end, 0)
        s.test_bidi_refresh()
        raw = s.current_selections()
        offset = len(raw) // s.columns - s.lines
        self.assertIn(offset, (0, 1))
        rows = [[x for x, b in enumerate(raw[s.columns * (y + offset) : s.columns * (y + offset + 1)]) if b] for y in range(s.lines)]
        self.ae(rows[1:], [[]] * (s.lines - 1))
        return rows[0]

    def test_auto_direction_option_default(self):
        self.assertIs(defaults.bidi_auto_direction, False)
        ans = {}
        parse_conf_item('bidi_auto_direction', 'yes', ans)
        self.assertIs(ans['bidi_auto_direction'], True)

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
                self.ae((self.refresh(s, 0), s.test_bidi_levels(0)), (v2l, levels))
        # logical cell 1 (span 1) is at visual column 2; the emoji's span of 2 starts at its second cell, column 1
        s = self.drawn('א😀ב')
        self.refresh(s, 0)
        self.ae((s.test_bidi_visual_x(1, 0), s.test_bidi_visual_x(1, 0, 2)), (2, 1))

    def test_unflagged_continuation_and_history(self):
        # the continuation row (levels 2,2,1,2,2) has no RTL character of its own
        s = self.screen(cols=5, lines=5)
        s.draw('אבגדה12 34')
        self.ae((bool(s.line(1).as_ansi()), s.test_bidi_line_flag(1), s.test_bidi_line_flag(0)), (True, False, True))
        self.ae([self.refresh(s, 0), s.test_bidi_levels(1), self.refresh(s, 1)], [(4, 3, 2, 1, 0), (2, 2, 1, 2, 2), (3, 4, 2, 0, 1)])
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
        for what, text, opts in (('latin only', 'hello world', {}), ('option off', 'abc' + A, {'bidi_reorder': False})):
            with self.subTest(what):
                s = self.screen(**opts)
                s.draw(text)
                self.ae((s.test_bidi_line_flag(0), self.refresh(s, 0)), (False, None))
        self.assertIsNone(self.refresh(self.mixed(force_ltr=True), 0))  # force_ltr wins over bidi_reorder

    def test_flag_latch(self):
        # the flag is set when a reorderable codepoint is drawn and survives erasing it
        s = self.screen()
        s.draw('א')
        flags = [s.test_bidi_line_flag(0)]
        s.erase_in_line(0)
        flags.append(s.test_bidi_line_flag(0))
        s.cursor_position(1, 1)
        s.erase_in_line(2)
        self.ae((flags, s.test_bidi_line_flag(0), self.refresh(s, 0)), ([True, True], True, None))  # no map from the stale flag

    def test_flag_range_boundaries(self):
        # each side of the classifier's boundaries (zero-width ones never reach the flag test: the nearest drawable stands in); RLM: leading-mark path only
        flagged = (0x590, 0x8C9, 0xFB1D, 0xFDFF, 0xFE70, 0xFEFE, 0x10800, 0x10FFF, 0x1E800, 0x1EFFF)
        unflagged = (0x58F, 0x904, 0xFB1C, 0xFE10, 0xFE6F, 0xFF01, 0x107FF, 0x11003, 0x1E7FF, 0x1F000)
        for cp, want in [(cp, True) for cp in flagged] + [(cp, False) for cp in unflagged]:
            with self.subTest(hex(cp)):
                s = self.screen()
                s.draw(chr(cp))
                self.assertGreater(s.cursor.x, 0, 'codepoint did not draw')
                self.assertIs(s.test_bidi_line_flag(0), want)
        self.ae(self.screen().test_bidi_flag_misses(), [0x200F])

    def test_flag_named_scripts(self):
        # FriBidi 14: no assigned R/AL letter lies outside the flag; U+FEFD and U+FEFE are unassigned, AL by default
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

    def test_selection_and_inverse_maps(self):
        # NSI's map is not its own inverse: a swapped map lights visual 3 for logical 0 and visual 1 for logical 3
        s = self.nsi()
        self.ae([self.selected_columns(self.mixed(), 3, 4), self.selected_columns(s, 0, 0), self.selected_columns(s, 3, 3)], [[4, 5], [4], [0]])
        s = self.screen()
        s.draw('abc' + A + ' d')
        v2l = self.refresh(s, 0)
        levels = s.test_bidi_levels(0)
        for x in range(s.columns):
            vx = s.test_bidi_visual_x(x, 0)
            self.ae(
                (s.test_bidi_logical_x(vx, 0), vx, s.test_bidi_logical_x(x, 0), s.test_bidi_is_rtl(x, 0)), (x, v2l.index(x), v2l[x], bool(levels[x] & 1)), x
            )
        self.ae((s.test_bidi_visual_x(2, 3), s.test_bidi_logical_x(2, 3), s.test_bidi_is_rtl(2, 3)), (2, 2, False))
        s = self.nsi()
        self.ae(
            (s.test_bidi_levels(0), tuple(s.test_bidi_logical_x(x, 0) for x in range(5)), tuple(s.test_bidi_visual_x(x, 0) for x in range(5))),
            (NSI_LEVELS, NSI_V2L, NSI_L2V),
        )

    def test_upload_permutes_cells(self):
        # each visual cell is the logical cell v2l[visual], told apart by fg colour, on a map that is not its own inverse
        s = self.screen(cols=5)
        fgs = draw_coloured(s, NSI)
        self.ae([c.fg for c in self.uploaded(s, 0)], [fgs[NSI_V2L[v]] for v in range(5)])
        s = self.screen(force_ltr=True)
        fgs = draw_coloured(s, 'abc' + A)
        self.ae([c.fg for c in self.uploaded(s, 0)[: len(fgs)]], fgs)

    def test_widen_merges_flag_and_map(self):
        s = self.screen(cols=5, lines=5)
        s.draw('abcde' + A)
        self.ae(([s.test_bidi_line_flag(0), s.test_bidi_line_flag(1)], self.refresh(s, 1)), ([False, True], (2, 1, 0, 3, 4)))
        s.resize(5, 25)
        self.ae((s.test_bidi_line_flag(0), self.refresh(s, 0)), (True, tuple(range(5)) + (7, 6, 5) + tuple(range(8, 25))))

    def test_overlay_row_keeps_map(self):
        # the IME overlay row keeps its map, observed through the real update-cell-data path
        s = self.mixed(bidi_auto_direction=False)
        s.cursor_position(1, 9)
        test_screen_update_cell_data(s)
        before = s.test_bidi_v2l(0)
        s.test_update_overlay_text('xy')
        test_screen_update_cell_data(s)
        self.ae((before, s.test_bidi_v2l(0)), ((0, 1, 2, 5, 4, 3) + ID10[6:],) * 2)

    def test_option_off_at_runtime(self):
        # turning bidi off at runtime drops the maps on the next frame; a resize after that must leave the selection clean
        for opts in ({'bidi_reorder': False}, {'bidi_reorder': True, 'force_ltr': True}):
            s = self.mixed()
            self.ae(self.refresh(s, 0), (0, 1, 2, 5, 4, 3) + ID10[6:])
            self.set_options(opts)
            s.cursor_position(1, 1)
            s.erase_in_line(2)
            s.draw('hello')
            s.test_bidi_refresh()
            self.ae((s.test_bidi_v2l(0), [s.test_bidi_logical_x(x, 0) for x in range(s.columns)]), (None, list(ID10)), opts)
        s = self.mixed()
        self.refresh(s, 0)
        s.current_selections()
        self.set_options({'bidi_reorder': False})
        s.resize(5, 8)
        s.test_bidi_refresh()
        s.current_selections()
        self.assertFalse(s.is_selection_dirty())
        s.test_bidi_refresh()
        self.assertFalse(s.is_selection_dirty())

    def test_resize_narrower_drops_maps(self):
        # until the next frame, maps sized for the old width must not be used
        s = self.screen(cols=20, lines=3)
        s.draw(''.join(chr(0x5D0 + i) for i in range(20)))
        self.ae(self.refresh(s, 0), tuple(range(19, -1, -1)))
        s.resize(3, 10)
        self.ae([(s.test_bidi_logical_x(x, 0), s.test_bidi_visual_x(x, 0)) for x in range(10)], [(x, x) for x in range(10)])
        self.ae(self.refresh(s, 0), tuple(range(9, -1, -1)))

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

    def assert_shaped(self, s, y, fresh, buf=None, msg=''):
        # the glyphs of row y (in buf, or in a new frame) are those of its text shaped alone
        self.ae(self.sprites(gpu_row(s, test_screen_update_cell_data(s) if buf is None else buf, y)), fresh, msg)

    def test_latin_paragraph_dirties_only_written_row(self):
        # nothing to reorder: a write to one row of a soft-wrapped paragraph must not redraw the rest of it
        s = self.screen(cols=10, lines=8)
        s.draw('x' * 75)
        test_screen_update_cell_data(s)
        self.ae(self.dirty(s), [])
        s.draw('x')
        s.test_bidi_refresh()
        self.ae(self.dirty(s), [7])

    def test_level_change_redraws_paragraph(self):
        # losing the last RTL char: row 1, shaped at 2,2,1,2,2, must redraw at level 0
        s = self.paragraph('אבגד.', '12 34')
        test_screen_update_cell_data(s)
        self.ae((s.test_bidi_levels(1), self.dirty(s)), ((2, 2, 1, 2, 2), []))
        s.cursor_position(1, 1)
        s.draw('abcd')  # row 0 keeps its last cell, as writing it would split the paragraph
        self.ae((s.linebuf.is_continued(1), self.dirty(s)), (True, [0]))
        s.test_bidi_refresh()
        self.ae((s.test_bidi_v2l(1), self.dirty(s)), (None, [0, 1]))

    def test_split_paragraph_redraws_old_continuation(self):
        # once split, row 1 (N0 made the brackets of (12) R) has no RTL and no flag: redraw it plain
        s = self.paragraph('אבגדה', '(12)')
        test_screen_update_cell_data(s)
        self.ae((s.test_bidi_levels(1), s.test_bidi_line_flag(1), self.dirty(s)), ((1, 2, 2, 1, 0), False, []))
        self.split(s, ch='x')
        s.test_bidi_refresh()
        self.ae((s.test_bidi_v2l(1), self.dirty(s)), (None, [0, 1]))
        self.assert_shaped(s, 1, self.fresh_sprites('(12)'))

    def test_split_paragraph_reshapes_lower_part(self):
        # split changes row 1 to 0,0,0,1,0; a paused frame sees the change since the last live frame, a second pause keeps it
        for split_before_pause, paused_frames in ((False, 0), (False, 1), (False, 2), (True, 1), (True, 2)):
            with self.subTest(split_before_pause=split_before_pause, paused_frames=paused_frames):
                s = self.paragraph('אבגדה', '(12ב')
                test_screen_update_cell_data(s)
                self.ae((s.test_bidi_levels(1), s.test_bidi_line_flag(1), self.dirty(s)), ((1, 2, 2, 1, 0), True, []))
                fresh = self.fresh_sprites('(12ב')
                if split_before_pause:
                    self.split(s)
                if paused_frames:
                    s.pause_rendering(True, 1000)
                for i in range(paused_frames):
                    buf = test_screen_update_cell_data(s)
                    if split_before_pause:
                        self.ae(s.test_bidi_levels(1), (0, 0, 0, 1, 0))
                        self.assert_shaped(s, 1, fresh, buf)
                if not split_before_pause:
                    self.split(s)
                    if not paused_frames:
                        self.ae(self.dirty(s), [0])
                if paused_frames:
                    s.pause_rendering(False)
                self.ae((self.sprites(self.uploaded(s, 1)), s.test_bidi_levels(1)), (fresh, (0, 0, 0, 1, 0)))

    def beta_over_a(self, s, y, scrolled_by=0):
        # ב over row 3 turns the ! ending row 1 from N2 level 0 into N1 level 1: one re-resolve redraws row 1 (levels) and row 2 (written)
        count = s.test_bidi_resolve_count()
        s.cursor_position(3, 1)
        s.draw('ב')
        self.ae((s.scrolled_by, self.dirty(s)), (scrolled_by, [2]))
        s.test_bidi_refresh()
        self.ae((self.dirty(s), s.historybuf.dirty_lines(), s.test_bidi_resolve_count() - count, s.test_bidi_levels(y)), ([1, 2], [], 1, (2, 2, 1, 2, 1)))

    def test_dirty_row_redraws_only_rows_whose_levels_change(self):
        s = self.paragraph('אבגדה', '12 3!', 'abcde', 'fghij', 'klmno', lines=6)
        test_screen_update_cell_data(s)
        self.ae((s.test_bidi_levels(1), self.dirty(s)), ((2, 2, 1, 2, 0), []))
        self.beta_over_a(s, 1)
        buf = test_screen_update_cell_data(s)
        f = self.paragraph('אבגדה', '12 3!', 'בbcde', 'fghij', 'klmno', lines=6)
        fbuf = test_screen_update_cell_data(f)
        self.ae(f.test_bidi_levels(1), (2, 2, 1, 2, 1))
        want = [self.sprites(gpu_row(f, fbuf, y)) for y in range(5)]
        self.assertTrue(all(map(any, want)))
        self.ae([self.sprites(gpu_row(s, buf, y)) for y in range(5)], want)
        s.cursor_position(4, 2)
        s.draw('z')  # Latin over Latin changes no levels: only the written row redraws
        s.test_bidi_refresh()
        self.ae(self.dirty(s), [3])

    def test_paused_frame_redraws_only_rows_whose_levels_change(self):
        # only rows 0 (written) and 1 redraw; with pixel scroll the snapshot has a leading row, indices shift by one
        for pixel_scroll in (False, True):
            with self.subTest(pixel_scroll=pixel_scroll):
                s = self.paragraph('אבגדה', '(12ב', pixel_scroll=pixel_scroll)
                parse_bytes(s, '\r\nאב\r\nגד'.encode())
                test_screen_update_cell_data(s)
                self.assertTrue(all(s.test_bidi_levels(y) is not None for y in (1, 2, 3)))
                self.split(s)
                s.pause_rendering(True, 1000)
                s.test_bidi_refresh()
                self.ae(s.test_bidi_paused_dirty_lines(), [y + int(pixel_scroll) for y in (0, 1)])

    def test_line_scrolled_into_view_reshaped_after_split(self):
        # (12ב split while out of view: no state to compare, so it reshapes when scrolled back in
        for paused in (False, True):
            with self.subTest(paused=paused):
                s = self.screen(cols=5, lines=3, scrollback=10)
                parse_bytes(s, b'x\r\ny\r\n')
                s.draw('אבגדה(12ב')
                test_screen_update_cell_data(s)
                self.ae(s.test_bidi_levels(2), (1, 2, 2, 1, 0))
                s.scroll(1, True)
                test_screen_update_cell_data(s)
                self.ae(s.scrolled_by, 1)
                self.split(s, row=2)
                s.scroll(1, False)
                self.ae(s.scrolled_by, 0)
                if paused:
                    s.pause_rendering(True, 1000)
                self.ae((self.sprites(self.uploaded(s, 2)), s.test_bidi_levels(2)), (self.fresh_sprites('(12ב'), (0, 0, 0, 1, 0)))

    def test_paused_frame_row_continuing_from_history(self):
        # row 0 (12ב continues from history; the paused snapshot resolves row 0 alone at 0,0,0,1,0, the next live frame at 1,2,2,1,0
        s = self.paragraph('אבגדה', '(12ב', lines=3, scrollback=10, pixel_scroll=False)
        parse_bytes(s, b'\r\nx\r\ny')
        test_screen_update_cell_data(s)
        self.ae(s.test_bidi_levels(0), (1, 2, 2, 1, 0))
        s.pause_rendering(True, 1000)
        buf = test_screen_update_cell_data(s)
        self.ae(s.test_bidi_levels(0), (0, 0, 0, 1, 0))
        self.assert_shaped(s, 0, self.fresh_sprites('(12ב'), buf)
        s.pause_rendering(False)
        buf = test_screen_update_cell_data(s)
        self.assert_shaped(s, 0, self.sprites(self.uploaded(self.paragraph('אבגדה', '(12ב', lines=3), 1)), buf)

    def test_history_slot_reuse_reshapes_line(self):
        # X (א2 3() is pushed into O's slot; comparing X as O would skip reshaping since X's glyphs were shaped at O's levels
        s = self.screen(cols=5, lines=4, scrollback=0, pixel_scroll=False)
        s.draw('א2 3ב')  # O
        parse_bytes(s, b'\r\nh\r\ni\r\nj\r\n')
        s.draw('א2 3(abcde')  # X, W
        parse_bytes(s, b'\r\na\r\nb')
        test_screen_update_cell_data(s)
        self.ae(s.test_bidi_levels(0), (1, 2, 1, 2, 0))
        s.scroll(10, True)
        test_screen_update_cell_data(s)
        self.ae(s.test_bidi_levels(0), (1, 2, 1, 2, 1))
        s.cursor_position(2, 1)
        s.draw('ב')  # W: the ( of X becomes level 1
        s.cursor_position(4, 1)
        parse_bytes(s, b'\n')  # X into the slot of O
        buf = test_screen_update_cell_data(s)
        self.ae(s.test_bidi_levels(3), (1, 2, 1, 2, 1))
        self.assert_shaped(s, 3, self.fresh_sprites('א2 3(בbcde', pixel_scroll=False), buf)

    def test_full_ring_push_reshapes_only_reused_slots(self):
        # a full ring (4 slots) reuses only slot 0; the other lines keep their ids and levels, so only slot 0 is dirty
        def ring():
            s = self.screen(cols=5, lines=4, scrollback=0, pixel_scroll=False)
            parse_bytes(s, '\r\n'.join(['אב 12'] * 8).encode())
            test_screen_update_cell_data(s)
            self.prerender(s)
            return s

        def push(s, scroll):
            s.scroll(scroll, True)
            test_screen_update_cell_data(s)
            test_screen_update_cell_data(s)
            self.ae((s.scrolled_by, s.historybuf.dirty_lines(), s.test_bidi_levels(0)), (min(scroll, 4), [], (1, 1, 1, 2, 2)))
            s.cursor_position(4, 1)
            parse_bytes(s, b'\n')
            s.test_bidi_refresh()

        # the reused slot is off screen: row 0 shows slot 3, keeping its id and levels
        s = ring()
        push(s, 2)
        self.ae(s.historybuf.dirty_lines(), [0])
        # the reused slot is on screen: clean text and same levels, so only forgetting the slot's id marks it dirty
        s = ring()
        push(s, 10)
        self.ae(s.historybuf.dirty_lines(), [0])
        buf = test_screen_update_cell_data(s)
        fresh = self.fresh_sprites('אב 12', pixel_scroll=False)
        for y in range(4):
            self.assert_shaped(s, y, fresh, buf, f'row {y}')

    def test_history_slot_reuse_reshapes_line_in_paused_frame(self):
        # P is pushed into O's slot and X (continuation) into H's slot: the pause must forget those ids, else X is compared as H
        s = self.screen(cols=5, lines=4, scrollback=0, pixel_scroll=False)
        parse_bytes(s, b'o\r\n')
        s.draw('(12ב')  # H
        parse_bytes(s, b'\r\ni\r\nj\r\n')
        s.draw('אבגדה(12ב')  # P, X
        parse_bytes(s, b'\r\na\r\nb')
        test_screen_update_cell_data(s)
        self.ae(s.test_bidi_levels(1), (1, 2, 2, 1, 0))
        s.scroll(10, True)
        test_screen_update_cell_data(s)
        self.ae((s.scrolled_by, s.test_bidi_levels(1)), (4, (0, 0, 0, 1, 0)))
        self.split(s)  # P from X
        s.cursor_position(4, 1)
        parse_bytes(s, b'\n\n')  # P into the slot of O, X into the slot of H
        fresh = self.fresh_sprites('(12ב', pixel_scroll=False)
        s.pause_rendering(True, 1000)
        s.test_bidi_refresh()
        self.ae(s.test_bidi_paused_dirty_lines(), [2, 3])
        buf = test_screen_update_cell_data(s)
        self.ae(s.test_bidi_levels(3), (0, 0, 0, 1, 0))
        self.assert_shaped(s, 3, fresh, buf)

    def test_dirty_row_below_view_redraws_paragraph(self):
        # scrolled up one row: a dirty row below view re-resolves the paragraph; row 2 redraws, others keep their levels
        s = self.paragraph('אבגדה', 'אבגדה', '12 3!', 'abcde', lines=3, scrollback=10)
        test_screen_update_cell_data(s)
        s.scroll(1, True)
        test_screen_update_cell_data(s)
        self.ae((s.scrolled_by, self.dirty(s), s.test_bidi_levels(2)), (1, [], (2, 2, 1, 2, 0)))
        self.beta_over_a(s, 2, scrolled_by=1)

    def test_tall_paragraph_keeps_uploaded_maps(self):
        # 400 rows on 300 lines (100 in history): ! between R letters maps (1,0) except at the window end where it is (0,1)
        s = self.screen(cols=2, lines=300, scrollback=200)
        s.draw('א!' * 400)
        self.ae(s.historybuf.count, 100)
        s.test_bidi_refresh()
        maps = self.maps(s)
        self.ae(set(maps), {(0, 1), (1, 0)})
        self.ae([y for y, m in enumerate(maps) if m == (0, 1)], [155, 283, 299])

    ABC_A = (0, 1, 2, 5, 4, 3, 6, 7, 8, 9)
    EDGE_PARAGRAPH = 'אבג abאב 12ab'
    MIXED = 'alpha שלום beta עולם (12) gamma'[:30]
    MIXED_STATE = (  # (maps, levels)
        ((0, 1, 2, 3, 4, 5, 9, 8, 7, 6),) * 2 + ((4, 2, 3, 1, 0, 5, 6, 7, 8, 9), None),
        ((0,) * 6 + (1,) * 4,) * 2 + ((1, 1, 2, 2, 1, 0, 0, 0, 0, 0), None),
    )

    def state(self, s):
        return self.maps(s), tuple(s.test_bidi_levels(y) for y in range(s.lines))

    def filled(self, rows, **opts):
        s = self.screen(**opts)
        self.fill(s, rows)
        return s

    def drawn(self, text, **opts):
        s = self.screen(**opts)
        s.draw(text)
        return s

    def put(self, s, y, x, text):
        s.cursor_position(y, x)
        s.draw(text)

    def warmed(self, text, **opts):
        s = self.drawn(text, **opts)
        self.frame(s)
        self.warm(s)
        return s

    def history(self, rows, up=0):
        # 5x3 screen, one row per line, every history line rendered once
        s = self.screen(cols=5, lines=3, scrollback=10)
        for r in rows:
            parse_bytes(s, r.encode() + b'\r\n')
        self.prerender(s)
        if up:
            s.scroll(up, True)
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

    def toggle_drops_maps(self, text, v2l, rtl, **opts):
        # bidi_reorder off then on, force_ltr on then off: either drops the maps, undoing it must resolve again
        on = {'bidi_reorder': True, 'force_ltr': False, **opts}
        for off in ({**on, 'bidi_reorder': False}, {**on, 'force_ltr': True}):
            with self.subTest(off=off):
                s = self.warmed(text, **opts)
                self.ae(s.test_bidi_v2l(0), v2l)
                self.set_options(off)
                self.frame(s)
                self.assertIsNone(s.test_bidi_v2l(0), off)
                self.set_options(on)
                self.assertGreater(self.frame(s), 0, off)
                self.ae((s.test_bidi_v2l(0), s.test_bidi_paragraph_rtl(0), self.frame(s)), (v2l, rtl, 0), off)

    def test_cache_reuses_unchanged_paragraphs(self):
        # trigger: one dirty row makes only its own paragraph resolve again
        rows = ['אבג abc'] * 3
        s = self.filled(rows)
        self.ae([self.frame(s), self.frame(s)], [3, 0])
        before = self.maps(s)
        self.ae([m is None for m in before], [False] * 3 + [True] * 2)
        s.current_selections()
        self.assertFalse(s.is_selection_dirty())
        self.put(s, 2, 10, 'z')
        self.ae(self.frame(s), 1)
        after = self.maps(s)
        self.ae((after[0], after[2]), (before[0], before[2]))
        f = self.filled(rows)
        self.put(f, 2, 10, 'z')
        self.frame(f)
        self.ae(after, self.maps(f))
        self.ae(self.frame(s), 0)

    def test_cache_view_change_resolves_again(self):
        # every trigger changes which line a render row shows (or the layout) without a dirty row: no stamp may be trusted
        rows = ['אבג abc', 'abc אבג', 'אב', 'xyz', 'אבגדה']
        hist = ['אבג', 'abאב', 'אב12', 'xyz', 'אבגדה', 'a', 'אבג abc']
        long_row = 'אבגדהוזחטיכלמ'

        def net_zero_build():
            s = self.screen()
            parse_bytes(s, b'\x1b[?1049h')
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
        # (name, build, trigger, expected maps, compare with a fresh screen)
        cases = (
            ('index', lambda: self.filled(rows), lambda s: parse_bytes(s, b'\x1b[5;1H\x1bD'), self.plain_maps(rows[1:], lines=5), True),
            ('reverse index', lambda: self.filled(rows), lambda s: parse_bytes(s, b'\x1b[1;1H\x1bM'), self.plain_maps([''] + rows[:-1]), True),
            ('insert then delete line', net_zero_build, net_zero, (None, None, None, rev[3:], None), True),
            ('scrollback view', lambda: self.history(hist), up, self.plain_maps(hist[5:], 5, lines=3), False),
            ('view crossing bottom edge', bottom_build, up, (None, None, None, m[0], m[1]), False),
            ('view crossing top edge', top_build, down, (m[1], m[2], None, None, None), False),
            (
                'history add while scrolled',
                lambda: self.history(hist[:6], 2),
                lambda s: parse_bytes(s, '\x1b[3;1H\nאב'.encode()),
                self.plain_maps(hist[2:5], 5),
                False,
            ),
            ('resize rewrap', lambda: self.drawn(long_row), lambda s: s.resize(5, 13), (rev, None, None, None, None), True),
        )
        for name, build, trigger, expect, fresh in cases:
            with self.subTest(name):
                self.trigger_case(build, trigger, expect, fresh)

    def test_cache_option_toggle(self):
        self.toggle_drops_maps('abc' + A, self.ABC_A, False)

    def test_cache_losing_and_gaining_rtl(self):
        # trigger: a dirty row. Overwriting the only RTL run leaves a Latin row (nothing to resolve); Hebrew written into it resolves
        s = self.warmed('abc' + A)
        self.put(s, 1, 4, 'xyz')
        self.frame(s)
        self.assertIsNone(s.test_bidi_v2l(0))
        self.warm(s)
        self.put(s, 1, 4, A)
        self.ae((self.frame(s), s.test_bidi_v2l(0), self.frame(s)), (1, self.ABC_A, 0))

    def test_cache_tall_paragraph_always_resolves(self):
        # a paragraph cut by the row cap is never cached: unchanged frames still resolve it
        s = self.screen(cols=2, lines=300, scrollback=200)
        s.draw('א!' * 400)
        seen = []
        for _ in range(3):
            self.assertGreater(self.frame(s), 0)
            seen.append(self.maps(s))
        self.ae(seen, seen[:1] * 3)

    def test_cache_paused_rendering(self):
        # trigger: rendering paused, then resumed. Paused frames read a snapshot and are not cached
        rows = ['אבג abc', 'abc אבג']
        s = self.filled(rows)
        self.frame(s)
        self.warm(s)
        s.pause_rendering(True, 1000)
        self.frame(s)
        s.pause_rendering(False)
        self.assertGreater(self.frame(s), 0)
        f = self.filled(rows)
        self.frame(f)
        self.ae(self.maps(s), self.maps(f))

    def test_cache_overlay_row(self):
        # an overlay line on a Hebrew row, then removed: the row keeps its map, so the cache holds throughout
        s = self.screen(bidi_auto_direction=False)
        s.draw('abc' + A)
        s.cursor_position(1, 9)
        self.frame(s)
        self.warm(s)
        seen = []
        for overlay, n in (('xy', 3), ('', 1)):
            s.test_update_overlay_text(overlay)
            seen += [(self.frame(s), s.test_bidi_v2l(0)) for _ in range(n)]
        self.ae(seen, [(0, self.ABC_A)] * 4)

    def test_cache_reset(self):
        # trigger: RIS. Cleared rows take the plain row path whatever the cache says, so this checks the maps, not the invalidation
        s = self.warmed('abc' + A)
        parse_bytes(s, b'\x1bc')
        self.frame(s)
        self.ae(self.maps(s), (None,) * 5)
        s.draw('abc' + A)
        self.assertGreater(self.frame(s), 0)
        self.ae(s.test_bidi_v2l(0), self.ABC_A)

    def test_cache_same_class_edit_skips_resolve(self):
        # a dirty row that feeds FriBidi the same classes, brackets and cell layout keeps its paragraph's maps without resolving
        s = self.screen(lines=4, scrollback=0)
        s.draw(self.MIXED)
        self.ae([self.frame(s), self.frame(s)], [1, 0])
        self.ae((self.state(s), self.dirty(s)), (self.MIXED_STATE, []))
        count = s.test_bidi_resolve_count()
        self.put(s, 1, 1, 'x')
        s.test_bidi_refresh()
        self.ae((s.test_bidi_resolve_count() - count, self.dirty(s)), (0, [0]))
        buf = test_screen_update_cell_data(s)
        self.ae((s.test_bidi_resolve_count() - count, self.state(s)), (0, self.MIXED_STATE))
        f = self.drawn('x' + self.MIXED[1:], lines=4, scrollback=0)
        fbuf = test_screen_update_cell_data(f)
        self.ae(self.state(s), self.state(f))
        want = [self.sprites(gpu_row(f, fbuf, y)) for y in range(3)]
        self.assertTrue(all(map(any, want)), 'a blank fresh row')
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
                self.ae(s.test_bidi_levels(0), levels0)
                self.put(s, 1, col, new)
                self.ae((str(s.linebuf.line(0)), self.frame(s), s.test_bidi_levels(0)), (final, 1, levels1))
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
            self.ae((s.scrolled_by, self.dirty(s), self.frame(s), s.test_bidi_levels(2)), (1, [2], 1, (2, 2, 1, 2, 0)))
            s.scroll(1, False)
            self.frame(s)
            self.same_as(s, self.stacked(('אבגדה', 'אבגדה', '12 3!', 'azcde')))
        with self.subTest('v explicit embedding then a blank'):
            # ב/, an OSC 66 cell holding RLO, then EM SPACE (WS, not a blank): a blank for it keeps class WS, so the trim must not drop it
            def rlo(last):
                s = self.screen(cols=4, lines=1, scrollback=0)
                s.draw('ב/')
                parse_bytes(s, '\x1b]66;w=1;\u202e\x07'.encode())
                s.draw(last)
                self.ae(str(s.linebuf.line(0))[2], '\u202e')
                return s

            s = rlo('\u2003')
            self.frame(s)
            self.warm(s)
            self.put(s, 1, 4, ' ')
            self.frame(s)
            self.ae((s.test_bidi_levels(0), s.test_bidi_v2l(0)), ((1, 1, 0, 0), (1, 0, 2, 3)))
            self.same_as(s, rlo(' '))

    def test_cache_bypass_between_resolves(self):
        # A resolves MIXED; B draws a scale-2 multicell (paragraph inactive); C restores the cells: same FriBidi input as A, B's maps
        s = self.screen(lines=4, scrollback=0)
        s.draw(self.MIXED)
        self.ae(self.frame(s), 1)
        self.warm(s)
        self.ae(self.maps(s), self.MIXED_STATE[0])
        s.cursor_position(1, 1)
        draw_multicell(s, 'x', scale=2)
        self.ae([s.linebuf.is_continued(y) for y in range(4)], [False, True, True, False])
        self.frame(s)
        self.ae(self.maps(s), (None,) * 4)
        self.put(s, 1, 1, self.MIXED[:2])
        self.put(s, 2, 1, self.MIXED[10:12])
        self.ae(str(s.linebuf.line(0)) + str(s.linebuf.line(1)), self.MIXED[:20])
        self.ae([s.linebuf.is_continued(y) for y in range(4)], [False, True, True, False])  # same bounds as in A: only the signatures differ
        self.ae(self.frame(s), 1)
        self.ae(self.state(s), self.MIXED_STATE)

    def test_pointed_hebrew_shaping(self):
        # shin + qamats + shin dot, lamed, vav + holam, final mem: 4 cells (marks join their base cell), every group covers one cell
        with tempfile.TemporaryDirectory() as tdir:
            path = os.path.join(tdir, 'LiberationMono-Regular.ttf')
            with open(path, 'wb') as f:
                f.write(testing_font_data('LiberationMono-Regular.ttf'))
            groups = shape_string('שָׁלוֹם', path=path, rtl=True)
        self.ae([g[0] for g in groups], [1, 1, 1, 1], groups)

    RTL_WRAPPED = (((4, 3, 2, 1, 0), (4, 0, 1, 2, 3), None, None), ((1,) * 5, (2, 2, 2, 2, 1), None, None))  # (maps, levels)
    LTR_WRAPPED = (((0, 4, 3, 2, 1), None, None, None), ((0, 1, 1, 1, 1), None, None, None))

    def wrapped_rtl_screen(self, first='א'):
        s = self.screen(cols=5, lines=4, scrollback=0, bidi_auto_direction=True)
        s.draw(first + 'בגדה')
        s.draw('ab 1')
        return s

    def upload_sprites(self, s, rows=(0, 1)):
        buf = test_screen_update_cell_data(s)
        return [self.sprites(gpu_row(s, buf, y)) for y in rows]

    def edit_first(self, s, ch, resolves, dirty, rtl, want):
        # draws ch over the first cell, checks the refresh resolve count and dirty list, then uploads and returns sprites
        s.cursor_position(1, 1)
        s.draw(ch)
        count = s.test_bidi_resolve_count()
        s.test_bidi_refresh()
        self.ae(s.test_bidi_resolve_count() - count, resolves)
        self.ae((self.dirty(s), [s.test_bidi_paragraph_rtl(y) for y in (0, 1)]), (dirty, [rtl, rtl]))
        got = self.upload_sprites(s)
        self.ae((s.test_bidi_resolve_count() - count, self.state(s)), (resolves, want))
        return got

    def matches_fresh(self, s, got, f, rows=(0, 1)):
        # s, whose upload was got, has the maps, levels and sprites of a fresh screen f that only saw the final text
        want = self.upload_sprites(f, rows)
        self.ae(self.state(s), self.state(f))
        self.assertTrue(all(map(any, want)), 'a blank fresh row')
        self.ae(got, want)

    def test_trailing_blank_trim_keeps_maps(self):
        # dropping the blanks at a paragraph's end before resolving must not change any row's maps
        def wrapped_blank(s):
            s.draw('א' * 10)
            s.draw('ב')
            s.cursor_position(2, 1)
            s.erase_characters(1)
            self.assertTrue(s.linebuf.is_continued(1))

        def rlo_cell(s):  # draw drops a zero-width Cf, an OSC 66 cell keeps it; untrimmed, blanks after the RLO set the eos of the run before
            s.draw('ב/')
            parse_bytes(s, '\x1b]66;w=1;\u202e\x07'.encode())
            self.ae(str(s.linebuf.line(0))[2], '\u202e')

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
        for auto in (False, True):
            for name, build in cases.items():
                with self.subTest(name, auto=auto):
                    screens = []
                    for trim in (True, False):
                        s = self.screen(bidi_auto_direction=auto)
                        s.test_bidi_trim(trim)
                        build(s)
                        screens.append(s)
                    counts = [self.frame(s) for s in screens]
                    for s in screens:
                        self.warm(s)
                    rtl = auto and name != 'LTR start'
                    self.ae([s.test_bidi_paragraph_rtl(0) for s in screens], [rtl, rtl])
                    self.ae(self.state(screens[0]), self.state(screens[1]))
                    if name == 'ES next to EN':
                        self.ae(counts, [1, 1])

    def test_rtl_paragraph_right_aligned(self):
        # auto: the first strong R sets paragraph level 1, the blanks take it (L1): all level 1; auto off: paragraph level 0
        s = self.screen(bidi_auto_direction=True)
        fgs = draw_coloured(s, A)
        row = self.uploaded(s, 0)
        self.ae((s.test_bidi_levels(0), s.test_bidi_v2l(0), s.test_bidi_paragraph_rtl(0)), ((1,) * 10, ID10[::-1], True))
        self.ae(([c.fg for c in row[7:]], all(c.sprite_idx for c in row[7:]), self.sprites(row[:7])), (fgs[::-1], True, [0] * 7))
        self.ae((s.cursor.x, s.test_bidi_visual_x(3, 0)), (3, 6))
        f = self.screen(bidi_auto_direction=False)
        draw_coloured(f, A)
        frow = self.uploaded(f, 0)
        self.ae((f.test_bidi_levels(0), f.test_bidi_v2l(0), f.test_bidi_paragraph_rtl(0)), ((1, 1, 1) + (0,) * 7, (2, 1, 0) + ID10[3:], False))
        self.ae(row[9].sprite_idx, frow[2].sprite_idx)

    def test_rtl_paragraph_direction(self):
        # P2-P3: the first strong character (R, AL or L; not EN or AN) sets the direction; per row, per auto mode listed, after a refresh: (levels, map, rtl)
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
                    s = self.screen(cols=cols, bidi_auto_direction=auto)
                    s.draw(text)
                    s.test_bidi_refresh()
                    self.ae([(s.test_bidi_levels(y), s.test_bidi_v2l(y), s.test_bidi_paragraph_rtl(y)) for y in range(len(rows))], rows)

    def test_rtl_first_strong_edit(self):
        # writing the first strong character flips the paragraph; row 1 is not written but its levels change, so it must be reshaped
        s = self.wrapped_rtl_screen()
        self.ae(self.frame(s), 1)
        self.warm(s)
        self.ae((self.state(s), [s.test_bidi_paragraph_rtl(y) for y in (0, 1)]), (self.RTL_WRAPPED, [True, True]))
        for ch, rtl, want, resolves, dirty, new_screen in (
            ('x', False, self.LTR_WRAPPED, 1, [0, 1], False),
            ('א', True, self.RTL_WRAPPED, 1, [0, 1], False),
            ('ב', True, self.RTL_WRAPPED, 0, [0], True),  # R for R: direction depends on classes alone, same-class skips resolve
        ):
            with self.subTest(ch):
                if new_screen:
                    s = self.wrapped_rtl_screen()
                    self.frame(s)
                    self.warm(s)
                got = self.edit_first(s, ch, resolves, dirty, rtl, want)
                self.matches_fresh(s, got, self.wrapped_rtl_screen(ch))

    def test_rtl_option_toggles_reresolve(self):
        # toggling bidi_auto_direction changes every paragraph's direction without dirtying a row: the frame must re-resolve and reshape
        expect = {}
        for auto, v2l in ((False, (2, 1, 0) + ID10[3:]), (True, ID10[::-1])):
            f = self.drawn(A, bidi_auto_direction=auto)
            expect[auto] = (self.upload_sprites(f, (0,))[0], v2l)
        s = self.drawn(A, bidi_auto_direction=True)
        self.ae(self.frame(s), 1)
        self.warm(s)
        self.ae((s.test_bidi_v2l(0), s.test_bidi_paragraph_rtl(0)), (expect[True][1], True))
        for auto in (False, True):
            with self.subTest(auto=auto):
                self.set_options({'bidi_reorder': True, 'bidi_auto_direction': auto})
                self.ae(self.dirty(s), [])
                count = s.test_bidi_resolve_count()
                s.test_bidi_refresh()
                self.assertGreater(s.test_bidi_resolve_count() - count, 0)
                self.ae(self.dirty(s), [0])
                self.ae((self.upload_sprites(s, (0,))[0], s.test_bidi_v2l(0), s.test_bidi_paragraph_rtl(0)), (*expect[auto], auto))
                self.warm(s)
        self.toggle_drops_maps(A, ID10[::-1], True, bidi_auto_direction=True)

    def test_rtl_paragraph_cache_triggers(self):
        # cache triggers on RTL paragraphs (auto on): each re-resolves and equals a fresh screen. Maps are right-aligned
        rev, rev5, mix = ID10[::-1], (4, 3, 2, 1, 0), (9, 8, 7, 4, 5, 6, 3, 2, 1, 0)
        long = 'אבגדהוזחטיכלמ'
        para = long + 'נ'
        base = (rev, mix, rev, rev, None)

        def scr(text, **kw):
            s = self.screen(bidi_auto_direction=True, **kw)
            parse_bytes(s, text.encode())
            return s

        def basic():  # a Hebrew-first paragraph soft-wrapped onto rows 2-3, whose first row ends in a blank (map start)
            return scr('אבג\r\nאבג abc\r\nאבגדהוזחט אבג\x1b[1;1H')

        def alt():
            return scr('\x1b[?1049hאבג\r\nאבג abc\x1b[4;1H' + long + '\x1b[1;1H')

        def edge(text, **kw):
            s = scr(text, cols=5, scrollback=10, **kw)
            self.prerender(s)
            return s

        def edge_top():
            s = edge(para + '\r\nx1\r\nx2\r\nx3\r\nx4\r\n', pixel_scroll=False)
            s.scroll(3, True)
            return s

        cases = (
            ('dirty row', basic, lambda s: parse_bytes(s, b'\x1b[4;4H\xd7\x93'), base),
            ('index', basic, lambda s: parse_bytes(s, b'\x1b[5;1H\x1bD'), (mix, rev, rev, None, None)),
            ('reverse index', basic, lambda s: parse_bytes(s, b'\x1b[1;1H\x1bM'), (None, rev, mix, rev, rev)),
            ('insert then delete line', alt, lambda s: parse_bytes(s, b'\x1b[L\x1b[M'), (rev, mix, None, rev, rev)),
            ('view scroll, bottom edge', lambda: edge('x0\r\nx1\r\nx2\r\n' + para), lambda s: s.scroll(1, True), (None, None, None, rev5, rev5)),
            ('view scroll, top edge', edge_top, lambda s: s.scroll(1, False), (rev5, rev5, None, None, None)),
            ('resize with rewrap', lambda: scr(long), lambda s: s.resize(5, 13), (tuple(range(12, -1, -1)), None, None, None, None)),
            ('pause', basic, lambda s: (s.pause_rendering(True, 1000), self.frame(s), s.pause_rendering(False)), base),
        )
        for name, build, trigger, expect in cases:
            with self.subTest(name):
                screens = []
                self.trigger_case(lambda: screens.append(build()) or screens[-1], trigger, expect, fresh=not name.startswith('view'))
                self.assertTrue(all(any(s.test_bidi_paragraph_rtl(y) for y in range(s.lines)) for s in screens), 'no RTL row')
        s = basic()
        self.warm(s)
        self.ae((self.maps(s), [s.test_bidi_paragraph_rtl(y) for y in range(4)]), (base, [True] * 4))

    def test_rtl_paragraph_split_changes_direction(self):
        # clearing row 0's soft wrap makes row 1 its own paragraph with its own first strong character: unwritten, but reshaped
        def build(first, second):
            s = self.screen(cols=5, lines=5, bidi_auto_direction=True)
            s.draw(first)
            s.draw(second)
            return s

        for first, second, rtl_after, map_after in (('abcde', 'אבג', True, (4, 3, 2, 1, 0)), ('אבגדה', 'abc', False, None)):
            with self.subTest(first):
                s = build(first, second)
                self.warm(s)
                self.assertIs(s.test_bidi_paragraph_rtl(1), not rtl_after)
                self.put(s, 1, 5, 'x')
                count = s.test_bidi_resolve_count()
                s.test_bidi_refresh()
                self.assertGreater(s.test_bidi_resolve_count() - count, 0)
                self.assertIn(1, self.dirty(s))
                got = self.upload_sprites(s, (1,))
                self.ae((s.test_bidi_v2l(1), bool(s.test_bidi_paragraph_rtl(1))), (map_after, rtl_after))
                f = build(first, second)
                self.put(f, 1, 5, 'x')
                self.matches_fresh(s, got, f, (1,))
                self.warm(s)

    def test_rtl_paragraph_selection_and_mouse(self):
        s = self.drawn(A, bidi_auto_direction=True)
        self.ae((self.selected_columns(s, 0, 2), s.test_bidi_paragraph_rtl(0)), ([7, 8, 9], True))
        # a click at visual 0, left of the text, is a trailing blank at paragraph level 1, logical 9: the half is flipped
        self.ae((s.test_bidi_logical_x(0, 0), s.test_bidi_is_rtl(9, 0), s.test_bidi_logical_x(9, 0)), (9, True, 0))

    LTR_MIXED = (0, 1, 2, 3, 6, 5, 4, 7, 8, 9)  # 'abc אבג' at level 0
    RTL_MIXED = (9, 8, 7, 6, 5, 4, 3, 0, 1, 2)  # the same at level 1
    LRM, RLM, ALM = '\u200e', '\u200f', '\u061c'
    RTL_PLAIN = (9, 8, 7) + ID10[:7]  # 'abc def' at level 2 in an RTL paragraph

    def bases(self, s, *seqs):
        ans = []
        for seq in seqs:
            parse_bytes(s, seq)
            ans.append('NONE LTR RTL AUTO'.split()[s.test_bidi_base()])
        return ' '.join(ans)

    def replies(self, s, *seqs):
        ans = []
        for seq in seqs:
            s.callbacks.clear()
            parse_bytes(s, seq)
            ans.append(s.callbacks.wtcbuf)
        return ans

    def refreshed(self, s):
        s.test_bidi_refresh()
        return self.maps(s)

    def marked(self, mark=RLM, **opts):
        s = self.screen(bidi_auto_direction=True, **opts)
        parse_bytes(s, f'{mark}abc def'.encode())
        return s

    def test_bidi_control_parse(self):
        # SCP 0 and no parameter give AUTO back; extra parameters, SCP 3 and a private SCP are ignored
        self.ae(self.bases(self.screen(bidi_auto_direction=False), b'', b'\x1b[8l', b'\x1b[8h'), 'LTR NONE LTR')
        seqs = (b'', b'\x1b[8l', b'\x1b[8h', b'\x1b[2 k', b'\x1b[1 k', b'\x1b[0 k', b'\x1b[2 k', b'\x1b[ k',
                b'\x1b[2 k', b'\x1b[1;0 k', b'\x1b[1;2;3 k', b'\x1b[3 k', b'\x1b[?1 k', b'\x1b[0 k', b'\x1b[?2501l', b'\x1b[?2501h')  # fmt: skip
        self.ae(self.bases(self.screen(bidi_auto_direction=True), *seqs), 'AUTO NONE AUTO RTL LTR AUTO RTL AUTO RTL LTR LTR LTR LTR AUTO LTR AUTO')
        self.ae(self.bases(self.screen(bidi_auto_direction=False), b'\x1b[?2501h', b'\x1b[?2501l', b'\x1b[2 k'), 'AUTO LTR RTL')
        s = self.screen(bidi_auto_direction=False)
        self.ae(
            (self.bases(s, b'\x1b[4;8l'), self.replies(s, b'\x1b[4$p'), self.bases(s, b'\x1b[4;8h'), self.replies(s, b'\x1b[4$p')),
            ('NONE', [b'\x1b[4;2$y'], 'LTR', [b'\x1b[4;1$y']),
        )
        seqs = (b'\x1b[8$p', b'\x1b[8l\x1b[8$p', b'\x1b[8h\x1b[8$p', b'\x1b[?2501$p', b'\x1b[?2501h\x1b[?2501$p', b'\x1b[?2501l\x1b[?2501$p')
        want = [b'\x1b[8;1$y', b'\x1b[8;2$y', b'\x1b[8;1$y', b'\x1b[?2501;2$y', b'\x1b[?2501;1$y', b'\x1b[?2501;2$y']
        self.ae(self.replies(s, *seqs), want)
        self.ae(self.replies(self.screen(bidi_auto_direction=True), b'\x1b[?2501$p'), [b'\x1b[?2501;1$y'])
        # bidi_reorder off is the master switch: BDSM reports reset (explicit) whatever was set
        s = self.screen(bidi_reorder=False, bidi_auto_direction=False)
        self.ae(self.replies(s, b'\x1b[8$p', b'\x1b[8h\x1b[8$p', b'\x1b[8l\x1b[8$p'), [b'\x1b[8;2$y'] * 3)

    def test_bidi_control_change_marks_dirty(self):
        # a change of base with no output after it must mark the screen dirty, setting the value it already has must not
        s = self.screen(bidi_auto_direction=False)
        s.draw('abc ' + A)
        seqs = (b'\x1b[8h', b'\x1b[8l', b'\x1b[8l', b'\x1b[8h', b'\x1b[0 k', b'\x1b[1 k', b'\x1b[1 k', b'\x1b[2 k', b'\x1b[0 k',
                b'\x1b[?2501h', b'\x1b[?2501h', b'\x1b[?2501l')  # fmt: skip
        got = []
        for seq in seqs:
            test_screen_update_cell_data(s)
            clean = not s.is_dirty
            parse_bytes(s, seq)
            got.append((clean, s.is_dirty))
        self.ae(got, [(True, d) for d in (0, 1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 1)])

    def test_bidi_control_lifetime(self):
        # the base survives DECSC/DECRC, resize and erase; a reset or the alternate screen restore it
        s = self.screen(bidi_auto_direction=True)
        self.ae(self.bases(s, b'\x1b[2 k', b'\x1b7\x1b[8l\x1b8'), 'RTL NONE')
        s.draw(A)
        s.resize(s.lines + 1, s.columns + 3)
        s.resize(s.lines, s.columns - 2)
        self.ae(self.bases(s, b'', b'\x1b[8h'), 'NONE RTL')
        s.draw(A)
        self.ae(self.bases(s, b'\x1b[2J\x1b[H'), 'RTL')
        for reset in (b'\x1bc', b'\x1b[!p'):
            with self.subTest(reset=reset):
                seqs = (b'\x1b[8l\x1b[2 k\x1b[?2501l', b'\x1b[?1049h', b'\x1b[8h\x1b[1 k', reset, b'\x1b[?1049l')
                self.ae(self.bases(self.screen(bidi_auto_direction=True), *seqs), 'NONE NONE LTR AUTO AUTO')
        for mode in (b'1049', b'47', b'1047'):
            with self.subTest(mode=mode):
                on, off = b'\x1b[?' + mode + b'h', b'\x1b[?' + mode + b'l'
                seqs = (b'\x1b[2 k', on, b'\x1b[8l', off, b'\x1b[8l' + on, b'\x1b[8h\x1b[1 k', off)
                self.ae(self.bases(self.screen(bidi_auto_direction=False), *seqs), 'RTL RTL NONE RTL NONE LTR NONE')

    def test_bdsm_explicit_disables_reordering(self):
        # CSI 8 l stops reordering on the next frame, so the rows shaped with levels are reshaped; CSI 8 h gives the maps back
        rows = ['ab אב(ג)', A + ' (1)']
        s = self.screen(bidi_auto_direction=True)
        self.fill(s, rows)
        test_screen_update_cell_data(s)
        before = self.maps(s)

        def state():
            return self.maps(s), [s.test_bidi_line_rendered_with_bidi(y) for y in range(2)]

        self.ae(state(), (((0, 1, 2, 7, 6, 5, 4, 3, 8, 9), ID10[::-1], None, None, None), [True, True]))
        parse_bytes(s, b'\x1b[8l')
        buf = test_screen_update_cell_data(s)
        self.ae(state(), ((None,) * s.lines, [False, False]))
        parse_bytes(s, b'\x1b[8h')
        test_screen_update_cell_data(s)
        self.ae(self.maps(s), before)
        f = self.screen(bidi_reorder=False, bidi_auto_direction=True)  # made last, as making a screen sets the options
        self.fill(f, rows)
        fbuf = test_screen_update_cell_data(f)
        for y in range(2):
            fresh = self.sprites(gpu_row(f, fbuf, y))
            self.assertTrue(any(fresh))
            self.ae(self.sprites(gpu_row(s, buf, y)), fresh, f'row {y}')

    def test_scp_overrides_auto(self):
        # SCP 2 forces the paragraph direction whatever the autodetection says; SCP 0 or no parameter gives the option back
        s = self.screen(bidi_auto_direction=False)
        parse_bytes(s, b'\x1b[2 k')
        self.fill(s, ['abc ' + A, 'abc'])
        self.ae(self.refreshed(s), (self.RTL_MIXED, self.RTL_MIXED) + (ID10[::-1],) * 3)
        self.ae((s.test_bidi_levels(1), [s.test_bidi_paragraph_rtl(y) for y in range(2)]), ((2, 2, 2) + (1,) * 7, [True, True]))
        rows = [A + ' abc', 'abc ' + A, 'abc']
        for auto in (True, False):
            f = self.screen(bidi_auto_direction=auto)
            self.fill(f, rows)
            want = self.refreshed(f)
            for seq in (b'\x1b[0 k', b'\x1b[ k'):
                with self.subTest(auto=auto, seq=seq):
                    s = self.screen(bidi_auto_direction=auto)
                    self.fill(s, rows)
                    parse_bytes(s, b'\x1b[2 k')
                    self.assertNotEqual(self.refreshed(s), want)
                    parse_bytes(s, seq)
                    self.ae(self.refreshed(s), want)

    def test_hebrew_first_stays_ltr(self):
        # A Hebrew-first line stays LTR-based when the program asks for it (SCP 1, mode 2501 or 8 reset)
        ltr = (3, 2, 1, 0) + ID10[4:]
        for auto, seq, want in ((True, b'\x1b[1 k', ltr), (True, b'\x1b[?2501l', ltr), (True, b'\x1b[8l', None), (False, b'', ltr)):
            with self.subTest(auto=auto, seq=seq):
                s = self.screen(bidi_auto_direction=auto)
                parse_bytes(s, seq)
                s.draw('שלום abc')
                self.ae(self.refresh(s, 0), want)
                self.ae([s.test_bidi_visual_x(x, 0) for x in range(s.columns)], list(want or ID10))
                if want:
                    self.assertIs(s.test_bidi_paragraph_rtl(0), False)

    def test_control_change_reresolves(self):
        # The base is in the frame key: a change makes the next frame resolve the cached paragraphs again (explicit mode drops the maps)
        rows = ['abc ' + A, A + ' abc']

        def build():
            s = self.screen(bidi_auto_direction=False)
            self.fill(s, rows)
            return s

        s = build()
        self.frame(s)
        self.warm(s)
        sent = b''
        for seq, base in ((b'\x1b[8l', 'NONE'), (b'\x1b[8h', 'LTR'), (b'\x1b[2 k', 'RTL'), (b'\x1b[0 k', 'LTR'), (b'\x1b[?2501h', 'AUTO')):
            with self.subTest(seq=seq):
                sent += seq
                self.ae(self.bases(s, seq), base)
                count = self.frame(s)
                if base == 'NONE':
                    self.ae(self.maps(s), (None,) * s.lines)
                else:
                    self.assertGreater(count, 0)
                f = self.screen(bidi_auto_direction=False)
                parse_bytes(f, sent)
                self.fill(f, rows)
                self.frame(f)
                self.ae(self.maps(s), self.maps(f))
                self.warm(s)
        self.trigger_case(build, lambda s: parse_bytes(s, b'\x1b[Hx\x1b[2 k'), (self.RTL_MIXED, (9, 8, 7, 4, 5, 6, 3, 2, 1, 0)) + (ID10[::-1],) * 3)
        # entering and leaving the alternate screen (47 keeps its content) resolve again
        s = build()
        parse_bytes(s, b'\x1b[?47h')
        self.fill(s, rows[::-1])
        self.frame(s)
        self.warm(s)
        for seq in (b'\x1b[?47l', b'\x1b[?47h'):
            with self.subTest(seq=seq):
                parse_bytes(s, seq)
                self.assertGreater(self.frame(s), 0)
                self.warm(s)

    def test_paused_frame_uses_paused_base(self):
        # A paused frame renders its snapshot with the base captured when the pause began; a base sent during the
        # pause applies when it ends; sent before it with no live frame between, the snapshot goes with the new base
        pause, scp = b'\x1b[?2026h', b'\x1b[2 k'
        for seqs, during in ((pause + scp, self.LTR_MIXED), (scp + pause, self.RTL_MIXED)):
            with self.subTest(seqs=seqs):
                s = self.screen(bidi_auto_direction=False)
                s.draw('abc ' + A)
                self.frame(s)
                self.warm(s)
                parse_bytes(s, seqs)
                self.frame(s)
                self.ae(s.test_bidi_v2l(0), during)
                parse_bytes(s, b'\x1b[?2026l')
                self.frame(s)
                self.ae(s.test_bidi_v2l(0), self.RTL_MIXED)

    def test_tmux_like_opt_out(self):
        # Two panes on the alternate screen, border at column 10; the opt-out is sent on the main screen and carried into it
        row_a = 'אבג דהו   │ abc def   '  # Hebrew in the left pane only
        row_b = 'אבג       │ שלום      '  # Hebrew on both sides
        for seq, cols, rtl, left in (
            (b'', [11, 11], True, ('AUTO', 11, False)),
            (b'\x1b[8l', [10, 10], None, ('NONE', 10, True)),
            (b'\x1b[1 k', [10, 5], False, ('LTR', 10, False)),
        ):
            with self.subTest(seq=seq):
                s = self.screen(cols=22, bidi_auto_direction=True)
                s.draw(row_a)
                parse_bytes(s, seq + b'\x1b[?1049h')
                self.fill(s, [row_a, row_b])
                for y in range(2):
                    self.refresh(s, y)
                dirs = [s.test_bidi_v2l(y) if rtl is None else s.test_bidi_paragraph_rtl(y) for y in range(2)]
                self.ae(([s.test_bidi_visual_x(10, y) for y in range(2)], dirs), (cols, [rtl, rtl]))
                parse_bytes(s, b'\x1b[?1049l')
                self.refresh(s, 0)
                self.ae((self.bases(s, b''), s.test_bidi_visual_x(10, 0), s.test_bidi_v2l(0) is None), left)

    def test_leading_mark_paragraph(self):
        # A leading mark has no cell: RLM or ALM make the paragraph RTL, LRM LTR, and ALM turns the digits after it AN
        for name, text, v2l, levels, rtl, mark in (
            ('RLM', f'{self.RLM}abc def', self.RTL_PLAIN, (2,) * 7 + (1,) * 3, True, 2),
            ('LRM', f'{self.LRM}{A} x', (2, 1, 0) + ID10[3:], None, False, 1),
            ('none', f'{A} x', ID10[::-1], None, True, 0),
            ('ALM', f'{self.ALM}12 34', (9, 8, 7, 6, 5, 3, 4, 2, 0, 1), (2, 2, 1, 2, 2) + (1,) * 5, True, 3),
        ):
            with self.subTest(name):
                s = self.screen(bidi_auto_direction=True)
                parse_bytes(s, text.encode())
                self.ae((self.refresh(s, 0), s.test_bidi_paragraph_rtl(0), s.test_bidi_leading_mark(0)), (v2l, rtl, mark))
                if levels:
                    self.ae(s.test_bidi_levels(0), levels)

    def test_leading_mark_erase_in_line(self):
        # EL 1 and EL 2 take the mark with the start of the line; EL 0 at column 0 erases only cells, the mark sits before cell 0
        for seq, at_start in ((b'\x1b[2K', False), (b'\x1b[1K', False), (b'\x1b[0K', True)):
            with self.subTest(seq=seq):
                s = self.marked()
                self.ae((self.refresh(s, 0), s.test_bidi_leading_mark(0)), (self.RTL_PLAIN, 2))
                if at_start:
                    s.cursor_position(1, 1)
                parse_bytes(s, seq)
                self.ae(s.test_bidi_leading_mark(0), 2 if at_start else 0)
                s.test_bidi_refresh()
                self.assertIs(s.test_bidi_paragraph_rtl(0), at_start)

    def test_leading_mark_history_and_reflow(self):
        # The mark moves with the line into the history; a narrower screen wraps the marked row, only its first row keeps the mark
        s = self.marked(cols=10, lines=3)
        s.cursor_position(3, 1)
        s.index()
        self.ae(s.historybuf.count, 1)
        s.scroll(1, True)
        self.ae((self.refresh(s, 0), s.test_bidi_paragraph_rtl(0)), (self.RTL_PLAIN, True))
        s = self.marked(cols=10, lines=3)
        self.ae(self.refresh(s, 0), self.RTL_PLAIN)
        s.resize(3, 5)
        marks = [s.test_bidi_leading_mark(y) for y in (0, 1)]
        self.ae(
            (marks, self.refresh(s, 0), self.refresh(s, 1), [s.test_bidi_paragraph_rtl(y) for y in range(2)]), ([2, 0], ID10[:5], (4, 3, 2, 0, 1), [True, True])
        )
        s.resize(3, 10)
        self.ae(([s.test_bidi_leading_mark(y) for y in (0, 1)], self.refresh(s, 0), s.test_bidi_paragraph_rtl(0)), ([2, 0], self.RTL_PLAIN, True))

    def test_leading_mark_only_change_reresolves(self):
        # The mark is the only difference between the rows: a signature that ignores it would keep the old maps
        marked, plain = self.refreshed(self.marked()), self.refreshed(self.marked(''))
        self.assertNotEqual(plain, marked)
        s = self.marked()
        self.frame(s)
        self.warm(s)
        self.ae(self.maps(s), marked)
        for mark, want in (('', plain), (self.RLM, marked), ('', plain)):
            with self.subTest(mark=mark):
                s.cursor_position(1, 1)
                parse_bytes(s, b'\x1b[2K' + f'{mark}abc def'.encode())
                self.assertGreater(self.frame(s), 0, 'the mark changed, so the paragraph must resolve again')
                self.ae((self.maps(s), s.test_bidi_leading_mark(0)), (want, 2 if mark else 0))
                self.warm(s)

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
            ('blank after digits in rtl run', L, A + ' 12', (1, 1, 1, 1, 2, 2), None, (((6, 0), 6),), [(6, 6, L, 6)]),
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
                s = self.screen(bidi_auto_direction=auto)
                s.draw(text)
                got = self.refresh(s, 0)
                if v2l:  # None: not asserted
                    self.ae(got, v2l)
                if levels:
                    self.ae(s.test_bidi_levels(0)[: len(levels)], levels)
                self.ae([s.test_bidi_visual_x(*args) for args, _ in vx], [want for _, want in vx])
                for x, beam, mirrored, *other in cursors:
                    beam_got, *others = [s.test_cursor_visual(x, 0, k) for k in (CURSOR_BEAM, CURSOR_BLOCK, CURSOR_UNDERLINE, CURSOR_HOLLOW)]
                    self.ae(beam_got, (beam, mirrored))
                    if other:
                        self.ae(others, [(other[0], False)] * 3)

    def test_cursor_after_rtl_char_paused_pixel_scroll(self):
        # The paused snapshot has a leading row with pixel scroll: a block read from row y instead of y + 1 sees the row above
        s = self.screen(pixel_scroll=True, bidi_auto_direction=False)
        parse_bytes(s, f'abcdefgh\r\nls {A}'.encode())
        self.frame(s)
        self.ae(s.test_cursor_render_x(CURSOR_BLOCK), (2, False))
        parse_bytes(s, b'\x1b[?2026h')
        self.frame(s)
        self.ae((s.test_cursor_render_x(CURSOR_BLOCK), s.test_cursor_visual(6, 1, CURSOR_BLOCK)), ((2, False), (2, False)))

    def preedit_reference(self, text, x, preedit='xy'):
        # Rows a bidi-off screen uploads for text, cursor at x, without and with the pre-edit; call before making the screen under test
        f = self.screen(bidi_reorder=False, bidi_auto_direction=False)
        draw_coloured(f, text)
        f.cursor_position(1, x + 1)
        plain = self.uploaded(f, 0)
        self.assertTrue(any(self.sprites(plain)))
        f.test_update_overlay_text(preedit)
        return plain, self.uploaded(f, 0)

    def test_preedit_rtl_paragraph(self):
        # The cursor in the blank at x=7 is left of the text; the pre-edit of n cells ends there and extends left (IME anchor at 3 - n)
        plain_ref, preedit_ref = self.preedit_reference(A + ' ' + A, 7)
        s = self.screen(bidi_auto_direction=True)
        draw_coloured(s, A + ' ' + A)
        row0, maps0 = self.uploaded(s, 0), self.maps(s)

        def caret():
            return s.test_ime_anchor_x(), s.test_cursor_render_x(CURSOR_BEAM)

        self.ae(
            (maps0[0], s.test_cursor_visual(7, 0, CURSOR_BEAM), s.test_bidi_line_rendered_with_bidi(0), s.test_ime_anchor_x()), (ID10[::-1], (2, True), True, 3)
        )
        self.warm(s)
        count = s.test_bidi_resolve_count()
        s.test_update_overlay_text('xy')
        self.ae(caret(), (1, (3, False)))
        s.test_update_overlay_text('xyz')
        self.ae(caret(), (0, (3, False)))
        s.test_update_overlay_text('xy')
        row = self.uploaded(s, 0)
        self.ae(
            (s.test_bidi_v2l(0), s.test_bidi_line_rendered_with_bidi(0), row[:1] + row[3:], row[1:3]), (maps0[0], True, row0[:1] + row0[3:], preedit_ref[7:9])
        )
        self.ae(caret(), (1, (3, False)))
        for i in range(3):
            self.frame(s)
        self.ae((s.test_bidi_resolve_count(), s.test_bidi_line_rendered_with_bidi(0)), (count, True))
        s.test_update_overlay_text('')
        self.ae((self.uploaded(s, 0), self.maps(s)), (row0, maps0))
        parse_bytes(s, b'\x1b[8l')
        self.ae(self.sprites(self.uploaded(s, 0)), self.sprites(plain_ref))

    def test_preedit_in_other_rows(self):
        # LTR cell: the cursor on b follows a Latin letter, the pre-edit block starts at column 1 and extends right
        want = (0, 1, 2, 5, 4, 3) + ID10[6:]
        _, preedit_ref = self.preedit_reference('abc' + A, 1)
        s = self.screen(bidi_auto_direction=False)
        draw_coloured(s, 'abc' + A)
        s.cursor_position(1, 2)
        row0 = self.uploaded(s, 0)
        self.ae((s.test_bidi_v2l(0), s.test_cursor_visual(1, 0, CURSOR_BEAM)), (want, (1, False)))
        s.test_update_overlay_text('xy')
        self.ae((s.test_ime_anchor_x(), s.test_cursor_render_x(CURSOR_BEAM)), (1, (3, False)))
        row = self.uploaded(s, 0)
        self.ae((s.test_bidi_v2l(0), row[:1] + row[3:], row[1:3]), (want, row0[:1] + row0[3:], preedit_ref[1:3]))
        # The overlay is drawn into the real row: an RLM pre-edit at column 0 must not leave the mark or the flag on it
        s = self.screen(bidi_auto_direction=True)
        self.fill(s, ['abc def'])
        self.frame(s)
        for text in ('\u200fxy', ''):
            s.test_update_overlay_text(text)
            self.frame(s)
        self.ae((s.test_bidi_leading_mark(0), s.test_bidi_line_flag(0)), (0, False))
        f = self.screen(bidi_auto_direction=True)
        f.draw('abc def')
        self.frame(f)
        self.ae(self.maps(s), self.maps(f))
        # A Latin row has no map: the pre-edit row is uploaded as a bidi-off screen uploads it (underline colour checked on its own)
        plain_ref, preedit_ref = self.preedit_reference('abc def', 7)
        self.ae(preedit_ref[:7] + preedit_ref[9:], plain_ref[:7] + plain_ref[9:])
        self.assertTrue(all(c.decoration_fg and not p.decoration_fg for c, p in zip(preedit_ref[7:9], plain_ref[7:9])))
        s = self.screen(bidi_auto_direction=True)
        draw_coloured(s, 'abc def')
        self.frame(s)
        s.test_update_overlay_text('xy')
        self.ae((s.test_ime_anchor_x(), s.test_cursor_render_x(CURSOR_BEAM)), (7, (9, False)))
        self.ae((self.uploaded(s, 0), s.test_bidi_v2l(0)), (preedit_ref, None))
