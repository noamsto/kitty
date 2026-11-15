#!/usr/bin/env python
# License: GPLv3

import struct
from collections import namedtuple

from kitty.fast_data_types import test_screen_update_cell_data
from kitty.fonts.render import setup_for_testing

from .base import BaseTest, draw_multicell, parse_bytes

A = 'אבג'  # three Hebrew letters, bidi class R
ID10 = tuple(range(10))
# a row whose map is not its own inverse: R R WS EN EN, N1 makes the space R
NSI = 'אב 12'
NSI_V2L = (3, 4, 2, 1, 0)
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
        s = self.create_screen(cols=10, options={})  # no CSI 8 h
        s.draw('abc' + A)
        self.ae((self.refresh(s, 0), self.memo(s, 0)), (None, NONE))
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
        # RLM is zero width, so it never needs the classifier: a leading-mark matter
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
                self.ae(self.frame(s), (1, 0))

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
                    self.ae((v2l, row), (None, logical))

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
