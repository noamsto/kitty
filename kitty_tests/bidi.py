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
        # split comes before the pause, the paused frames already resolve the
        # new levels, though with no level compare in a paused frame row 1 keeps
        # its old glyphs until the next live frame; so a second paused frame must
        # not replace the line state the first one kept.
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
                    if split_before_pause:
                        split(s)
                    s.pause_rendering(True, 1000)
                    for i in range(paused_frames):
                        test_screen_update_cell_data(s)
                    if not split_before_pause:
                        split(s)
                    s.pause_rendering(False)
                    row1 = gpu_row(s, test_screen_update_cell_data(s), 1)
                    self.ae(s.test_bidi_levels(1), (0, 0, 0, 1, 0))
                    f = self.screen(cols=5, lines=5)
                    f.draw('(12ב')
                    fresh = gpu_row(f, test_screen_update_cell_data(f), 0)
                    self.assertTrue(any(c.sprite_idx for c in fresh))
                    self.ae([c.sprite_idx for c in row1], [c.sprite_idx for c in fresh])

    def test_dirty_row_below_view_redraws_paragraph(self):
        # scrolled up, a dirty linebuf row of the paragraph below the view marks
        # the on-screen rows dirty
        with setup_for_testing():
            s = self.screen(cols=5, lines=3, scrollback=10)
            s.draw('אבגדה' * 4)
            self.ae(s.historybuf.count, 1)
            test_screen_update_cell_data(s)
            s.scroll(1, True)
            test_screen_update_cell_data(s)
            self.ae(s.scrolled_by, 1)
            self.ae(s.linebuf.dirty_lines(), [])
            s.cursor_position(3, 1)
            s.draw('a')
            self.ae(s.scrolled_by, 1)
            self.ae(s.linebuf.dirty_lines(), [2])
            s.test_bidi_refresh()
            self.ae(s.linebuf.dirty_lines(), [0, 1, 2])

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
