#!/usr/bin/env python
# License: GPLv3

import os
import struct
import tempfile
from collections import namedtuple

from kitty.fast_data_types import test_screen_update_cell_data
from kitty.fonts.render import setup_for_testing, shape_string

from .base import BaseTest, draw_multicell, parse_bytes
from .fonts import testing_font_data

A = 'אבג'  # three Hebrew letters, bidi class R
ID10 = tuple(range(10))
# A row whose map is not its own inverse, on 5 columns: R R WS EN EN at
# paragraph level 0. W2 and W7 keep EN (last strong is R), N1 makes the space,
# between R and EN (as R), R. I1: R -> 1, EN -> 2. L2 reverses cols 3..4, then
# 0..4: v2l = 3 4 2 1 0, so l2v = 4 3 2 0 1.
NSI = 'אב 12'
NSI_LEVELS = (1, 1, 1, 2, 2)
NSI_V2L = (3, 4, 2, 1, 0)
NSI_L2V = (4, 3, 2, 0, 1)
GPUCellData = namedtuple('GPUCellData', 'fg bg decoration_fg sprite_idx attrs')
GPU_CELL = struct.Struct('=5I')  # GPUCell in kitty/line.h, 20 bytes


def gpu_row(s, buf, y):
    # the cells of screen row y in the buffer test_screen_update_cell_data
    # returns, which has an extra leading render row when pixel scroll is on
    row_size = s.columns * GPU_CELL.size
    start = (y + len(buf) // row_size - s.lines) * row_size
    return [GPUCellData(*c) for c in GPU_CELL.iter_unpack(buf[start : start + row_size])]


def draw_coloured(s, text):
    # draws each character with its own 256-colour fg and returns the GPUCell.fg
    # of each, an indexed colour being stored as (index << 8) | 1
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

    def test_auto_direction_option_default(self):
        from kitty.options.parse import parse_conf_item
        from kitty.options.types import defaults

        self.assertIs(defaults.bidi_auto_direction, False)
        ans = {}
        parse_conf_item('bidi_auto_direction', 'yes', ans)
        self.assertIs(ans['bidi_auto_direction'], True)

    def test_basic_run(self):
        # (a) abcאבג: L L L R R R then blanks. Hebrew letters resolve to level 1,
        # everything else level 0. Reordering reverses only the level 1 run
        # (cols 3..5) so visual order is 0 1 2 5 4 3 6 7 8 9. Trailing blanks
        # are WS at the end of the paragraph, reset to level 0 by L1.
        s = self.screen()
        s.draw('abc' + A)
        v2l = self.refresh(s, 0)
        self.ae(v2l, (0, 1, 2, 5, 4, 3, 6, 7, 8, 9))
        self.ae(s.test_bidi_levels(0), (0, 0, 0, 1, 1, 1, 0, 0, 0, 0))

    def test_neutrals_between_rtl(self):
        # (b) אב, גד: R R CS WS R R. The comma and the space lie between R and R,
        # so N1 gives them R. Cols 0..5 are one level 1 run and are fully
        # reversed. Trailing blanks go to level 0 (L1).
        s = self.screen()
        s.draw('אב, גד')
        v2l = self.refresh(s, 0)
        self.ae(s.test_bidi_levels(0), (1, 1, 1, 1, 1, 1, 0, 0, 0, 0))
        self.ae(v2l, (5, 4, 3, 2, 1, 0, 6, 7, 8, 9))

    def test_wide_cells(self):
        # (c1) א😀ב: emoji is class ON between R and R so N1 makes it R. Cells are
        # א=0, emoji=1,2 (second cell is the continuation), ב=3, all level 1.
        # Reversing the run visually gives cells 3, then the emoji reversed
        # (2 left of 1), then 0.
        s = self.screen()
        s.draw('א😀ב')
        v2l = self.refresh(s, 0)
        self.ae(s.test_bidi_levels(0), (1, 1, 1, 1, 0, 0, 0, 0, 0, 0))
        self.ae(v2l, (3, 2, 1, 0, 4, 5, 6, 7, 8, 9))
        # with a span of 1 logical cell 1 alone is at visual column 2; with the
        # emoji's span of 2 its leftmost visual cell is its second cell, column 1
        self.ae(s.test_bidi_visual_x(1, 0), 2)
        self.ae(s.test_bidi_visual_x(1, 0, 2), 1)
        # (c2) א好ב: 好 is strong L so it stays level 0 and its two cells keep order;
        # the single-cell R runs on either side reverse to themselves.
        s = self.screen()
        s.draw('א好ב')
        v2l = self.refresh(s, 0)
        self.ae(s.test_bidi_levels(0), (1, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        self.ae(v2l, ID10)
        # (c3) אב好גד: R R L(wide) R R. Two level 1 runs of two cells each:
        # cells 0,1 reverse to 1,0 and cells 4,5 to 5,4, the wide cells 2,3 stay.
        s = self.screen()
        s.draw('אב好גד')
        v2l = self.refresh(s, 0)
        self.ae(v2l, (1, 0, 2, 3, 5, 4, 6, 7, 8, 9))

    def test_soft_wrap_unflagged_continuation(self):
        # (d) cols=5: אבגדה fills row 0, 12 34 soft-wraps to row 1 and has no RTL
        # character so its has_bidi is False. Paragraph: R*5 EN EN WS EN EN.
        # W2 keeps EN (last strong is R). N1 treats EN as R, so the space is R.
        # I1 (the paragraph level, 0, is even): R -> level 1 and EN -> level 2.
        # Row 0 is all level 1: (4,3,2,1,0).
        # Row 1 levels 2,2,1,2,2: reverse level>=2 runs -> 1 0 2 4 3, then
        # reverse the whole level>=1 span -> 3 4 2 0 1.
        s = self.screen(cols=5, lines=5)
        s.draw('אבגדה')
        s.draw('12 34')
        self.assertTrue(s.line(1).as_ansi())
        self.assertFalse(s.test_bidi_line_flag(1))
        self.assertTrue(s.test_bidi_line_flag(0))
        s.test_bidi_refresh()
        self.ae(self.refresh(s, 0), (4, 3, 2, 1, 0))
        self.ae(s.test_bidi_levels(1), (2, 2, 1, 2, 2))
        self.ae(self.refresh(s, 1), (3, 4, 2, 0, 1))

    def test_latin_only_and_option_off(self):
        # (e) no strong RTL: no flag, no map
        s = self.screen()
        s.draw('hello world')
        self.assertFalse(s.test_bidi_line_flag(0))
        self.assertIsNone(self.refresh(s, 0))
        # (f) option off: Hebrew sets no flag and gets no map
        s = self.create_screen(cols=10, lines=5, options={'bidi_reorder': False})
        s.draw('abc' + A)
        self.assertFalse(s.test_bidi_line_flag(0))
        self.assertIsNone(self.refresh(s, 0))
        # (l) force_ltr wins over bidi_reorder: no map
        s = self.screen(force_ltr=True)
        s.draw('abc' + A)
        self.assertIsNone(self.refresh(s, 0))
        # and a real frame uploads the row unpermuted
        with setup_for_testing():
            s = self.screen(force_ltr=True)
            fgs = draw_coloured(s, 'abc' + A)
            row = gpu_row(s, test_screen_update_cell_data(s), 0)
            self.assertIsNone(s.test_bidi_v2l(0))
            self.ae([c.fg for c in row[: len(fgs)]], fgs)

    def test_flag_latch(self):
        # (g) the flag is a latch set when a reorderable codepoint is drawn.
        s = self.screen()
        self.assertFalse(s.test_bidi_line_flag(0))
        s.draw('א')
        self.assertTrue(s.test_bidi_line_flag(0))
        # erasing the cell leaves the flag (false positive accepted)
        s.erase_in_line(0)
        self.assertTrue(s.test_bidi_line_flag(0))
        s.cursor_position(1, 1)
        s.erase_in_line(2)
        self.assertTrue(s.test_bidi_line_flag(0))
        # with the text gone the stale flag gives no map: all cells are level 0
        self.assertIsNone(self.refresh(s, 0))

    def test_flag_range_boundaries(self):
        # (S5) the classifier's ranges, each boundary on both sides. The set is
        # the one bidi_codepoint_may_reorder has always had. A fresh screen per
        # codepoint, so one stale flag cannot hide another.
        # U+08FF, U+0900, U+FE00 and U+11000 are zero-width marks: draw_text_loop returns
        # before the flag test for them, so they are replaced by the nearest
        # drawable codepoint on the same side (U+08C9, U+0904, U+FE10, U+11003).
        # U+FE6F, the codepoint below the flagged U+FE70, is unassigned and draws.
        # U+FEFF is zero-width, so the unflagged side above U+FEFE is U+FF01.
        flagged = (0x590, 0x8C9, 0xFB1D, 0xFDFF, 0xFE70, 0xFEFE, 0x10800, 0x10FFF, 0x1E800, 0x1EFFF)
        unflagged = (0x58F, 0x904, 0xFB1C, 0xFE10, 0xFE6F, 0xFF01, 0x107FF, 0x11003, 0x1E7FF, 0x1F000)
        for cp, want in [(cp, True) for cp in flagged] + [(cp, False) for cp in unflagged]:
            with self.subTest(hex(cp)):
                s = self.screen()
                s.draw(chr(cp))
                self.assertGreater(s.cursor.x, 0, 'codepoint did not draw')
                self.assertIs(s.test_bidi_line_flag(0), want)
        # option off: Hebrew sets no flag
        s = self.screen(bidi_reorder=False)
        s.draw('א')
        self.assertFalse(s.test_bidi_line_flag(0))

    def test_flag_covers_fribidi_rtl(self):
        # RLM is zero-width so it never reaches the draw loop's flag check; the leading-mark path handles it.
        self.ae(self.screen().test_bidi_flag_misses(), [0x200F])

    def test_flag_named_scripts(self):
        # coverage only: these blocks were already flagged in round 1; FriBidi 14
        # shows no assigned R/AL letter outside the flag
        for cp in (0x0710, 0x0784, 0x07CA, 0x0800, 0x0840, 0x08A0, 0x0870, 0xFB2A, 0xFB50, 0xFE8D, 0x10A10, 0x1E900, 0x1EE00):
            with self.subTest(hex(cp)):
                s = self.screen()
                s.draw('ab ' + chr(cp) + chr(cp))
                self.assertTrue(s.test_bidi_line_flag(0), f'U+{cp:04X} did not set the flag')
                v2l = self.refresh(s, 0)
                self.assertIsNotNone(v2l, f'U+{cp:04X} produced no map')
                self.assertNotEqual(v2l, ID10, f'U+{cp:04X} was not reordered')

    def test_history(self):
        # (h) 2 visible rows, 4 rows of one paragraph. Rows 0 and 1 are pushed to
        # the scrollback. Visible row 0 is five R letters, level 1: (4,3,2,1,0).
        # Visible row 1 is 12 34 after R text: levels 2,2,1,2,2 -> (3,4,2,0,1)
        # as derived in test_soft_wrap_unflagged_continuation.
        s = self.screen(cols=5, lines=2, scrollback=10)
        for t in ('אבגדה', 'ובזחט', 'יכלמנ', '12 34'):
            s.draw(t)
        self.ae(s.historybuf.count, 2)
        self.ae(self.refresh(s, 0), (4, 3, 2, 1, 0))
        self.ae(self.refresh(s, 1), (3, 4, 2, 0, 1))
        # Only the continuation visible: text before it is all in history.
        s = self.screen(cols=5, lines=1, scrollback=10)
        for t in ('אבגדה', 'ובזחט', '12 34'):
            s.draw(t)
        self.ae(s.historybuf.count, 2)
        self.ae(self.refresh(s, 0), (3, 4, 2, 0, 1))

    def test_bypass(self):
        # (i) a row with an image placeholder or scaled text is not reordered
        s = self.screen()
        s.draw(A)
        self.assertIsNotNone(self.refresh(s, 0))
        s = self.screen()
        s.draw(A)
        s.draw(chr(0x10EEEE))
        self.assertIsNone(self.refresh(s, 0))
        s = self.screen()
        s.draw(A)
        draw_multicell(s, 'x', scale=2)
        self.assertIsNone(self.refresh(s, 0))

    def selected_columns(self, s, start, end):
        # the visual columns lit on screen row 0 when logical cells start..end
        # of it are selected; no other row may be lit
        s.clear_selection()
        s.start_selection(start, 0)
        s.update_selection(end, 0)
        s.test_bidi_refresh()
        raw = s.current_selections()
        offset = len(raw) // s.columns - s.lines
        self.assertIn(offset, (0, 1))
        lit = []
        for y in range(s.lines):
            row = raw[s.columns * (y + offset) : s.columns * (y + offset + 1)]
            nonzero = [x for x, b in enumerate(row) if b]
            if y:
                self.ae(nonzero, [], f'row {y}')
            else:
                lit = nonzero
        return lit

    def test_selection_permutation(self):
        # (j) logical cells 3 and 4 of abcאבג sit at visual columns 5 and 4
        # (v2l = 0 1 2 5 4 3 ...: visual 5 shows logical 3, visual 4 logical 4).
        s = self.screen()
        s.draw('abc' + A)
        self.ae(self.selected_columns(s, 3, 4), [4, 5])
        # on a row whose map is not its own inverse, a swapped map lights
        # visual 3 for logical 0 and visual 1 for logical 3
        s = self.screen(cols=5)
        s.draw(NSI)
        self.ae(self.refresh(s, 0), NSI_V2L)
        self.ae(self.selected_columns(s, 0, 0), [4])
        self.ae(self.selected_columns(s, 3, 3), [0])

    def test_inverse_maps(self):
        # (k) logical_x(visual_x(x)) == x; v2l and visual_x are inverse
        s = self.screen()
        s.draw('abc' + A + ' d')
        v2l = self.refresh(s, 0)
        levels = s.test_bidi_levels(0)
        for x in range(s.columns):
            vx = s.test_bidi_visual_x(x, 0)
            self.ae(s.test_bidi_logical_x(vx, 0), x)
            self.ae(vx, v2l.index(x))
            self.ae(s.test_bidi_logical_x(x, 0), v2l[x])
            self.ae(s.test_bidi_is_rtl(x, 0), bool(levels[x] & 1))
        # rows without a map give identity
        self.ae((s.test_bidi_visual_x(2, 3), s.test_bidi_logical_x(2, 3), s.test_bidi_is_rtl(2, 3)), (2, 2, False))
        # the map above is its own inverse, so this one tells v2l from l2v
        s = self.screen(cols=5)
        s.draw(NSI)
        self.ae(self.refresh(s, 0), NSI_V2L)
        self.ae(s.test_bidi_levels(0), NSI_LEVELS)
        self.ae(tuple(s.test_bidi_logical_x(x, 0) for x in range(5)), NSI_V2L)
        self.ae(tuple(s.test_bidi_visual_x(x, 0) for x in range(5)), NSI_L2V)
        self.assertNotEqual(NSI_V2L, NSI_L2V)

    def test_upload_permutes_cells(self):
        # each visual cell of the uploaded buffer is the logical cell v2l[visual],
        # told apart by fg colour, on a row whose map is not its own inverse
        with setup_for_testing():
            s = self.screen(cols=5)
            fgs = draw_coloured(s, NSI)
            row = gpu_row(s, test_screen_update_cell_data(s), 0)
            self.ae(s.test_bidi_v2l(0), NSI_V2L)
            self.ae([c.fg for c in row], [fgs[NSI_V2L[v]] for v in range(5)])

    def test_widen_merges_flag_and_map(self):
        # (m) row 0 abcde is Latin, the continuation row holds the only Hebrew.
        s = self.screen(cols=5, lines=5)
        s.draw('abcde')
        s.draw(A)
        self.assertFalse(s.test_bidi_line_flag(0))
        self.assertTrue(s.test_bidi_line_flag(1))
        # Row 1 is R R R blank blank: levels 1,1,1,0,0 -> reverse cols 0..2.
        self.ae(self.refresh(s, 1), (2, 1, 0, 3, 4))
        s.resize(5, 25)
        # merged: abcde + אבג on one row; the flag arrived only through the OR
        self.assertTrue(s.test_bidi_line_flag(0))
        v2l = self.refresh(s, 0)
        # L*5 then R R R at cols 5..7 reversed -> 7 6 5, rest identity
        self.ae(v2l, tuple(range(5)) + (7, 6, 5) + tuple(range(8, 25)))

    def test_overlay_row_unmapped(self):
        # IME overlay rows are not permuted, observed through the real
        # update-cell-data path
        with setup_for_testing():
            s = self.screen()
            s.draw('abc' + A)
            s.cursor_position(1, 9)  # row 0, so the overlay stays on the Hebrew row
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_v2l(0), (0, 1, 2, 5, 4, 3, 6, 7, 8, 9))
            s.test_draw_overlay_line('xy', 8, 0)
            test_screen_update_cell_data(s)
            self.assertIsNone(s.test_bidi_v2l(0))

    def test_option_off_at_runtime(self):
        # turning bidi off at runtime (config reload) drops the maps on the next
        # frame instead of leaving the old ones applied to selection, cursor and mouse
        for opts in ({'bidi_reorder': False}, {'bidi_reorder': True, 'force_ltr': True}):
            s = self.screen()
            s.draw('abc' + A)
            self.ae(self.refresh(s, 0), (0, 1, 2, 5, 4, 3, 6, 7, 8, 9))
            self.set_options(opts)
            s.cursor_position(1, 1)
            s.erase_in_line(2)
            s.draw('hello')
            s.test_bidi_refresh()
            self.assertIsNone(s.test_bidi_v2l(0), opts)
            self.ae([s.test_bidi_logical_x(x, 0) for x in range(s.columns)], list(ID10), opts)

    def test_resize_narrower_drops_maps(self):
        # until the next frame, maps sized for the old width must not be used
        s = self.screen(cols=20, lines=3)
        s.draw(''.join(chr(0x5D0 + i) for i in range(20)))
        self.ae(self.refresh(s, 0), tuple(range(19, -1, -1)))
        s.resize(3, 10)
        self.ae([s.test_bidi_logical_x(x, 0) for x in range(10)], list(ID10))
        self.ae([s.test_bidi_visual_x(x, 0) for x in range(10)], list(ID10))
        self.ae(self.refresh(s, 0), tuple(range(9, -1, -1)))

    def test_selection_clean_after_resize_with_option_off(self):
        # a resize after bidi is turned off drops the maps, the next selection
        # apply must leave the selection clean instead of dirty every frame
        s = self.screen()
        s.draw('abc' + A)
        self.refresh(s, 0)
        s.current_selections()
        self.set_options({'bidi_reorder': False})
        s.resize(5, 8)
        s.test_bidi_refresh()
        s.current_selections()
        self.assertFalse(s.is_selection_dirty())
        s.test_bidi_refresh()
        self.assertFalse(s.is_selection_dirty())

    def test_latin_paragraph_dirties_only_written_row(self):
        # with nothing to reorder, a write to one row of a soft-wrapped paragraph
        # must not redraw the rest of it
        with setup_for_testing():
            s = self.screen(cols=10, lines=8)
            s.draw('x' * 75)
            test_screen_update_cell_data(s)
            self.ae(s.linebuf.dirty_lines(), [])
            s.draw('x')
            self.ae(s.linebuf.dirty_lines(), [7])
            s.test_bidi_refresh()
            self.ae(s.linebuf.dirty_lines(), [7])

    def test_level_change_redraws_paragraph(self):
        with setup_for_testing():
            # the paragraph loses its last RTL char: the clean continuation row
            # was shaped at levels 2,2,1,2,2 (the . is CS between R and EN, so R)
            # and must be redrawn at level 0. Row 0 keeps its last cell, as
            # writing it would clear the soft wrap and split the paragraph.
            s = self.screen(cols=5, lines=5)
            s.draw('אבגד.')
            s.draw('12 34')
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(1), (2, 2, 1, 2, 2))
            self.ae(s.linebuf.dirty_lines(), [])
            s.cursor_position(1, 1)
            s.draw('abcd')
            self.assertTrue(s.linebuf.is_continued(1))
            self.ae(s.linebuf.dirty_lines(), [0])
            s.test_bidi_refresh()
            self.assertIsNone(s.test_bidi_v2l(1))
            self.ae(s.linebuf.dirty_lines(), [0, 1])

    def test_split_paragraph_redraws_old_continuation(self):
        # Row 1 holds (12) soft-wrapped after five R letters. N0 makes the brackets
        # R (EN inside counts as R, R before the opening bracket), so the row is
        # shaped with levels 1,2,2,1,0 and mirrored brackets. Overwriting the last
        # cell of row 0 clears its soft wrap: row 1 becomes its own paragraph with
        # no RTL and no flag, and must be redrawn without levels.
        with setup_for_testing():
            s = self.screen(cols=5, lines=5)
            s.draw('אבגדה')
            s.draw('(12)')
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(1), (1, 2, 2, 1, 0))
            self.assertFalse(s.test_bidi_line_flag(1))
            self.ae(s.linebuf.dirty_lines(), [])
            s.cursor_position(1, 5)
            s.draw('x')
            self.assertFalse(s.linebuf.is_continued(1))
            self.ae(s.linebuf.dirty_lines(), [0])
            s.test_bidi_refresh()
            self.assertIsNone(s.test_bidi_v2l(1))
            self.ae(s.linebuf.dirty_lines(), [0, 1])
            row1 = gpu_row(s, test_screen_update_cell_data(s), 1)
            # the same text rendered on a fresh screen, never bidi
            f = self.screen(cols=5, lines=5)
            f.draw('(12)')
            fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
            self.assertTrue(any(c.sprite_idx for c in fresh))
            self.ae([c.sprite_idx for c in row1], [c.sprite_idx for c in fresh])

    def test_split_paragraph_reshapes_lower_part(self):
        # In the paragraph, searching back from 12 the first strong type is R (ה),
        # so W7 leaves it EN; ( (ON, unpaired) lies between R and EN (EN counts as
        # R for N1), so level 1 and mirrored; 12 at level 0 goes to 2 (I1); ב is 1;
        # the trailing empty cell (WS) is 0 by L1: (1, 2, 2, 1, 0). Overwriting row
        # 0's last cell clears its soft wrap, so row 1 becomes its own paragraph
        # with no dirty row: searching back from 12 reaches sos (L), so W7 makes it
        # L; ( lies between sos and L, level 0, not mirrored; 12 is 0; ב 1; trailing
        # 0: (0, 0, 0, 1, 0). Row 1 must be reshaped at the new levels, so its
        # sprites match the same text rendered alone.
        with setup_for_testing():
            s = self.screen(cols=5, lines=5)
            s.draw('אבגדה')
            s.draw('(12ב')
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(1), (1, 2, 2, 1, 0))
            self.assertTrue(s.test_bidi_line_flag(1))
            self.ae(s.linebuf.dirty_lines(), [])
            s.cursor_position(1, 5)
            s.draw('ו')
            self.assertFalse(s.linebuf.is_continued(1))
            self.ae(s.linebuf.dirty_lines(), [0])
            row1 = gpu_row(s, test_screen_update_cell_data(s), 1)
            self.ae(s.test_bidi_levels(1), (0, 0, 0, 1, 0))
            f = self.screen(cols=5, lines=5)
            f.draw('(12ב')
            fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
            self.assertTrue(any(c.sprite_idx for c in fresh))
            self.ae([c.sprite_idx for c in row1], [c.sprite_idx for c in fresh])

    def test_split_paragraph_reshapes_after_pause(self):
        # As test_split_paragraph_reshapes_lower_part, with a paused frame
        # between the last live frame and the split: the first live frame after
        # the pause must still see that row 1's levels changed since the last
        # live frame, as rendering is paused for synchronized output. When the
        # split comes before the pause, the paused frames resolve the new levels
        # and reshape row 1, whose levels may have changed since the last live
        # frame, so the paused upload has glyphs shaped at them; a second paused
        # frame must not replace the line state the first one kept.
        def split(s):
            s.cursor_position(1, 5)
            s.draw('ו')
            self.assertFalse(s.linebuf.is_continued(1))

        for split_before_pause in (False, True):
            for paused_frames in (1, 2):
                with self.subTest(split_before_pause=split_before_pause, paused_frames=paused_frames), setup_for_testing():
                    s = self.screen(cols=5, lines=5)
                    s.draw('אבגדה')
                    s.draw('(12ב')
                    test_screen_update_cell_data(s)
                    self.ae(s.test_bidi_levels(1), (1, 2, 2, 1, 0))
                    f = self.screen(cols=5, lines=5)
                    f.draw('(12ב')
                    fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
                    self.assertTrue(any(c.sprite_idx for c in fresh))
                    if split_before_pause:
                        split(s)
                    s.pause_rendering(True, 1000)
                    for i in range(paused_frames):
                        buf = test_screen_update_cell_data(s)
                        if split_before_pause:
                            self.ae(s.test_bidi_levels(1), (0, 0, 0, 1, 0))
                            self.ae([c.sprite_idx for c in gpu_row(s, buf, 1)], [c.sprite_idx for c in fresh])
                    if not split_before_pause:
                        split(s)
                    s.pause_rendering(False)
                    row1 = gpu_row(s, test_screen_update_cell_data(s), 1)
                    self.ae(s.test_bidi_levels(1), (0, 0, 0, 1, 0))
                    self.ae([c.sprite_idx for c in row1], [c.sprite_idx for c in fresh])

    def test_dirty_row_redraws_only_rows_whose_levels_change(self):
        # One paragraph of 5 rows, all soft-wrapped. In row 1 the ! (ON) lies
        # between 3 (EN, counts as R for N1) and a (L) of row 2: N2, embedding
        # direction, level 0; 12 3 are 2 2 1 2 by I1 (W7 keeps EN, last strong
        # is R): (2, 2, 1, 2, 0). Writing ב over row 2's first cell puts ! between
        # R and R: N1, level 1. Rows 0, 3 and 4 keep their levels, so only row 1
        # (levels) and row 2 (written) are redrawn.
        text = ('אבגדה', '12 3!', 'abcde', 'fghij', 'klmno')
        with setup_for_testing():
            s = self.screen(cols=5, lines=6)
            for line in text:
                s.draw(line)
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(1), (2, 2, 1, 2, 0))
            self.ae(s.linebuf.dirty_lines(), [])
            count = s.test_bidi_resolve_count()
            s.cursor_position(3, 1)
            s.draw('ב')
            self.ae(s.linebuf.dirty_lines(), [2])
            s.test_bidi_refresh()
            self.ae(s.linebuf.dirty_lines(), [1, 2])
            self.ae(s.historybuf.dirty_lines(), [])
            self.ae(s.test_bidi_resolve_count() - count, 1)
            self.ae(s.test_bidi_levels(1), (2, 2, 1, 2, 1))
            buf = test_screen_update_cell_data(s)
            f = self.screen(cols=5, lines=6)
            for line in ('אבגדה', '12 3!', 'בbcde', 'fghij', 'klmno'):
                f.draw(line)
            fbuf = test_screen_update_cell_data(f)
            self.ae(f.test_bidi_levels(1), (2, 2, 1, 2, 1))
            for y in range(5):
                fresh = gpu_row(f, fbuf, y)
                self.assertTrue(any(c.sprite_idx for c in fresh))
                self.ae([c.sprite_idx for c in gpu_row(s, buf, y)], [c.sprite_idx for c in fresh])
            # Latin over Latin changes no levels: only the written row is redrawn
            s.cursor_position(4, 2)
            s.draw('z')
            s.test_bidi_refresh()
            self.ae(s.linebuf.dirty_lines(), [3])

    def test_paused_frame_redraws_only_rows_whose_levels_change(self):
        # Rows 2 and 3 are one-row paragraphs with levels. Overwriting the last
        # cell of אבגדה splits it from (12ב: row 1 gets other levels, rows 2 and
        # 3 keep theirs. A paused frame compares with the last live frame, so
        # only rows 0 (written) and 1 (levels) are redrawn.
        # With pixel scroll on the paused snapshot has a leading history row, so
        # the dirty indices are shifted by one.
        for pixel_scroll in (False, True):
            with self.subTest(pixel_scroll=pixel_scroll), setup_for_testing():
                ps = int(pixel_scroll)
                s = self.screen(cols=5, lines=5, pixel_scroll=pixel_scroll)
                s.draw('אבגדה')
                s.draw('(12ב')
                parse_bytes(s, '\r\nאב\r\nגד'.encode())
                test_screen_update_cell_data(s)
                for y in (1, 2, 3):
                    self.assertIsNotNone(s.test_bidi_levels(y))
                s.cursor_position(1, 5)
                s.draw('ו')
                s.pause_rendering(True, 1000)
                s.test_bidi_refresh()
                self.ae(s.test_bidi_paused_dirty_lines(), [y + ps for y in (0, 1)])

    def test_line_scrolled_into_view_reshaped_after_split(self):
        # linebuf rows y, אבגדה and (12ב, with x in history. In (12ב, searching
        # back from 12 the first strong type is R, so W7 leaves it EN; ( lies
        # between R and EN (as R): level 1; 12 is 2; ב is 1; the trailing cell 0:
        # (1, 2, 2, 1, 0). Overwriting the last cell of אבגדה clears its soft wrap,
        # so (12ב becomes its own paragraph: W7 makes 12 L, ( is 0, 12 is 0, ב is
        # 1: (0, 0, 0, 1, 0). The line was out of view in the last frame and its
        # paragraph has no dirty row, so it has no state to compare and is
        # reshaped when it comes back into view.
        for paused in (False, True):
            with self.subTest(paused=paused), setup_for_testing():
                s = self.screen(cols=5, lines=3, scrollback=10)
                parse_bytes(s, b'x\r\ny\r\n')
                s.draw('אבגדה')
                s.draw('(12ב')
                test_screen_update_cell_data(s)
                self.ae(s.test_bidi_levels(2), (1, 2, 2, 1, 0))
                s.scroll(1, True)
                test_screen_update_cell_data(s)
                self.ae(s.scrolled_by, 1)
                s.cursor_position(2, 5)
                s.draw('ו')
                self.assertFalse(s.linebuf.is_continued(2))
                s.scroll(1, False)
                self.ae(s.scrolled_by, 0)
                if paused:
                    s.pause_rendering(True, 1000)
                row = gpu_row(s, test_screen_update_cell_data(s), 2)
                self.ae(s.test_bidi_levels(2), (0, 0, 0, 1, 0))
                f = self.screen(cols=5, lines=3)
                f.draw('(12ב')
                fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
                self.assertTrue(any(c.sprite_idx for c in fresh))
                self.ae([c.sprite_idx for c in row], [c.sprite_idx for c in fresh])

    def test_paused_frame_row_continuing_from_history(self):
        # linebuf row 0, (12ב, continues אבגדה in history: levels (1, 2, 2, 1, 0)
        # as in test_split_paragraph_reshapes_lower_part. The paused snapshot walk
        # cannot see history, so row 0 resolves alone, as a paragraph of its own:
        # (0, 0, 0, 1, 0). The paused frame must upload it with glyphs shaped at
        # those levels, and the next live frame at the paragraph's again. With
        # pixel scroll on the snapshot has a leading row from history, which this
        # needs not to see.
        with setup_for_testing():
            s = self.screen(cols=5, lines=3, scrollback=10, pixel_scroll=False)
            s.draw('אבגדה')
            s.draw('(12ב')
            parse_bytes(s, b'\r\nx\r\ny')
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(0), (1, 2, 2, 1, 0))
            s.pause_rendering(True, 1000)
            buf = test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(0), (0, 0, 0, 1, 0))
            f = self.screen(cols=5, lines=3)
            f.draw('(12ב')
            fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
            self.assertTrue(any(c.sprite_idx for c in fresh))
            self.ae([c.sprite_idx for c in gpu_row(s, buf, 0)], [c.sprite_idx for c in fresh])
            s.pause_rendering(False)
            buf = test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(0), (1, 2, 2, 1, 0))
            g = self.screen(cols=5, lines=3)
            g.draw('אבגדה')
            g.draw('(12ב')
            fresh = gpu_row(g, test_screen_update_cell_data(g), 1)
            self.ae([c.sprite_idx for c in gpu_row(s, buf, 0)], [c.sprite_idx for c in fresh])

    def test_history_slot_reuse_reshapes_line(self):
        # History line ids are ring slots. X (א2 3( soft-wrapped into W) is
        # pushed into the slot of O, the oldest history line, which the last
        # frame showed at row 0 with levels (1, 2, 1, 2, 1). X's new levels are
        # equal to those, so comparing them as if X were O would skip reshaping
        # X although its glyphs were shaped at other levels.
        with setup_for_testing():
            s = self.screen(cols=5, lines=4, scrollback=0, pixel_scroll=False)
            s.draw('א2 3ב')  # O
            parse_bytes(s, b'\r\nh\r\ni\r\nj\r\n')
            s.draw('א2 3(')  # X
            s.draw('abcde')  # W
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
            f = self.screen(cols=5, lines=4, pixel_scroll=False)
            f.draw('א2 3(')
            f.draw('בbcde')
            fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
            self.assertTrue(any(c.sprite_idx for c in fresh))
            self.ae([c.sprite_idx for c in gpu_row(s, buf, 3)], [c.sprite_idx for c in fresh])

    def test_full_ring_push_reshapes_only_reused_slots(self):
        # History line ids are ring slots and a push into a full ring reuses
        # only the oldest slot. The history lines whose slots were not reused
        # keep their ids and levels, so they are not redrawn.
        #
        # The history ring holds 4 lines. Eight lines fill slots 0-3 oldest
        # first, so the push below reuses slot 0, and the pushed line, history
        # line 0, is in slot 0. dirty_lines() lists ring slots, so [0] means
        # the pushed line only. test_bidi_refresh deliberately skips the
        # scrolled_by bump screen_update_cell_data makes, so the view keeps
        # the history line numbers of the last frame.
        def fill():
            s = self.screen(cols=5, lines=4, scrollback=0, pixel_scroll=False)
            for i in range(8):
                s.draw('אב 12')
                if i < 7:
                    parse_bytes(s, b'\r\n')
            self.ae(s.historybuf.count, 4)
            # apply the history count, then draw every line once so none is
            # dirty for want of a frame
            test_screen_update_cell_data(s)
            self.prerender(s)
            self.ae(s.scrolled_by, 0)
            return s

        with setup_for_testing():
            # the reused slot (the oldest line) is off screen: rows 0-1 show
            # history lines 1 (slot 2) and 0 (slot 3)
            s = fill()
            s.scroll(2, True)
            test_screen_update_cell_data(s)
            test_screen_update_cell_data(s)
            self.ae(s.scrolled_by, 2)
            self.ae(s.historybuf.dirty_lines(), [])
            self.ae(s.test_bidi_levels(0), (1, 1, 1, 2, 2))
            s.cursor_position(4, 1)
            parse_bytes(s, b'\n')
            s.test_bidi_refresh()
            # row 0 now shows slot 3, which keeps its id and levels; row 1
            # shows the pushed line, new in slot 0
            self.ae(s.historybuf.dirty_lines(), [0])

            # the reused slot (the oldest line) is on screen at the top: rows
            # 0-3 show slots 0-3. The pushed line has the levels of the line
            # it replaces and fill() drew it, so its text is clean and only forgetting
            # the slot's id marks it dirty: compared with the old line, it would
            # look unchanged
            s = fill()
            s.scroll(10, True)
            test_screen_update_cell_data(s)
            test_screen_update_cell_data(s)
            self.ae(s.scrolled_by, 4)
            self.ae(s.historybuf.dirty_lines(), [])
            self.ae(s.test_bidi_levels(0), (1, 1, 1, 2, 2))
            s.cursor_position(4, 1)
            parse_bytes(s, b'\n')
            s.test_bidi_refresh()
            # rows 0-3 show slots 1, 2, 3, 0: only the pushed line, at row 3
            # in the reused slot, is dirty
            self.ae(s.historybuf.dirty_lines(), [0])
            buf = test_screen_update_cell_data(s)
            self.ae(s.scrolled_by, 4)
            f = self.screen(cols=5, lines=4, pixel_scroll=False)
            f.draw('אב 12')
            fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
            self.assertTrue(any(c.sprite_idx for c in fresh))
            for y in range(4):
                self.ae([c.sprite_idx for c in gpu_row(s, buf, y)], [c.sprite_idx for c in fresh], f'row {y}')

    def test_history_slot_reuse_reshapes_line_in_paused_frame(self):
        # A history slot is reused between the last live frame and the pause,
        # and paused frames compare levels with the last live frame's ids. P is
        # pushed into the slot of O and X, its continuation, into the slot of H,
        # which the last frame showed at levels (0, 0, 0, 1, 0). The pause must
        # forget those ids, otherwise X is compared as if it were H.
        with setup_for_testing():
            s = self.screen(cols=5, lines=4, scrollback=0, pixel_scroll=False)
            parse_bytes(s, b'o\r\n')
            s.draw('(12ב')  # H
            parse_bytes(s, b'\r\ni\r\nj\r\n')
            s.draw('אבגדה')  # P
            s.draw('(12ב')  # X, continuation of P
            parse_bytes(s, b'\r\na\r\nb')
            self.ae(s.historybuf.count, 4)
            test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(1), (1, 2, 2, 1, 0))
            s.scroll(10, True)
            test_screen_update_cell_data(s)
            self.ae(s.scrolled_by, 4)
            self.ae(s.test_bidi_levels(1), (0, 0, 0, 1, 0))
            s.cursor_position(1, 5)
            s.draw('ו')  # split P from X
            self.assertFalse(s.linebuf.is_continued(1))
            s.cursor_position(4, 1)
            parse_bytes(s, b'\n\n')  # P into the slot of O, X into the slot of H
            f = self.screen(cols=5, lines=4, pixel_scroll=False)
            f.draw('(12ב')
            fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
            self.assertTrue(any(c.sprite_idx for c in fresh))
            s.pause_rendering(True, 1000)
            s.test_bidi_refresh()
            self.ae(s.test_bidi_paused_dirty_lines(), [2, 3])
            buf = test_screen_update_cell_data(s)
            self.ae(s.test_bidi_levels(3), (0, 0, 0, 1, 0))
            self.ae([c.sprite_idx for c in gpu_row(s, buf, 3)], [c.sprite_idx for c in fresh])

    def test_dirty_row_below_view_redraws_paragraph(self):
        # scrolled up, a dirty linebuf row of the paragraph below the view
        # re-resolves the paragraph and redraws the on-screen rows whose levels
        # change. As in test_dirty_row_redraws_only_rows_whose_levels_change, row
        # 1 (12 3!) goes from (2, 2, 1, 2, 0) to (2, 2, 1, 2, 1) when ב is
        # written over the first cell of the next row. Linebuf row 1 (screen row 2)
        # changes; the two rows above it (linebuf row 0 and the history row of the
        # same paragraph) keep their levels and are not redrawn.
        with setup_for_testing():
            s = self.screen(cols=5, lines=3, scrollback=10)
            s.draw('אבגדה')
            s.draw('אבגדה')
            s.draw('12 3!')
            s.draw('abcde')
            self.ae(s.historybuf.count, 1)
            test_screen_update_cell_data(s)
            s.scroll(1, True)
            test_screen_update_cell_data(s)
            self.ae(s.scrolled_by, 1)
            self.ae(s.linebuf.dirty_lines(), [])
            self.ae(s.test_bidi_levels(2), (2, 2, 1, 2, 0))
            count = s.test_bidi_resolve_count()
            s.cursor_position(3, 1)
            s.draw('ב')
            self.ae(s.scrolled_by, 1)
            self.ae(s.linebuf.dirty_lines(), [2])
            s.test_bidi_refresh()
            self.ae(s.linebuf.dirty_lines(), [1, 2])
            self.ae(s.historybuf.dirty_lines(), [])
            self.ae(s.test_bidi_resolve_count() - count, 1)
            self.ae(s.test_bidi_levels(2), (2, 2, 1, 2, 1))

    def test_tall_paragraph_keeps_uploaded_maps(self):
        # One paragraph of 400 rows of 'א!' on a 300 line screen, 100 rows in the
        # history. Each ! (ON) lies between R letters, N1 makes it R (level 1) and
        # each row maps to (1, 0), except at the end of a resolved window, where the
        # ! lies between R and eos (L), N2 gives it level 0 and its row maps to
        # (0, 1). With BIDI_MAX_PARAGRAPH_ROWS = 256:
        # row 0 resolves [-100, 155], row 156 resolves [28, 283] and row 284
        # resolves [44, 299]. Later windows must not rewrite the maps of rows
        # already uploaded with an earlier one, so rows 155 and 283 keep the map
        # their glyphs were shaped with; 299 is the true end of the paragraph.
        s = self.screen(cols=2, lines=300, scrollback=200)
        s.draw('א!' * 400)
        self.ae(s.historybuf.count, 100)
        s.test_bidi_refresh()
        maps = [s.test_bidi_v2l(y) for y in range(s.lines)]
        self.ae(set(maps), {(0, 1), (1, 0)})
        self.ae([y for y, m in enumerate(maps) if m == (0, 1)], [155, 283, 299])

    # Paragraph cache. Every trigger starts from a screen warmed until a frame
    # resolves nothing, then must make the next frame resolve again, and the maps
    # must equal those of a fresh screen given the same content in one frame.
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
        # the maps of a screen filled with rows from the top, lines tall
        # (default: one per row), with nothing pushed into the history
        f = self.screen(cols=cols, lines=lines or len(rows))
        self.fill(f, rows)
        self.ae(f.historybuf.count, 0)
        self.frame(f)
        return self.maps(f)

    def trigger_case(self, build, trigger, expect=None, fresh=True):
        # build() returns a new screen; trigger(s) changes it. Returns the maps
        # after the trigger frame, equal to those of a fresh screen that sees the
        # trigger and one frame only (fresh=False when the first frame of a
        # screen changes the view, as the history count is applied then) and to
        # expect when given.
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

    def test_cache_reuses_unchanged_paragraphs(self):
        # Unchanged rows reuse their maps and resolve nothing; one written cell
        # makes only its own paragraph resolve again (trigger: a dirty row).
        # 'אבג abc' at the forced paragraph level 0: the letters are level 1,
        # the space (between R and L, N2) and abc level 0, so every row has the
        # non-trivial map (2, 1, 0, 3, 4, ...).
        with setup_for_testing():
            s = self.screen()
            rows = ['אבג abc'] * 3
            self.fill(s, rows)
            self.ae(self.frame(s), 3)
            self.ae(self.frame(s), 0)
            before = self.maps(s)
            self.assertTrue(all(before[y] is not None for y in range(3)))
            self.assertIsNone(before[3])
            s.current_selections()
            self.assertFalse(s.is_selection_dirty())
            s.cursor_position(2, 10)
            s.draw('z')
            self.ae(self.frame(s), 1)
            after = self.maps(s)
            self.ae(after[0], before[0])
            self.ae(after[2], before[2])
            f = self.screen()
            self.fill(f, rows)
            f.cursor_position(2, 10)
            f.draw('z')
            self.frame(f)
            self.ae(after, self.maps(f))
            self.ae(self.frame(s), 0)

    def test_cache_scroll_index(self):
        # Trigger: the screen scrolls (index at the last row). Every row now
        # holds another line, so no stamp may be trusted.
        rows = ['אבג abc', 'abc אבג', 'אב', 'xyz', 'אבגד']

        def build():
            s = self.screen()
            self.fill(s, rows)
            return s

        def trigger(s):
            parse_bytes(s, b'\x1b[5;1H\x1bD')

        with setup_for_testing():
            got = self.trigger_case(build, trigger)
            f = self.screen()
            self.fill(f, rows[1:])
            self.frame(f)
            self.ae(got, self.maps(f))

    def test_cache_reverse_index(self):
        # Trigger: reverse index at the top row moves every line down one row.
        rows = ['אבג abc', 'abc אבג', 'אב', 'xyz', 'אבגד']

        def build():
            s = self.screen()
            self.fill(s, rows)
            return s

        def trigger(s):
            parse_bytes(s, b'\x1b[1;1H\x1bM')

        with setup_for_testing():
            got = self.trigger_case(build, trigger)
            f = self.screen()
            self.fill(f, [''] + rows[:-1])
            self.frame(f)
            self.ae(got, self.maps(f))

    def test_cache_insert_delete_lines_net_zero(self):
        # Trigger: insert then delete a line at row 0 of the alt screen. The
        # insert pushes the paragraph's second row off the bottom, the delete
        # brings the long row back to row 3 with a blank row 4 under it. The
        # long row keeps its wrap flag, so row 4 still counts as its
        # continuation: the paragraph is still [3, 4] and matches the stored
        # bounds, only the linebuf movement generation forces the re-resolve.
        # 10 R letters at level 1 reverse fully: (9, ..., 0); the blank row 4
        # is all level 0 and unmapped.
        def build():
            s = self.screen()
            parse_bytes(s, b'\x1b[?1049h')
            s.cursor_position(4, 1)
            s.draw('אבגדהוזחטיכלמ')
            s.cursor_position(1, 1)
            return s

        def trigger(s):
            parse_bytes(s, b'\x1b[L\x1b[M')
            self.assertTrue(s.linebuf.is_continued(4))

        with setup_for_testing():
            rev = tuple(range(9, -1, -1))
            self.trigger_case(build, trigger, (None, None, None, rev, None))

    def test_cache_scrollback_view(self):
        # Trigger: scrolling the view up one line shows other lines on every row
        rows = ['אבג', 'abאב', 'אב12', 'xyz', 'אבגדה', 'a', 'אבג abc']

        def build():
            s = self.screen(cols=5, lines=3, scrollback=10)
            for r in rows:
                parse_bytes(s, r.encode() + b'\r\n')
            self.prerender(s)
            return s

        def trigger(s):
            s.scroll(1, True)

        with setup_for_testing():
            # the last line soft-wraps onto two rows, then comes a blank row: the
            # view showed 'אבג a', 'bc' and the blank row, one line up it shows
            # 'a' and the wrapped paragraph. The paragraph fits in the view both
            # times, so its stamps no longer match its render rows.
            self.trigger_case(build, trigger, self.plain_maps(rows[5:], 5, lines=3), fresh=False)

    # A soft-wrapped paragraph of three rows on 5 columns, of which one row is
    # out of view after the scroll. Its range-y bounds are the same before and
    # after, and the render rows it keeps on screen held two of its own rows last
    # frame, so their stamps still match: only the scroll in the frame key makes
    # it resolve again. The expected maps are those of the paragraph alone at the
    # top of a fresh screen.
    EDGE_PARAGRAPH = 'אבג abאב 12ab'

    def edge_paragraph_maps(self):
        f = self.screen(cols=5, lines=5)
        f.draw(self.EDGE_PARAGRAPH)
        self.ae(f.historybuf.count, 0)
        self.frame(f)
        m = self.maps(f)
        self.assertTrue(all(m[y] is not None for y in range(3)))
        self.ae(m[3:], (None, None))
        return m

    def test_cache_scrollback_view_crossing_bottom_edge(self):
        # Trigger: scrolling the view up one line. One Latin line in the
        # history, two on screen, then the paragraph on screen rows 2-4. After
        # the scroll the view shows the history line, the two Latin lines and
        # the paragraph's first two rows; its third row is below the view. The
        # extra leading render row of pixel scroll is above the history line,
        # where there is no line.
        def build():
            s = self.screen(cols=5, lines=5, scrollback=10)
            parse_bytes(s, b'x0\r\nx1\r\nx2\r\n')
            s.draw(self.EDGE_PARAGRAPH)
            self.assertTrue(s.linebuf.is_continued(4))
            self.prerender(s)
            self.ae(s.historybuf.count, 1)
            return s

        def trigger(s):
            s.scroll(1, True)

        with setup_for_testing():
            m = self.edge_paragraph_maps()
            self.trigger_case(build, trigger, (None, None, None, m[0], m[1]), fresh=False)

    def test_cache_scrollback_view_crossing_top_edge(self):
        # Trigger: scrolling the view down one line. The paragraph then four
        # Latin lines and a blank row: the paragraph is the whole history.
        # Scrolled up 3 the view shows the paragraph on rows 0-2; one line down
        # its first row is above the view and its other two on rows 0-1. Pixel
        # scroll is off, as its extra leading render row would still show the
        # first row.
        def build():
            s = self.screen(cols=5, lines=5, scrollback=10, pixel_scroll=False)
            s.draw(self.EDGE_PARAGRAPH)
            parse_bytes(s, b'\r\nx1\r\nx2\r\nx3\r\nx4\r\n')
            self.prerender(s)
            self.ae(s.historybuf.count, 3)
            s.scroll(3, True)
            return s

        def trigger(s):
            s.scroll(1, False)

        with setup_for_testing():
            m = self.edge_paragraph_maps()
            self.trigger_case(build, trigger, (m[1], m[2], None, None, None), fresh=False)

    def test_cache_history_add_while_scrolled(self):
        # Trigger: scrolled up, a line enters the history; the view follows it
        # and every row shows another line than before
        rows = ['אבג', 'abאב', 'אב12', 'xyz', 'אבגדה', 'a']

        def build():
            s = self.screen(cols=5, lines=3, scrollback=10)
            for r in rows:
                parse_bytes(s, r.encode() + b'\r\n')
            # render every history line once (they stay dirty until first shown),
            # which also applies the history count, then scroll up 2
            self.prerender(s)
            s.scroll(2, True)
            return s

        def trigger(s):
            parse_bytes(s, '\x1b[3;1H\nאב'.encode())

        with setup_for_testing():
            # the view follows its lines into the history and shows the same text
            self.trigger_case(build, trigger, self.plain_maps(rows[2:5], 5), fresh=False)

    def test_cache_resize_rewrap(self):
        # Trigger: a wider screen rewraps the paragraph. 13 R letters on one row
        # reverse fully: (12, ..., 0).
        def build():
            s = self.screen()
            s.draw('אבגדהוזחטיכלמ')
            return s

        def trigger(s):
            s.resize(5, 13)

        with setup_for_testing():
            rev = tuple(range(12, -1, -1))
            self.trigger_case(build, trigger, (rev, None, None, None, None))

    def test_cache_option_toggle(self):
        # Trigger: bidi_reorder off then on, force_ltr on then off. Either change
        # drops the maps, undoing it must resolve again.
        # 'abc' + three R letters: L L L R R R, maps (0, 1, 2, 5, 4, 3, ...).
        want = (0, 1, 2, 5, 4, 3, 6, 7, 8, 9)
        with setup_for_testing():
            try:
                for off in ({'bidi_reorder': False}, {'bidi_reorder': True, 'force_ltr': True}):
                    s = self.screen()
                    s.draw('abc' + A)
                    self.frame(s)
                    self.warm(s)
                    self.ae(s.test_bidi_v2l(0), want)
                    self.set_options(off)
                    self.frame(s)
                    self.assertIsNone(s.test_bidi_v2l(0), off)
                    self.set_options({'bidi_reorder': True, 'force_ltr': False})
                    self.assertGreater(self.frame(s), 0, off)
                    self.ae(s.test_bidi_v2l(0), want, off)
                    self.ae(self.frame(s), 0)
            finally:
                self.set_options({'bidi_reorder': True, 'force_ltr': False})

    def test_cache_losing_and_gaining_rtl(self):
        # Trigger: a dirty row. Overwriting the only RTL run leaves a Latin row,
        # which resolves nothing, writing Hebrew into a Latin row resolves it.
        with setup_for_testing():
            s = self.screen()
            s.draw('abc' + A)
            self.frame(s)
            self.warm(s)
            s.cursor_position(1, 4)
            s.draw('xyz')
            self.frame(s)
            self.assertIsNone(s.test_bidi_v2l(0))
            self.warm(s)
            s.cursor_position(1, 4)
            s.draw(A)
            self.ae(self.frame(s), 1)
            self.ae(s.test_bidi_v2l(0), (0, 1, 2, 5, 4, 3, 6, 7, 8, 9))
            self.ae(self.frame(s), 0)

    def test_cache_tall_paragraph_always_resolves(self):
        # A paragraph cut by the row cap is never cached, as its bounds depend on
        # the row it is entered from: unchanged frames still resolve it.
        with setup_for_testing():
            s = self.screen(cols=2, lines=300, scrollback=200)
            s.draw('א!' * 400)
            self.assertGreater(self.frame(s), 0)
            first = self.maps(s)
            self.assertGreater(self.frame(s), 0)
            self.ae(self.maps(s), first)
            self.assertGreater(self.frame(s), 0)

    def test_cache_paused_rendering(self):
        # Trigger: rendering is paused, then resumed. Paused frames read a
        # snapshot and are not cached, the live frame after must resolve again.
        def build():
            s = self.screen()
            self.fill(s, ['אבג abc', 'abc אבג'])
            return s

        with setup_for_testing():
            s = build()
            self.frame(s)
            self.warm(s)
            s.pause_rendering(True, 1000)
            self.frame(s)
            s.pause_rendering(False)
            self.assertGreater(self.frame(s), 0)
            f = build()
            self.frame(f)
            self.ae(self.maps(s), self.maps(f))

    def test_cache_overlay_row(self):
        # Trigger: an overlay line on a Hebrew row, then removed. The row is
        # unmapped while the overlay shows and mapped again afterwards.
        want = (0, 1, 2, 5, 4, 3, 6, 7, 8, 9)
        with setup_for_testing():
            s = self.screen()
            s.draw('abc' + A)
            s.cursor_position(1, 9)
            self.frame(s)
            self.warm(s)
            self.ae(s.test_bidi_v2l(0), want)
            s.test_draw_overlay_line('xy', 8, 0)
            self.frame(s)
            self.assertIsNone(s.test_bidi_v2l(0))
            s.test_update_overlay_text('')
            self.assertGreater(self.frame(s), 0)
            self.ae(s.test_bidi_v2l(0), want)

    def test_cache_reset(self):
        # Trigger: RIS. Cleared rows take the plain row path whatever the cache
        # says, so this checks the maps, not that the reset invalidated the cache.
        want = (0, 1, 2, 5, 4, 3, 6, 7, 8, 9)
        with setup_for_testing():
            s = self.screen()
            s.draw('abc' + A)
            self.frame(s)
            self.warm(s)
            parse_bytes(s, b'\x1bc')
            self.frame(s)
            self.ae(self.maps(s), (None,) * 5)
            s.draw('abc' + A)
            self.assertGreater(self.frame(s), 0)
            self.ae(s.test_bidi_v2l(0), want)

    # Cache shortcut: a paragraph whose dirty rows still feed FriBidi the same
    # class, bracket and cell layout keeps its maps without resolving; any other
    # edit resolves. One paragraph over rows 0-2 of a 10x4 screen at paragraph
    # level 0: 'alpha שלום' / ' beta עולם' / ' (12) gamm'. A space between L and
    # R takes level 0 (N2). The brackets enclose EN (R for N0) and the strong
    # type before them is R (עולם), so N0 makes them R; the space before ( then
    # lies between R and R (N1, level 1). W2/W7 keep 12 as EN (last strong is R),
    # level 2 by I1. L2 reverses each run at level 1 or more, highest first.
    MIXED = 'alpha שלום beta עולם (12) gamma'[:30]
    MIXED_LEVELS = ((0,) * 6 + (1,) * 4,) * 2 + ((1, 1, 2, 2, 1, 0, 0, 0, 0, 0), None)
    MIXED_V2L = ((0, 1, 2, 3, 4, 5, 9, 8, 7, 6),) * 2 + ((4, 2, 3, 1, 0, 5, 6, 7, 8, 9), None)

    def levels(self, s):
        return tuple(s.test_bidi_levels(y) for y in range(s.lines))

    def test_cache_same_class_edit_skips_resolve(self):
        # x over the a at row 0 col 0: L for L, no bracket, same cell. Every
        # FriBidi input is unchanged, so the maps stay MIXED's and only the
        # written row is reshaped.
        with setup_for_testing():
            s = self.screen(lines=4, scrollback=0)
            s.draw(self.MIXED)
            self.ae(self.frame(s), 1)
            self.ae(self.frame(s), 0)
            self.ae(self.maps(s), self.MIXED_V2L)
            self.ae(self.levels(s), self.MIXED_LEVELS)
            self.ae(s.linebuf.dirty_lines(), [])
            count = s.test_bidi_resolve_count()
            s.cursor_position(1, 1)
            s.draw('x')
            s.test_bidi_refresh()
            self.ae(s.test_bidi_resolve_count() - count, 0)
            self.ae(s.linebuf.dirty_lines(), [0])
            buf = test_screen_update_cell_data(s)
            self.ae(s.test_bidi_resolve_count() - count, 0)
            self.ae(self.maps(s), self.MIXED_V2L)
            self.ae(self.levels(s), self.MIXED_LEVELS)
            f = self.screen(lines=4, scrollback=0)
            f.draw('x' + self.MIXED[1:])
            fbuf = test_screen_update_cell_data(f)
            self.ae(self.maps(s), self.maps(f))
            self.ae(self.levels(s), self.levels(f))
            for y in range(3):
                fresh = gpu_row(f, fbuf, y)
                self.assertTrue(any(c.sprite_idx for c in fresh))
                self.ae([c.sprite_idx for c in gpu_row(s, buf, y)], [c.sprite_idx for c in fresh], f'row {y}')

    def test_cache_level_changing_edits_resolve(self):
        # An edit that changes a class, a bracket or the layout must resolve
        # again and give a fresh screen's maps. Row 0, paragraph level 0.
        cases = (
            # (i) א(ב)c: ( and ) pair and enclose R with R before them, N0 makes
            # both R: levels 1 1 1 1 0. [ for ( keeps class ON but ends the pair:
            # [ lies between R and R (N1, 1), ) between R and L (N2, 0).
            ('i bracket', 10, 'א(ב)c', 2, '[', (1, 1, 1, 1, 0) + (0,) * 5, (3, 2, 1, 0) + ID10[4:], (1, 1, 1, 0, 0) + (0,) * 5, (2, 1, 0) + ID10[3:]),
            # (ii) אa!ב: ! between L and R (N2, 0), each R alone, so v2l is the
            # identity. ג for a: ! between R and R (N1, 1), cols 0-3 reverse.
            ('ii class', 10, 'אa!ב', 2, 'ג', (1, 0, 0, 1) + (0,) * 6, ID10, (1, 1, 1, 1) + (0,) * 6, (3, 2, 1, 0) + ID10[4:]),
            # (iii) 中בגd (cells 0-1, 2, 3, 4) to aבג中 (0, 1, 2, 3-4): the fed
            # classes are L R R L both times, only the cells they start in move.
            # The R pair reverses in place: v2l 0 1 3 2 4, then 0 2 1 3 4.
            ('iii layout', 5, '中בגd', 1, 'aבג中', (0, 0, 1, 1, 0), (0, 1, 3, 2, 4), (0, 1, 1, 0, 0), (0, 2, 1, 3, 4)),
        )
        with setup_for_testing():
            for name, cols, text, col, new, levels0, v2l0, levels1, v2l1 in cases:
                with self.subTest(name):
                    s = self.screen(cols=cols)
                    s.draw(text)
                    self.frame(s)
                    self.warm(s)
                    self.ae((s.test_bidi_levels(0), s.test_bidi_v2l(0)), (levels0, v2l0))
                    s.cursor_position(1, col)
                    s.draw(new)
                    self.ae(self.frame(s), 1)
                    self.ae((s.test_bidi_levels(0), s.test_bidi_v2l(0)), (levels1, v2l1))
                    f = self.screen(cols=cols)
                    f.draw(text[: col - 1] + new + text[col - 1 + len(new) :])
                    self.frame(f)
                    self.ae(self.maps(s), self.maps(f))
                    self.ae(self.levels(s), self.levels(f))
            with self.subTest('iv dirty row below the view'):
                # As in test_dirty_row_below_view_redraws_paragraph: one paragraph
                # over a history row and linebuf rows 0-2, scrolled up by one, so
                # linebuf row 2 is below the view. z for b there keeps class L and
                # no bracket, but a row that is not on screen never skips the
                # resolve. Screen row 2 (12 3!) stays (2, 2, 1, 2, 0): ! lies
                # between 3 (EN, R for N1) and a (L), N2.
                def build():
                    s = self.screen(cols=5, lines=3, scrollback=10)
                    for line in ('אבגדה', 'אבגדה', '12 3!', 'abcde'):
                        s.draw(line)
                    self.ae(s.historybuf.count, 1)
                    return s

                s = build()
                self.frame(s)
                s.scroll(1, True)
                self.frame(s)
                self.warm(s)
                self.ae(s.scrolled_by, 1)
                s.cursor_position(3, 2)
                s.draw('z')
                self.ae(s.scrolled_by, 1)
                self.ae(s.linebuf.dirty_lines(), [2])
                self.ae(self.frame(s), 1)
                self.ae(s.test_bidi_levels(2), (2, 2, 1, 2, 0))
                s.scroll(1, False)
                self.frame(s)
                f = self.screen(cols=5, lines=3, scrollback=10)
                for line in ('אבגדה', 'אבגדה', '12 3!', 'azcde'):
                    f.draw(line)
                self.frame(f)
                self.ae(self.maps(s), self.maps(f))
                self.ae(self.levels(s), self.levels(f))

            with self.subTest('v explicit embedding then a blank'):
                # ב/, an OSC 66 cell holding RLO, then EM SPACE (WS, not a blank,
                # so not trimmed). A blank for EM SPACE keeps class WS, so the
                # shortcut keeps the maps: they must be those of a resolve over
                # the final text, so the trim must not drop the blank.
                def rlo(s, last):
                    s.draw('ב/')
                    parse_bytes(s, '\x1b]66;w=1;\u202e\x07'.encode())
                    s.draw(last)
                    self.ae(str(s.linebuf.line(0))[2], '\u202e')

                s = self.screen(cols=4, lines=1, scrollback=0)
                rlo(s, '\u2003')
                self.frame(s)
                self.warm(s)
                s.cursor_position(1, 4)
                s.draw(' ')
                self.frame(s)
                f = self.screen(cols=4, lines=1, scrollback=0)
                rlo(f, ' ')
                self.frame(f)
                self.ae((s.test_bidi_levels(0), s.test_bidi_v2l(0)), ((1, 1, 0, 0), (1, 0, 2, 3)))
                self.ae(self.maps(s), self.maps(f))
                self.ae(self.levels(s), self.levels(f))

            # A wide character over a cell whose class it keeps: the fed classes
            # match at every cell a character starts in, only a cell that used
            # to start one is now a continuation. (vi) אa!ב, 😀 over !a: the
            # fed R ON R match R ON . R at cols 0, 1, 3; the old a at col 2 is
            # now a continuation. 😀 lies between R and R (N1, 1). (vii) אבג!ד
            # on 5 cols, 😀 over !ד: the old ד at col 4, past the last fed
            # character, is now a continuation. 😀 lies between R and eos (N2, 0).
            continuation_cases = (
                ('vi interior continuation', 10, 'א!aב', 2, 'א😀ב', (1, 0, 0, 1) + (0,) * 6, (1, 1, 1, 1) + (0,) * 6),
                ('vii trailing continuation', 5, 'אבג!ד', 4, 'אבג😀', (1, 1, 1, 1, 1), (1, 1, 1, 0, 0)),
            )
            for name, cols, text, col, final, levels0, levels1 in continuation_cases:
                with self.subTest(name):
                    s = self.screen(cols=cols, lines=1, scrollback=0)
                    s.draw(text)
                    self.frame(s)
                    self.warm(s)
                    self.ae(s.test_bidi_levels(0), levels0)
                    s.cursor_position(1, col)
                    s.draw('😀')
                    self.ae(str(s.linebuf.line(0)), final)
                    self.ae(self.frame(s), 1)
                    self.ae(s.test_bidi_levels(0), levels1)
                    f = self.screen(cols=cols, lines=1, scrollback=0)
                    f.draw(final)
                    self.frame(f)
                    self.ae(self.maps(s), self.maps(f))
                    self.ae(self.levels(s), self.levels(f))

    def test_cache_bypass_between_resolves(self):
        # Frame A resolves MIXED. Frame B draws a scale-2 multicell over rows
        # 0-1, cols 0-1: the bypass makes the paragraph inactive under valid
        # stamps. Frame C restores the four cells; the rows feed FriBidi what
        # they fed in frame A, but the maps are frame B's inactive ones, so C
        # must resolve and give MIXED's maps. The paragraph keeps its bounds in
        # every frame, so only the signatures can tell B's maps from A's.
        with setup_for_testing():
            s = self.screen(lines=4, scrollback=0)
            s.draw(self.MIXED)
            cont = [s.linebuf.is_continued(y) for y in range(4)]
            self.ae(cont, [False, True, True, False])
            self.ae(self.frame(s), 1)
            self.warm(s)
            self.ae(self.maps(s), self.MIXED_V2L)
            s.cursor_position(1, 1)
            draw_multicell(s, 'x', scale=2)
            self.ae([s.linebuf.is_continued(y) for y in range(4)], cont)
            self.frame(s)
            self.ae(self.maps(s), (None,) * 4)
            s.cursor_position(1, 1)
            s.draw(self.MIXED[:2])
            s.cursor_position(2, 1)
            s.draw(self.MIXED[10:12])
            self.ae([s.linebuf.is_continued(y) for y in range(4)], cont)
            self.ae(str(s.linebuf.line(0)) + str(s.linebuf.line(1)), self.MIXED[:20])
            self.ae(self.frame(s), 1)
            self.ae(self.maps(s), self.MIXED_V2L)
            self.ae(self.levels(s), self.MIXED_LEVELS)

    def test_pointed_hebrew_shaping(self):
        # (n) shin + qamats + shin dot, lamed, vav + holam, final mem: 4 cells
        # (marks join their base cell). Shaped RTL with the test font, every
        # group must cover exactly one cell.
        text = 'שָׁלוֹם'
        with tempfile.TemporaryDirectory() as tdir:
            path = os.path.join(tdir, 'LiberationMono-Regular.ttf')
            with open(path, 'wb') as f:
                f.write(testing_font_data('LiberationMono-Regular.ttf'))
            groups = shape_string(text, path=path, rtl=True)
        self.ae(len(groups), 4, groups)
        self.ae([g[0] for g in groups], [1, 1, 1, 1], groups)

    def test_trailing_blank_trim_keeps_maps(self):
        # Dropping the blanks at a paragraph's end before resolving must not
        # change any row's maps (L1 resets them to the paragraph level)
        def wrapped_blank(s):
            s.draw('א' * 10)
            s.draw('ב')
            s.cursor_position(2, 1)
            s.erase_characters(1)
            self.assertTrue(s.linebuf.is_continued(1))

        def rlo_cell(s):
            # draw drops a zero-width Cf, an OSC 66 cell keeps it. Untrimmed, the
            # blanks after the RLO sit at its level and set the eos of the run
            # before them (X10), so trimming would change the maps.
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
        with setup_for_testing():
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
                        # with auto on, only the LTR start gives an LTR paragraph
                        rtl = auto and name != 'LTR start'
                        for s in screens:
                            self.assertIs(s.test_bidi_paragraph_rtl(0), rtl)
                        for y in range(5):
                            self.ae(screens[0].test_bidi_v2l(y), screens[1].test_bidi_v2l(y), f'v2l row {y}')
                            self.ae(screens[0].test_bidi_levels(y), screens[1].test_bidi_levels(y), f'levels row {y}')
                        if name == 'ES next to EN':
                            self.ae(counts, [1, 1])

    # Right-to-left paragraphs (bidi_auto_direction). Creating a screen resets
    # the global options, so every screen sets the mode, and each is read before
    # a screen of the other mode is created.
    def direction_maps(self, text, auto, cols=10):
        # the maps, levels and paragraph direction of each row of a screen
        # given text, in one frame
        s = self.screen(cols=cols, bidi_auto_direction=auto)
        s.draw(text)
        s.test_bidi_refresh()
        return self.maps(s), self.levels(s), tuple(s.test_bidi_paragraph_rtl(y) for y in range(s.lines))

    def test_rtl_paragraph_right_aligned(self):
        with setup_for_testing():
            # אבג, auto: the first strong character is R (P2-P3), so the
            # paragraph level is 1. R at an odd level stays 1 (I2), and the
            # trailing blanks take the paragraph level (L1). All level 1, so
            # L2 reverses the whole row and the word is right-aligned.
            s = self.screen(bidi_auto_direction=True)
            fgs = draw_coloured(s, A)
            row = gpu_row(s, test_screen_update_cell_data(s), 0)
            self.ae(s.test_bidi_levels(0), (1,) * 10)
            self.ae(s.test_bidi_v2l(0), ID10[::-1])
            self.assertIs(s.test_bidi_paragraph_rtl(0), True)
            self.ae([c.fg for c in row[7:]], fgs[::-1])
            self.assertTrue(all(c.sprite_idx for c in row[7:]))
            self.ae([c.sprite_idx for c in row[:7]], [0] * 7)
            # the cursor, after the text, is on logical 3: visual 9 - 3
            self.ae(s.cursor.x, 3)
            self.ae(s.test_bidi_visual_x(3, 0), 6)
            # auto off: paragraph level 0, the letters 1, the blanks 0 (L1)
            f = self.screen(bidi_auto_direction=False)
            draw_coloured(f, A)
            frow = gpu_row(f, test_screen_update_cell_data(f), 0)
            self.ae(f.test_bidi_levels(0), (1, 1, 1) + (0,) * 7)
            self.ae(f.test_bidi_v2l(0), (2, 1, 0) + ID10[3:])
            self.assertIs(f.test_bidi_paragraph_rtl(0), False)
            self.ae(row[9].sprite_idx, frow[2].sprite_idx)
        # שלום!, auto: the ! lies between R and eos, which is R at paragraph
        # level 1, so N1 makes it R: all level 1, the ! (logical 4) at visual 5,
        # left of the word
        maps, levels, rtl = self.direction_maps('שלום!', True)
        self.ae((levels[0], maps[0], rtl[0]), ((1,) * 10, ID10[::-1], True))
        # auto off: eos is L at level 0, so N2 gives the ! level 0, right of the word
        maps, levels, rtl = self.direction_maps('שלום!', False)
        self.ae((levels[0], maps[0], rtl[0]), ((1, 1, 1, 1) + (0,) * 6, (3, 2, 1, 0) + ID10[4:], False))

    def test_rtl_paragraph_direction_from_first_strong(self):
        # אבג abc: R first, paragraph level 1. The space between R and L takes
        # the embedding direction, R (N2): 1. L at an odd level is 2 (I2), the
        # blanks 1 (L1). L2 reverses cols 4-6, then the whole row.
        maps, levels, rtl = self.direction_maps('אבג abc', True)
        self.ae(levels[0], (1, 1, 1, 1, 2, 2, 2, 1, 1, 1))
        self.ae(maps[0], (9, 8, 7, 4, 5, 6, 3, 2, 1, 0))
        self.assertIs(rtl[0], True)
        # abc אבג: L first, LTR, so the same as auto off. The space between L
        # and R takes the embedding direction, L (N2): 0. Only cols 4-6 reverse.
        on = self.direction_maps('abc אבג', True)
        off = self.direction_maps('abc אבג', False)
        self.ae(on[:2], off[:2])
        self.ae((on[1][0], on[0][0]), ((0, 0, 0, 0, 1, 1, 1, 0, 0, 0), (0, 1, 2, 3, 6, 5, 4, 7, 8, 9)))
        self.assertIs(on[2][0], False)
        # 12 אב ab: EN is weak, so R is the first strong, level 1. W2 and W7
        # keep EN (sos is R). The first space lies between EN (R for N1) and R:
        # 1. The second between R and L: e, 1 (N2). EN and L at an odd level
        # are 2 (I2), the blanks 1 (L1). L2 reverses cols 0-1 and 6-7, then
        # the whole row: 1 0 2 3 4 5 7 6 8 9 reversed.
        maps, levels, rtl = self.direction_maps('12 אב ab', True)
        self.ae(levels[0], (2, 2, 1, 1, 1, 1, 2, 2, 1, 1))
        self.ae(maps[0], (9, 8, 6, 7, 5, 4, 3, 2, 0, 1))
        self.assertIs(rtl[0], True)
        # (12 a soft-wrapped into אבגדה on 5 columns: the first strong is the
        # a on row 0, so LTR. W7 makes 12 L (sos L), ( and the space lie
        # between Ls (N1): row 0 is all 0, inactive. Row 1 is R at level 1.
        on = self.direction_maps('(12 aאבגדה', True, cols=5)
        off = self.direction_maps('(12 aאבגדה', False, cols=5)
        self.ae((on[0][:2], on[1][:2]), ((None, (4, 3, 2, 1, 0)), (None, (1,) * 5)))
        self.ae((on[0][:2], on[1][:2]), (off[0][:2], off[1][:2]))
        self.ae(on[2][:2], (False, False))
        # אבגדה soft-wrapped into abcde on 5 columns: RTL. Row 0 is R at level
        # 1. Row 1 is L at an odd level, 2 (I2), with no blank: active, and L2
        # reverses it twice, so its map is the identity.
        maps, levels, rtl = self.direction_maps('אבגדהabcde', True, cols=5)
        self.ae(levels[:2], ((1,) * 5, (2,) * 5))
        self.ae(maps[:2], ((4, 3, 2, 1, 0), (0, 1, 2, 3, 4)))
        self.ae(rtl[:2], (True, True))
        # مرحبا! (Arabic letters): AL is a strong R-type character, so P2 finds
        # it first and the paragraph level is 1. W3 turns AL to R, so the word
        # is R like Hebrew. The ! (ON) lies between R and the blanks (eos at
        # level 1, R): N1 gives it R, 1. The blanks are 1 (L1): all level 1,
        # L2 reverses the whole row, the ! lands at visual 4, left of the word.
        maps, levels, rtl = self.direction_maps('مرحبا!', True)
        self.ae(levels[0], (1,) * 10)
        self.ae(maps[0], tuple(range(9, -1, -1)))
        self.assertIs(rtl[0], True)

    def test_rtl_weak_only_paragraph_stays_ltr(self):
        # Guard. ١٢٣ 45! has no strong character (AN, WS, EN, ON): FriBidi
        # leaves the direction ON, which is LTR at level 0. W7 makes 45 L
        # (sos L), the space lies between AN (R for N1) and L, so e, 0 (N2),
        # the ! between L and eos (N1): 0. AN at an even level is 2 (I1). L2
        # reverses cols 0-2 twice: the identity, on an active row.
        text = '١٢٣ 45!'
        on = self.direction_maps(text, True)
        off = self.direction_maps(text, False)
        self.ae(on[:2], off[:2])
        self.ae((on[1][0], on[0][0]), ((2, 2, 2) + (0,) * 7, ID10))
        self.assertIs(on[2][0], False)

    # אבגדה soft-wrapped into ab 1 on 5 columns, auto: R first, paragraph level
    # 1. Row 0 is R at level 1. Row 1: W7 makes the 1 L (the last strong is
    # the b), the space lies between Ls (N1), and L at an odd level is 2 (I2);
    # the blank in col 4 is 1 (L1). L2 reverses cols 0-3, then 0-4: 4 0 1 2 3.
    RTL_WRAPPED_V2L = ((4, 3, 2, 1, 0), (4, 0, 1, 2, 3), None, None)
    RTL_WRAPPED_LEVELS = ((1,) * 5, (2, 2, 2, 2, 1), None, None)
    # x for the א: L first, paragraph level 0. Row 0: R at an even level is 1
    # (I1), cols 1-4 reverse. Row 1 resolves as above, all L: level 0, inactive.
    LTR_WRAPPED_V2L = ((0, 4, 3, 2, 1), None, None, None)
    LTR_WRAPPED_LEVELS = ((0, 1, 1, 1, 1), None, None, None)

    def wrapped_rtl_screen(self, first='א'):
        s = self.screen(cols=5, lines=4, scrollback=0, bidi_auto_direction=True)
        s.draw(first + 'בגדה')
        s.draw('ab 1')
        return s

    def assert_same_upload(self, s, buf, f, fbuf, rows):
        for y in rows:
            fresh = gpu_row(f, fbuf, y)
            self.assertTrue(any(c.sprite_idx for c in fresh))
            self.ae([c.sprite_idx for c in gpu_row(s, buf, y)], [c.sprite_idx for c in fresh], f'row {y}')

    def test_rtl_first_strong_edit_flips_direction(self):
        # Writing the first strong character, R to L and back, flips the whole
        # paragraph. Row 1 is not written, but its levels change, so it must be
        # reshaped.
        with setup_for_testing():
            s = self.wrapped_rtl_screen()
            self.ae(self.frame(s), 1)
            self.warm(s)
            self.ae((self.maps(s), self.levels(s)), (self.RTL_WRAPPED_V2L, self.RTL_WRAPPED_LEVELS))
            self.ae([s.test_bidi_paragraph_rtl(y) for y in (0, 1)], [True, True])
            steps = (('x', False, self.LTR_WRAPPED_V2L, self.LTR_WRAPPED_LEVELS), ('א', True, self.RTL_WRAPPED_V2L, self.RTL_WRAPPED_LEVELS))
            for ch, rtl, v2l, levels in steps:
                with self.subTest(ch):
                    s.cursor_position(1, 1)
                    s.draw(ch)
                    count = s.test_bidi_resolve_count()
                    s.test_bidi_refresh()
                    self.ae(s.test_bidi_resolve_count() - count, 1)
                    self.ae([s.test_bidi_paragraph_rtl(y) for y in (0, 1)], [rtl, rtl])
                    self.ae(s.linebuf.dirty_lines(), [0, 1])
                    buf = test_screen_update_cell_data(s)
                    self.ae((self.maps(s), self.levels(s)), (v2l, levels))
                    f = self.wrapped_rtl_screen(ch)
                    fbuf = test_screen_update_cell_data(f)
                    self.ae((self.maps(s), self.levels(s)), (self.maps(f), self.levels(f)))
                    self.assert_same_upload(s, buf, f, fbuf, (0, 1))

    def test_rtl_first_strong_same_class_edit_skips_resolve(self):
        # Guard. ב over the first strong א: R for R, no bracket, same cell. The
        # direction is a function of the classes alone (P2-P3), so the
        # signatures match, the shortcut keeps the maps, and they stay RTL.
        with setup_for_testing():
            s = self.wrapped_rtl_screen()
            self.frame(s)
            self.warm(s)
            count = s.test_bidi_resolve_count()
            s.cursor_position(1, 1)
            s.draw('ב')
            s.test_bidi_refresh()
            self.ae(s.test_bidi_resolve_count() - count, 0)
            self.ae(s.linebuf.dirty_lines(), [0])
            buf = test_screen_update_cell_data(s)
            self.ae(s.test_bidi_resolve_count() - count, 0)
            self.ae((self.maps(s), self.levels(s)), (self.RTL_WRAPPED_V2L, self.RTL_WRAPPED_LEVELS))
            self.ae([s.test_bidi_paragraph_rtl(y) for y in (0, 1)], [True, True])
            f = self.wrapped_rtl_screen('ב')
            fbuf = test_screen_update_cell_data(f)
            self.ae((self.maps(s), self.levels(s)), (self.maps(f), self.levels(f)))
            self.assert_same_upload(s, buf, f, fbuf, (0, 1))

    def test_rtl_option_toggle_reresolves(self):
        # Toggling bidi_auto_direction changes every paragraph's direction
        # without dirtying a row, so the frame must re-resolve and reshape
        with setup_for_testing():
            expect = {}
            for auto in (False, True):
                f = self.screen(bidi_auto_direction=auto)
                f.draw(A)
                fbuf = test_screen_update_cell_data(f)
                expect[auto] = (self.maps(f), [c.sprite_idx for c in gpu_row(f, fbuf, 0)])
            self.assertNotEqual(expect[False][0], expect[True][0])
            s = self.screen(bidi_auto_direction=True)
            s.draw(A)
            self.ae(self.frame(s), 1)
            self.warm(s)
            self.ae(self.maps(s), expect[True][0])
            self.assertIs(s.test_bidi_paragraph_rtl(0), True)
            for auto in (False, True):
                with self.subTest(auto=auto):
                    self.set_options({'bidi_reorder': True, 'bidi_auto_direction': auto})
                    self.ae(s.linebuf.dirty_lines(), [])
                    count = s.test_bidi_resolve_count()
                    s.test_bidi_refresh()
                    self.assertGreater(s.test_bidi_resolve_count() - count, 0)
                    self.ae(s.linebuf.dirty_lines(), [0])
                    buf = test_screen_update_cell_data(s)
                    self.ae(self.maps(s), expect[auto][0])
                    self.ae([c.sprite_idx for c in gpu_row(s, buf, 0)], expect[auto][1])
                    self.assertIs(s.test_bidi_paragraph_rtl(0), auto)
                    self.warm(s)

    def test_rtl_paragraph_cache_triggers(self):
        # Cache triggers on RTL paragraphs (auto direction on): a dirty row,
        # index, reverse index, insert then delete line, view scroll at both
        # edges, resize with rewrap and a paused frame (the paragraph split has
        # its own test). Each re-resolves and equals a fresh screen, a quiet
        # frame resolves 0.
        # The expected maps are the right-aligned ones, worked out by hand:
        # 'אבג' and ten or fewer letters reverse fully (L1 gives the trailing
        # blanks level 1), 'אבג abc' has abc at level 2: (9, 8, 7, 4, 5, 6, 3, 2, 1, 0).
        # A blank continuation row of an RTL paragraph is all level 1: reversed.
        rev = ID10[::-1]
        mix = (9, 8, 7, 4, 5, 6, 3, 2, 1, 0)
        rev5 = (4, 3, 2, 1, 0)
        long = 'אבגדהוזחטיכלמ'
        para = 'אבגדהוזחטיכלמנ'
        base = (rev, mix, rev, rev, None)

        def basic():
            # two rows, then a Hebrew-first paragraph soft-wrapped onto rows 2-3,
            # whose first row ends in a blank (L1 keeps it at level 1: map start)
            s = self.screen(bidi_auto_direction=True)
            parse_bytes(s, 'אבג\r\nאבג abc\r\nאבגדהוזחט אבג'.encode())
            self.assertTrue(s.linebuf.is_continued(3))
            s.cursor_position(1, 1)
            return s

        def alt():
            s = self.screen(bidi_auto_direction=True)
            parse_bytes(s, b'\x1b[?1049h')
            self.fill(s, ['אבג', 'אבג abc'])
            s.cursor_position(4, 1)
            s.draw(long)
            s.cursor_position(1, 1)
            return s

        def edge_bottom():
            s = self.screen(cols=5, lines=5, scrollback=10, bidi_auto_direction=True)
            parse_bytes(s, b'x0\r\nx1\r\nx2\r\n')
            s.draw(para)
            self.prerender(s)
            self.ae(s.historybuf.count, 1)
            return s

        def edge_top():
            s = self.screen(cols=5, lines=5, scrollback=10, pixel_scroll=False, bidi_auto_direction=True)
            s.draw(para)
            parse_bytes(s, b'\r\nx1\r\nx2\r\nx3\r\nx4\r\n')
            self.prerender(s)
            self.ae(s.historybuf.count, 3)
            s.scroll(3, True)
            return s

        def resized():
            s = self.screen(bidi_auto_direction=True)
            s.draw(long)
            return s

        def paused(s):
            s.pause_rendering(True, 1000)
            self.frame(s)
            s.pause_rendering(False)

        def dirty(s):
            s.cursor_position(4, 4)
            s.draw('ד')

        cases = (
            ('dirty row', basic, dirty, base),
            ('index', basic, lambda s: parse_bytes(s, b'\x1b[5;1H\x1bD'), (mix, rev, rev, None, None)),
            ('reverse index', basic, lambda s: parse_bytes(s, b'\x1b[1;1H\x1bM'), (None, rev, mix, rev, rev)),
            ('insert then delete line', alt, lambda s: parse_bytes(s, b'\x1b[L\x1b[M'), (rev, mix, None, rev, rev)),
            ('view scroll, bottom edge', edge_bottom, lambda s: s.scroll(1, True), (None, None, None, rev5, rev5)),
            ('view scroll, top edge', edge_top, lambda s: s.scroll(1, False), (rev5, rev5, None, None, None)),
            ('resize with rewrap', resized, lambda s: s.resize(5, 13), (tuple(range(12, -1, -1)), None, None, None, None)),
            ('pause', basic, paused, base),
        )
        with setup_for_testing():
            for name, build, trigger, expect in cases:
                with self.subTest(name):
                    built = []

                    def tracked():
                        built.append(build())
                        return built[-1]

                    self.trigger_case(tracked, trigger, expect, fresh=not name.startswith('view'))
                    for s in built:
                        self.assertTrue(any(s.test_bidi_paragraph_rtl(y) for y in range(s.lines)), 'no RTL row')
            s = basic()
            self.frame(s)
            self.warm(s)
            self.ae(self.frame(s), 0)
            self.ae(self.maps(s), base)
            self.assertTrue(all(s.test_bidi_paragraph_rtl(y) for y in range(4)))

    def test_rtl_paragraph_split_changes_direction(self):
        # Overwriting the last cell of row 0 clears its soft wrap, so row 1
        # becomes its own paragraph with its own first strong character. Row 1
        # is not written, but its direction changes, so it must be reshaped.
        rev5 = (4, 3, 2, 1, 0)
        cases = (
            # abcde + אבג: one LTR paragraph, row 1 is R at level 1 (L1: the
            # blanks 0). After the split row 1 starts with R: RTL, all level 1
            ('abcde', 'אבג', True, rev5),
            # אבגדה + abc: one RTL paragraph, row 1 is L at level 2. After the
            # split row 1 starts with L: LTR, all level 0, so no map
            ('אבגדה', 'abc', False, None),
        )
        with setup_for_testing():
            for first, second, rtl_after, map_after in cases:
                with self.subTest(first):

                    def build():
                        s = self.screen(cols=5, lines=5, bidi_auto_direction=True)
                        s.draw(first)
                        s.draw(second)
                        self.assertTrue(s.linebuf.is_continued(1))
                        return s

                    def split(s):
                        s.cursor_position(1, 5)
                        s.draw('x')
                        self.assertFalse(s.linebuf.is_continued(1))

                    s = build()
                    self.frame(s)
                    self.warm(s)
                    before = s.test_bidi_paragraph_rtl(1)
                    self.assertIs(before, not rtl_after)
                    split(s)
                    count = s.test_bidi_resolve_count()
                    s.test_bidi_refresh()
                    self.assertGreater(s.test_bidi_resolve_count() - count, 0)
                    self.assertIn(1, s.linebuf.dirty_lines())
                    buf = test_screen_update_cell_data(s)
                    self.ae(s.test_bidi_v2l(1), map_after)
                    self.assertIs(bool(s.test_bidi_paragraph_rtl(1)), rtl_after)
                    f = build()
                    split(f)
                    fbuf = test_screen_update_cell_data(f)
                    self.ae(self.maps(s), self.maps(f))
                    self.ae(self.levels(s), self.levels(f))
                    self.assert_same_upload(s, buf, f, fbuf, (1,))
                    self.warm(s)
                    self.ae(self.frame(s), 0)

    def test_rtl_paragraph_reorder_option_toggle(self):
        # Trigger: bidi_reorder off then on, force_ltr on then off, with auto
        # direction on. Either change drops the maps, undoing it must resolve
        # again and give the right-aligned RTL maps.
        on = {'bidi_reorder': True, 'force_ltr': False, 'bidi_auto_direction': True}
        offs = ({'bidi_reorder': False, 'bidi_auto_direction': True}, {'bidi_reorder': True, 'force_ltr': True, 'bidi_auto_direction': True})
        want = tuple(range(9, -1, -1))
        with setup_for_testing():
            try:
                for off in offs:
                    with self.subTest(off=off):
                        s = self.screen(bidi_auto_direction=True)
                        s.draw(A)
                        self.frame(s)
                        self.warm(s)
                        self.ae(s.test_bidi_v2l(0), want)
                        self.set_options(off)
                        self.frame(s)
                        self.assertIsNone(s.test_bidi_v2l(0), off)
                        self.set_options(on)
                        self.assertGreater(self.frame(s), 0, off)
                        self.ae(s.test_bidi_v2l(0), want, off)
                        self.assertIs(s.test_bidi_paragraph_rtl(0), True)
                        self.ae(self.frame(s), 0)
            finally:
                self.set_options(on)

    def test_rtl_paragraph_selection_and_mouse(self):
        # אבג, auto: v2l 9..0 (test_rtl_paragraph_right_aligned)
        s = self.screen(bidi_auto_direction=True)
        s.draw(A)
        self.ae(self.selected_columns(s, 0, 2), [7, 8, 9])
        self.assertIs(s.test_bidi_paragraph_rtl(0), True)
        # a click at visual 0, left of the text, is on logical 9, a trailing
        # blank at the paragraph level, 1: the half is flipped
        self.ae(s.test_bidi_logical_x(0, 0), 9)
        self.assertIs(s.test_bidi_is_rtl(9, 0), True)
        # a click at visual 9, right of the text, lands on logical 0, the first
        # character (א): the half is flipped, so it is the start of that character
        self.ae(s.test_bidi_logical_x(9, 0), 0)

    def test_bidi_control_parse(self):
        NONE, LTR, RTL, AUTO = range(4)
        s = self.screen(bidi_auto_direction=False)
        c = s.callbacks
        self.ae(s.test_bidi_base(), LTR)
        parse_bytes(s, b'\x1b[8l')
        self.ae(s.test_bidi_base(), NONE)
        parse_bytes(s, b'\x1b[8h')
        self.ae(s.test_bidi_base(), LTR)
        s = self.screen(bidi_auto_direction=True)
        self.ae(s.test_bidi_base(), AUTO)
        parse_bytes(s, b'\x1b[8l')
        self.ae(s.test_bidi_base(), NONE)
        parse_bytes(s, b'\x1b[8h')
        self.ae(s.test_bidi_base(), AUTO)
        for seq, want in ((b'\x1b[2 k', RTL), (b'\x1b[1 k', LTR), (b'\x1b[0 k', AUTO), (b'\x1b[2 k', RTL), (b'\x1b[ k', AUTO)):
            with self.subTest(seq=seq):
                parse_bytes(s, seq)
                self.ae(s.test_bidi_base(), want)
        parse_bytes(s, b'\x1b[2 k')
        parse_bytes(s, b'\x1b[1;0 k')
        self.ae(s.test_bidi_base(), LTR)
        for seq in (b'\x1b[1;2;3 k', b'\x1b[3 k', b'\x1b[?1 k'):
            with self.subTest(seq=seq):
                parse_bytes(s, seq)
                self.ae(s.test_bidi_base(), LTR)
        parse_bytes(s, b'\x1b[0 k')
        self.ae(s.test_bidi_base(), AUTO)
        parse_bytes(s, b'\x1b[?2501l')
        self.ae(s.test_bidi_base(), LTR)
        parse_bytes(s, b'\x1b[?2501h')
        self.ae(s.test_bidi_base(), AUTO)
        s = self.screen(bidi_auto_direction=False)
        parse_bytes(s, b'\x1b[?2501h')
        self.ae(s.test_bidi_base(), AUTO)
        parse_bytes(s, b'\x1b[?2501l')
        self.ae(s.test_bidi_base(), LTR)
        parse_bytes(s, b'\x1b[2 k')
        self.ae(s.test_bidi_base(), RTL)
        s = self.screen(bidi_auto_direction=False)
        c = s.callbacks
        parse_bytes(s, b'\x1b[4;8l')
        self.ae(s.test_bidi_base(), NONE)
        parse_bytes(s, b'\x1b[4$p')
        self.ae(c.wtcbuf, b'\x1b[4;2$y')
        c.clear()
        parse_bytes(s, b'\x1b[4;8h')
        self.ae(s.test_bidi_base(), LTR)
        parse_bytes(s, b'\x1b[4$p')
        self.ae(c.wtcbuf, b'\x1b[4;1$y')
        c.clear()
        for seq, want in (
            (b'\x1b[8$p', b'\x1b[8;1$y'),
            (b'\x1b[8l\x1b[8$p', b'\x1b[8;2$y'),
            (b'\x1b[8h\x1b[8$p', b'\x1b[8;1$y'),
            (b'\x1b[?2501$p', b'\x1b[?2501;2$y'),
            (b'\x1b[?2501h\x1b[?2501$p', b'\x1b[?2501;1$y'),
            (b'\x1b[?2501l\x1b[?2501$p', b'\x1b[?2501;2$y'),
        ):
            with self.subTest(seq=seq):
                parse_bytes(s, seq)
                self.ae(c.wtcbuf, want)
                c.clear()
        s = self.screen(bidi_auto_direction=True)
        c = s.callbacks
        parse_bytes(s, b'\x1b[?2501$p')
        self.ae(c.wtcbuf, b'\x1b[?2501;1$y')

    def test_bidi_control_lifetime(self):
        NONE, LTR, RTL, AUTO = range(4)
        s = self.screen(bidi_auto_direction=True)
        parse_bytes(s, b'\x1b[2 k')
        parse_bytes(s, b'\x1b7\x1b[8l\x1b8')
        self.ae(s.test_bidi_base(), NONE)
        s.draw(A)
        s.resize(s.lines + 1, s.columns + 3)
        s.resize(s.lines, s.columns - 2)
        self.ae(s.test_bidi_base(), NONE)
        parse_bytes(s, b'\x1b[8h')
        self.ae(s.test_bidi_base(), RTL)
        s.draw(A)
        parse_bytes(s, b'\x1b[2J\x1b[H')
        self.ae(s.test_bidi_base(), RTL)
        for seq in (b'\x1bc', b'\x1b[!p'):
            with self.subTest(seq=seq):
                s = self.screen(bidi_auto_direction=True)
                parse_bytes(s, b'\x1b[8l\x1b[2 k\x1b[?2501l')
                self.ae(s.test_bidi_base(), NONE)
                parse_bytes(s, b'\x1b[?1049h')
                parse_bytes(s, b'\x1b[8h\x1b[1 k')
                self.ae(s.test_bidi_base(), LTR)
                parse_bytes(s, seq)
                self.ae(s.test_bidi_base(), AUTO)
                parse_bytes(s, b'\x1b[?1049l')
                self.ae(s.test_bidi_base(), AUTO)
        for mode in (b'1049', b'47', b'1047'):
            with self.subTest(mode=mode):
                s = self.screen(bidi_auto_direction=False)
                parse_bytes(s, b'\x1b[2 k')
                parse_bytes(s, b'\x1b[?' + mode + b'h')
                self.ae(s.test_bidi_base(), RTL)
                parse_bytes(s, b'\x1b[8l')
                self.ae(s.test_bidi_base(), NONE)
                parse_bytes(s, b'\x1b[?' + mode + b'l')
                self.ae(s.test_bidi_base(), RTL)
                parse_bytes(s, b'\x1b[8l\x1b[?' + mode + b'h')
                self.ae(s.test_bidi_base(), NONE)
                parse_bytes(s, b'\x1b[8h\x1b[1 k')
                self.ae(s.test_bidi_base(), LTR)
                parse_bytes(s, b'\x1b[?' + mode + b'l')
                self.ae(s.test_bidi_base(), NONE)

    # 'abc אבג' at paragraph level 0: the Hebrew run reversed in place. At
    # level 1: abc is 2 (I2), the space and the blanks 1 (N2, L1); L2 reverses
    # abc, then the row, so the row ends at the right edge, trailing blanks
    # first. 'abc' alone at level 1 maps the same way.
    LTR_MIXED = (0, 1, 2, 3, 6, 5, 4, 7, 8, 9)
    RTL_MIXED = (9, 8, 7, 6, 5, 4, 3, 0, 1, 2)

    def test_bdsm_explicit_disables_reordering(self):
        # Row 0, LTR paragraph: אב(ג) at level 1, the brackets R by N0 (R inside,
        # R before), so shaped mirrored. Row 1, RTL paragraph (auto, R first):
        # (1) is R by N0 (EN counts as R), 1 is 2, every other cell 1, so the row
        # is reversed. CSI 8 l stops reordering on the next frame with no config
        # reload, so the rows shaped with levels are reshaped on that frame;
        # CSI 8 h gives the old maps back.
        rows = ['ab אב(ג)', A + ' (1)']
        with setup_for_testing():
            s = self.screen(bidi_auto_direction=True)
            self.fill(s, rows)
            test_screen_update_cell_data(s)
            before = self.maps(s)
            self.ae(before, ((0, 1, 2, 7, 6, 5, 4, 3, 8, 9), ID10[::-1], None, None, None))
            self.assertTrue(all(s.test_bidi_line_rendered_with_bidi(y) for y in range(2)))
            parse_bytes(s, b'\x1b[8l')
            buf = test_screen_update_cell_data(s)
            self.ae(self.maps(s), (None,) * s.lines)
            self.assertFalse(any(s.test_bidi_line_rendered_with_bidi(y) for y in range(2)))
            parse_bytes(s, b'\x1b[8h')
            test_screen_update_cell_data(s)
            self.ae(self.maps(s), before)
            # made last, as making a screen sets the options
            f = self.screen(bidi_reorder=False, bidi_auto_direction=True)
            self.fill(f, rows)
            self.assert_same_upload(s, buf, f, test_screen_update_cell_data(f), range(2))

    def test_scp_overrides_auto(self):
        # SCP 1 and 2 force the paragraph direction whatever the autodetection
        # says; SCP 0 or no parameter gives the option's behaviour back.
        # A + ' abc' at level 0: the Hebrew run reversed in place, the space
        # between R and L at the embedding level (N2).
        s = self.screen(bidi_auto_direction=True)
        parse_bytes(s, b'\x1b[1 k')
        self.fill(s, [A + ' abc'])
        self.ae(self.refresh(s, 0), (2, 1, 0) + ID10[3:])
        self.assertIs(s.test_bidi_paragraph_rtl(0), False)
        # A Latin-first row with Hebrew and a Latin-only row both right-aligned,
        # and an all-blank row all level 1: the full reversal
        s = self.screen(bidi_auto_direction=False)
        parse_bytes(s, b'\x1b[2 k')
        self.fill(s, ['abc ' + A, 'abc'])
        s.test_bidi_refresh()
        self.ae(self.maps(s), (self.RTL_MIXED, self.RTL_MIXED) + (ID10[::-1],) * 3)
        self.ae(s.test_bidi_levels(1), (2, 2, 2) + (1,) * 7)
        self.assertTrue(all(s.test_bidi_paragraph_rtl(y) for y in range(2)))
        rows = [A + ' abc', 'abc ' + A, 'abc']
        for auto in (True, False):
            f = self.screen(bidi_auto_direction=auto)
            self.fill(f, rows)
            f.test_bidi_refresh()
            want = self.maps(f)
            for seq in (b'\x1b[0 k', b'\x1b[ k'):
                with self.subTest(auto=auto, seq=seq):
                    s = self.screen(bidi_auto_direction=auto)
                    self.fill(s, rows)
                    parse_bytes(s, b'\x1b[2 k')
                    s.test_bidi_refresh()
                    self.assertNotEqual(self.maps(s), want)
                    parse_bytes(s, seq)
                    s.test_bidi_refresh()
                    self.ae(self.maps(s), want)

    def test_owner_hebrew_first_stays_ltr(self):
        # A line whose first strong character is Hebrew stays LTR-based when the
        # program asks for it: שלום reversed in place, the line left-aligned.
        # Explicit mode shows it in logical order. The default options case
        # already passed before the direction control: coverage only.
        ltr = (3, 2, 1, 0) + ID10[4:]
        for auto, seq, want in (
            (True, b'\x1b[1 k', ltr),
            (True, b'\x1b[?2501l', ltr),
            (True, b'\x1b[8l', None),
            (False, b'', ltr),
        ):
            with self.subTest(auto=auto, seq=seq):
                s = self.screen(bidi_auto_direction=auto)
                parse_bytes(s, seq)
                s.draw('שלום abc')
                self.ae(self.refresh(s, 0), want)
                self.ae([s.test_bidi_visual_x(x, 0) for x in range(s.columns)], list(want or ID10))
                if want:
                    self.assertIs(s.test_bidi_paragraph_rtl(0), False)

    def test_control_change_reresolves(self):
        # The effective base is in the frame key: a change of base makes the next
        # frame resolve the cached paragraphs again (explicit mode resolves
        # nothing and drops the maps), and the same-class shortcut never keeps
        # maps across it.
        NONE, LTR, RTL, AUTO = range(4)
        rows = ['abc ' + A, A + ' abc']
        with setup_for_testing():
            s = self.screen(bidi_auto_direction=False)
            self.fill(s, rows)
            self.frame(s)
            self.warm(s)
            # no change: the cache holds
            self.ae(self.frame(s), 0)
            sent = b''
            for seq, base in ((b'\x1b[8l', NONE), (b'\x1b[8h', LTR), (b'\x1b[2 k', RTL), (b'\x1b[0 k', LTR), (b'\x1b[?2501h', AUTO)):
                with self.subTest(seq=seq):
                    parse_bytes(s, seq)
                    sent += seq
                    self.ae(s.test_bidi_base(), base)
                    count = self.frame(s)
                    if base == NONE:
                        self.ae(self.maps(s), (None,) * s.lines)
                    else:
                        self.assertGreater(count, 0)
                    got = self.maps(s)
                    f = self.screen(bidi_auto_direction=False)
                    parse_bytes(f, sent)
                    self.fill(f, rows)
                    self.frame(f)
                    self.ae(got, self.maps(f))
                    self.warm(s)
                    self.ae(self.frame(s), 0)
            # x for a, L for L, in the frame of the base change
            s = self.screen(bidi_auto_direction=False)
            self.fill(s, rows)
            self.frame(s)
            self.warm(s)
            s.cursor_position(1, 1)
            s.draw('x')
            parse_bytes(s, b'\x1b[2 k')
            count = self.frame(s)
            self.ae(self.maps(s)[:2], (self.RTL_MIXED, (9, 8, 7, 4, 5, 6, 3, 2, 1, 0)))
            self.assertGreater(count, 0)
            got = self.maps(s)
            f = self.screen(bidi_auto_direction=False)
            parse_bytes(f, b'\x1b[2 k')
            self.fill(f, ['x' + rows[0][1:], rows[1]])
            self.frame(f)
            self.ae(got, self.maps(f))
            # Entering and leaving the alternate screen (47 keeps its content)
            # resolve again: coverage, as the linebuf was in the key already
            s = self.screen(bidi_auto_direction=False)
            self.fill(s, rows)
            parse_bytes(s, b'\x1b[?47h')
            self.fill(s, rows[::-1])
            self.frame(s)
            self.warm(s)
            parse_bytes(s, b'\x1b[?47l')
            self.assertGreater(self.frame(s), 0)
            self.warm(s)
            parse_bytes(s, b'\x1b[?47h')
            self.assertGreater(self.frame(s), 0)

    def test_paused_frame_uses_paused_base(self):
        # A paused frame renders its snapshot with the base captured when the
        # pause began; a base sent during the pause applies when it ends.
        with setup_for_testing():
            s = self.screen(bidi_auto_direction=False)
            s.draw('abc ' + A)
            self.frame(s)
            self.warm(s)
            parse_bytes(s, b'\x1b[?2026h')
            parse_bytes(s, b'\x1b[2 k')
            self.frame(s)
            self.ae(s.test_bidi_v2l(0), self.LTR_MIXED)
            parse_bytes(s, b'\x1b[?2026l')
            self.frame(s)
            self.ae(s.test_bidi_v2l(0), self.RTL_MIXED)
            # sent before the pause with no live frame between: the snapshot
            # goes with the new base, not with the last live frame's
            s = self.screen(bidi_auto_direction=False)
            s.draw('abc ' + A)
            self.frame(s)
            self.warm(s)
            parse_bytes(s, b'\x1b[2 k\x1b[?2026h')
            self.frame(s)
            self.ae(s.test_bidi_v2l(0), self.RTL_MIXED)
            parse_bytes(s, b'\x1b[?2026l')
            self.frame(s)
            self.ae(s.test_bidi_v2l(0), self.RTL_MIXED)

    def test_tmux_like_opt_out(self):
        # A multiplexer's two panes on the alternate screen, the border at
        # logical column 10. The opt-out is sent on the main screen, which holds
        # row A too, and carried into the alternate screen on entry.
        NONE, LTR, RTL, AUTO = range(4)
        row_a = 'אבג דהו   │ abc def   '  # Hebrew in the left pane only
        row_b = 'אבג       │ שלום      '  # Hebrew on both sides
        border = 10

        def run(seq):
            s = self.screen(cols=22, bidi_auto_direction=True)
            s.draw(row_a)
            parse_bytes(s, seq + b'\x1b[?1049h')
            self.fill(s, [row_a, row_b])
            for y in range(2):
                self.refresh(s, y)
            return s, [s.test_bidi_visual_x(border, y) for y in range(2)]

        def leave(s):
            parse_bytes(s, b'\x1b[?1049l')
            self.refresh(s, 0)
            return s.test_bidi_base(), s.test_bidi_visual_x(border, 0)

        # No opt-out: both rows R first, so RTL paragraphs at level 1, every
        # cell but abc def at level 1, the row reversed: the border moves
        s, cols = run(b'')
        self.ae(cols, [11, 11])
        self.assertTrue(all(s.test_bidi_paragraph_rtl(y) for y in range(2)))
        self.ae(leave(s), (AUTO, 11))
        # Explicit mode: nothing is reordered, the borders stay put
        s, cols = run(b'\x1b[8l')
        self.ae([s.test_bidi_v2l(y) for y in range(2)], [None, None])
        self.ae(cols, [border, border])
        self.ae(leave(s), (NONE, border))
        self.assertIsNone(s.test_bidi_v2l(0))
        # LTR: row A's border is between R and L, at the embedding level (N2),
        # 0, so it stays. Row B's is between R and R, so R (N1) in any paragraph
        # direction: columns 0-15 form one level 1 run, reversed in place, and
        # the border moves to 15 - 10. This is the documented limit of SCP 1.
        s, cols = run(b'\x1b[1 k')
        self.ae(cols, [border, 5])
        self.assertFalse(any(s.test_bidi_paragraph_rtl(y) for y in range(2)))
        self.ae(leave(s), (LTR, border))
