#!/usr/bin/env python
# License: GPLv3

import os
import random
import struct

from kitty.fast_data_types import BIDI_MAX_ISOLATES, CURSOR_BEAM, CURSOR_BLOCK, CURSOR_HOLLOW, CURSOR_UNDERLINE, get_options, test_screen_update_cell_data
from kitty.fonts.render import setup_for_testing

from .base import BaseTest, draw_multicell, parse_bytes

A = 'אבג'  # three Hebrew letters, bidi class R
MIXED = 'abc' + A
NSI = 'אב 12'  # R R WS EN EN: its map (3, 4, 2, 1, 0) is not its own inverse
LRM, RLM = '‎', '‏'
ISOLATES = '⁦⁧⁨⁩'  # LRI RLI FSI PDI
LRE, RLE, PDF, LRO, RLO = '‪‫‬‭‮'
AUTO, SCP_RTL, SCP_LTR = '\x1b[?2501h', '\x1b[2 k', '\x1b[1 k'
PROMPT = '\x1b]133;A\x07'  # OSC 133 prompt start, as shell integration sends it
GPU_CELL = struct.Struct('=II')  # CellGPUData in kitty/line.h: sprite_idx, style_and_mark
CELL_GPU_STYLE_SHIFT = 2
EXTRA_CURSOR = 1 << 2  # an extra cursor of shape 1 in the selection buffer


def pb(s, data):
    parse_bytes(s, data.encode())


def frame(s):
    return test_screen_update_cell_data(s)


def order(s, y=0):
    # visual to logical map of screen row y in the last frame, None for a row shaped without levels
    return s.test_bidi_v2l(y + int(bool(get_options().pixel_scroll) and not s.is_using_alternate_linebuf()))


def visual(s, y=0):
    # screen row y as a new frame shows it: each char once, an empty cell as a space
    frame(s)
    cells = (s.cpu_cells(y, x) for x in order(s, y) or range(s.columns))
    return ''.join(c['text'] or ' ' for c in cells if not (c['mcd'] and (c['x'] or c['y']))).rstrip()


def orders(s):
    frame(s)
    return [order(s, y) for y in range(s.lines)]


def uploaded(s, y=0):
    # (sprite_idx, style_and_mark) of each cell of screen row y in a new frame
    buf = frame(s)
    row_size = s.columns * GPU_CELL.size
    start = (y + len(buf) // row_size - s.lines) * row_size  # pixel scroll adds a leading render row
    return list(GPU_CELL.iter_unpack(buf[start : start + row_size]))


def sprites(s, y=0):
    return [c[0] for c in uploaded(s, y)]


def styles(s, y=0):
    return [c[1] >> CELL_GPU_STYLE_SHIFT for c in uploaded(s, y)]


def draw_coloured(s, text, first_colour=1):
    # each char gets its own 256-colour fg, so its own style index
    for i, ch in enumerate(text):
        pb(s, f'\x1b[38;5;{first_colour + i}m{ch}')
    pb(s, '\x1b[m')


def osc66_controls(text, control_as=None):
    # each bidi control of text in an OSC 66 cell of its own, the only way a cell can start with one
    return ''.join(f'\x1b]66;w=1;{control_as or ch}\x07' if ch in ISOLATES + LRE + RLE + PDF + LRO + RLO else ch for ch in text)


def apply(s, op):
    if callable(op):
        op(s)
    else:
        pb(s, op)


class TestBidiReorder(BaseTest):
    def setUp(self):
        self.enterContext(setup_for_testing())

    def screen(self, *seqs, cols=10, lines=5, scrollback=5, text='', **opts):
        s = self.create_screen(cols=cols, lines=lines, scrollback=scrollback, options=opts)
        pb(s, '\x1b[8h' + ''.join(seqs))
        s.draw(text)
        return s

    def written(self, data, *seqs, cols=10, lines=1, **opts):
        s = self.screen(*seqs, cols=cols, lines=lines, **opts)
        pb(s, data)
        return s

    def paragraph(self, *rows, **kw):
        # rows of a 5 column screen, soft-wrapped into one paragraph
        return self.screen(cols=5, text=''.join(rows), **kw)

    def split(self, s, row=1, ch='ו'):
        # overwrite the last cell of the row above linebuf row `row`, which clears its soft wrap
        s.cursor_position(row, 5)
        s.draw(ch)
        self.assertFalse(s.linebuf.is_continued(row))

    def fresh_sprites(self, text, cols=5, **opts):
        ans = sprites(self.screen(cols=cols, lines=2, text=text, **opts))
        self.assertTrue(any(ans))
        return ans

    def guessed_sprites(self, text, cols=5):
        # text shaped without levels, by a screen that never sent CSI 8 h
        s = self.create_screen(cols=cols, lines=2, options={})
        s.draw(text)
        return sprites(s)

    def assert_frames_match_replay(self, build, *ops):
        # a screen uploaded after each op shows what a new one given the same ops and uploaded once does
        s, f = build(), build()
        frame(s)
        for op in ops:
            apply(s, op)
            frame(s)
        for op in ops:
            apply(f, op)
        self.ae(frame(s), frame(f))

    def test_visual_order(self):
        for text, cols, want in (
            (MIXED, 10, 'abcגבא'),
            ('אב, גד', 10, 'דג ,בא'),
            ('א好ב', 10, 'א好ב'),
            ('אב好גד', 10, 'בא好דג'),
            ('א😀ב', 10, 'ב😀א'),
            (NSI, 5, '12 בא'),
        ):
            with self.subTest(text=text):
                self.ae(visual(self.screen(cols=cols, text=text)), want)
        # no assigned R/AL letter lies outside the classifier; U+FEFD and U+FEFE are unassigned, AL by default
        for cp in (0x0710, 0x0784, 0x07CA, 0x0800, 0x0840, 0x08A0, 0x0870, 0xFB2A, 0xFB50, 0xFE8D, 0x10A10, 0x1E900, 0x1EE00, 0xFEFD, 0xFEFE):
            with self.subTest(hex(cp)):
                self.ae(orders(self.screen(text='ab ' + chr(cp) * 2))[0][3:5], (4, 3))

    def test_rows_without_a_map(self):
        self.assertIsNone(orders(self.screen(text='hello world'))[0])
        s = self.create_screen(cols=10, options={})  # never sent CSI 8 h
        s.draw(MIXED)
        self.assertIsNone(orders(s)[0])
        self.assertIsNone(orders(self.screen(text=MIXED, force_ltr=True))[0])
        # a row with an image placeholder or scaled text is not reordered
        for extra in (lambda s: s.draw(chr(0x10EEEE)), lambda s: draw_multicell(s, 'x', scale=2)):
            s = self.screen(text=A)
            extra(s)
            self.assertIsNone(orders(s)[0])

    def test_upload_permutes_cells(self):
        # each visual cell is the logical cell order[visual], told apart by style, also after a style table remap, live or paused
        for paused in (False, True):
            with self.subTest(paused=paused):
                s = self.screen(cols=5)
                pb(s, '\x1b[3H')
                draw_coloured(s, 'abcde', first_colour=100)
                s.erase_in_line(2)
                s.cursor_position(1, 1)
                draw_coloured(s, NSI)
                logical = [s.cell_style_idx(0, x) for x in range(5)]
                self.ae(styles(s), [logical[x] for x in (3, 4, 2, 1, 0)])
                if paused:
                    s.pause_rendering(True, 1000)
                    frame(s)
                s.test_gc_style_table()
                remapped = [s.cell_style_idx(0, x) for x in range(5)]
                self.assertNotEqual(remapped, logical)
                self.ae(styles(s), [remapped[x] for x in (3, 4, 2, 1, 0)])
        s = self.screen(force_ltr=True)
        draw_coloured(s, MIXED)
        self.ae(styles(s)[:6], [s.cell_style_idx(0, x) for x in range(6)])

    def test_paragraph_over_rows(self):
        # the continuation row has no RTL char of its own and is laid out with its paragraph, also from the history
        s = self.paragraph('אבגדה', '12 34')
        self.ae((visual(s, 0), visual(s, 1)), ('הדגבא', '34 12'))
        for lines, text, rows in ((2, 'אבגדהובזחטיכלמנ12 34', ('נמלכי', '34 12')), (1, 'אבגדהובזחט12 34', ('34 12',))):
            with self.subTest(lines=lines):
                s = self.screen(cols=5, lines=lines, scrollback=10, text=text)
                self.ae(s.historybuf.count, 2)
                self.ae(tuple(visual(s, y) for y in range(lines)), rows)
        # written before CSI 8 h, a row is reordered only with a stamped row of its paragraph
        s = self.create_screen(cols=5, lines=5, options={})
        s.draw('אבגדה')
        pb(s, '\x1b[8h\x1b[3Habc')
        self.assertIsNone(orders(s)[0])
        s = self.create_screen(cols=5, lines=5, options={})
        s.draw('אבגדה')
        pb(s, '\x1b[8hוזח')
        self.ae((visual(s, 0), visual(s, 1)), ('הדגבא', 'חזו'))
        # rewrapped wider, the rows merge
        s = self.screen(cols=5, text='abcde' + A)
        self.ae(visual(s, 1), 'גבא')
        s.resize(5, 25)
        self.ae(visual(s, 0), 'abcdeגבא')

    def test_tall_paragraph_keeps_uploaded_maps(self):
        # 400 rows on 300 lines (100 in history): ! between R letters maps (1,0) except at the paragraph end where it is (0,1). The
        # window, the rows on view and a screen height above them, holds the whole paragraph.
        s = self.screen(cols=2, lines=300, scrollback=200, text='א!' * 400)
        self.ae(s.historybuf.count, 100)
        frame(s)
        maps = tuple(s.test_bidi_v2l(r) for r in range(s.lines + 1))
        self.ae(set(maps), {(0, 1), (1, 0)})
        self.ae([r for r, m in enumerate(maps) if m == (0, 1)], [300])

    def test_osc66_cells(self):
        self.ae(visual(self.written('ab\x1b]66;w=1;א\x07\x1b]66;w=1;ב\x07c')), 'abבאc')
        # a right-to-left char keeps its cells together, shown last to first
        s = self.screen()
        draw_multicell(s, 'א', width=2)
        draw_multicell(s, 'ב', width=2)
        self.ae((visual(s), order(s)[:4]), ('בא', (3, 2, 1, 0)))
        # a bidi control in an OSC 66 cell resolves as ! does, among many and nested to FriBidi's deepest level
        for text, cols, lines in (
            ('a⁧bא⁩⁦ב⁩⁨ג⁩', 12, 2),
            ('א' + '⁧x⁩' * 3000, 200, 50),
            ('aא' + RLE + 'bc', 12, 2),
            ('a' + RLO + 'bc' + PDF + 'd' + LRO + 'אב', 12, 2),
            ((RLE + LRE) * 62 + 'אa', 200, 50),
        ):
            with self.subTest(text=text[:12]):
                neutral = orders(self.written(osc66_controls(text, '!'), cols=cols, lines=lines))
                self.assertIsNotNone(neutral[0])
                self.ae(orders(self.written(osc66_controls(text), cols=cols, lines=lines)), neutral)

    def test_combined_isolates(self):
        # each control is written right after a drawn char, so it is stored in that char's cell and takes no column
        for text, cells in (('a⁧bc⁩', ['a⁧', 'b', 'c⁩']), ('a⁦b⁩c', ['a⁦', 'b⁩', 'c']), ('a' + RLE + 'bc' + PDF, ['a' + RLE, 'b', 'c' + PDF])):
            s = self.screen(text=text)
            self.ae(([s.line(0)[i] for i in range(len(cells) + 1)], s.cursor.x), (cells + ['\x00'], len(cells)))
        for text, seqs, cols, rows in (
            ('a⁧12 34⁩', (), 10, ['a⁧34⁩ 12']),  # RLI makes the digits level 2 and the space between them R
            ('a⁧bc!⁩', (), 10, ['a⁧!⁩bc']),  # the ! takes the isolate's direction (N2, eos R)
            ('a⁨א!', (), 10, ['a⁨!א']),  # without the FSI the ! would take the paragraph's L
            ('a⁧!b⁩c', (), 10, ['a⁧b⁩!c']),  # the PDI ends the isolate
            ('א⁦!a⁩ג', (AUTO,), 10, ['      ג!a⁩א⁦']),  # LRI in an RTL paragraph makes the ! L
            ('a⁧b😀⁩c', (), 10, ['a⁧😀⁩bc']),  # a wide cell holds the PDI
            ('a⁧12 中⁩x', (), 10, ['a⁧中⁩ 12x']),
            ('a⁨אבגדהו!', (), 5, ['a⁨דגבא', '!וה']),  # an unmatched FSI isolates to the paragraph end, across the soft wrap
            # marks combined after a char are fed too
            ('C++' + LRM, (SCP_RTL,), 10, ['       C++' + LRM]),  # the + take the direction of C and the LRM (N1)
            ('12' + RLM + ' 34', (), 10, ['12' + RLM + '34']),  # the space between the RLM and 34 is R
            ('12؜ 34', (), 10, ['12؜34']),  # ALM makes 34 an Arabic number (W2)
        ):
            with self.subTest(text=text):
                s = self.screen(*seqs, cols=cols, lines=len(rows), text=text)
                self.ae([visual(s, y) for y in range(len(rows))], rows)
        # combined embeddings and overrides are never fed
        for control in (LRE, RLE, LRO, RLO):
            self.ae(orders(self.written('אa' + control + 'b!12' + PDF)), orders(self.written('אab!12')))
        self.ae(orders(self.written('x' + RLO + 'abc' + PDF + 'd' + LRE + 'אב' + PDF)), orders(self.written('xabcdאב')))
        # P2 and P3 skip the isolate: with no strong char before the FSI the paragraph takes the fallback direction
        self.ae(visual(self.screen(AUTO, text='> שלום')), '    םולש >')
        self.ae(visual(self.screen(AUTO, text='> ⁨שלום')), '> ⁨םולש')

    def test_isolate_flood(self):
        # every cell of a soft-wrapped paragraph holds a letter and 23 isolate controls, far over the cap: those past it are fed as
        # neutrals, so dropping the controls of the last cell changes no other row
        cell = (ISOLATES * 6)[:23]
        n = 9000
        s = self.written('a' + cell, cols=200)
        self.ae(len(s.line(0)[0]), 24)
        flood = orders(self.written('א' + cell + ('a' + cell) * (n - 1), cols=200, lines=50))
        self.assertIsNotNone(flood[0])
        last = n // 200 - 1
        self.ae(orders(self.written('א' + cell + ('a' + cell) * (n - 2) + 'a', cols=200, lines=50))[:last], flood[:last])

    def test_isolate_cap(self):
        # later text never changes the layout of earlier text: RLI 12 34 PDI stays honoured whatever the number of empty isolates
        # after it, up to the cap and past it
        head = 'א a⁧12 34⁩ '
        for groups in (16, 17, BIDI_MAX_ISOLATES, BIDI_MAX_ISOLATES + 1):
            with self.subTest(groups=groups):
                self.assertTrue(visual(self.written(head + 'b⁦⁩' * (groups - 1), cols=60)).startswith('א a⁧34⁩ 12 b'))
        # an initiator past the cap is fed as a neutral, and so is the PDI that matches it
        late = 'b⁦⁩' * BIDI_MAX_ISOLATES + head
        self.ae(orders(self.written(late, cols=60)), orders(self.written(''.join(ch for ch in late if ch not in ISOLATES), cols=60)))

    def test_bracket_pairs(self):
        # The N0 scan adds, per opening bracket, its distance back to the last letter or digit: 1 for the ( after א, then
        # 2k + 2 for pair k = 0, 1, ... after ב. Against 16 per char of the 80 cell row: 35 pairs give 1261 <= 1280, 36 pairs 1333.
        def both(text, cols=80, lines=1):
            return self.written(text, cols=cols, lines=lines), self.written(text.replace('(', '!').replace(')', '!'), cols=cols, lines=lines)

        kept, plain = both('א(ב)' + '()' * 35)
        self.ae((visual(kept)[:4], visual(plain)[:4]), (')ב(א', 'ב!א!'))
        for text, cols, lines in (('א(ב)' + '()' * 36, 80, 1), ('א(ב)' + '()' * 3000, 200, 50)):
            skipped, plain = both(text, cols, lines)
            self.ae(orders(skipped), orders(plain))

    def test_incremental_frames_match_a_fresh_upload(self):
        # an edit, split, scroll or history change re-shapes every row whose levels it changes, written or not, on view or not
        def history(*data, **kw):
            def build():
                s = self.screen(cols=5, lines=3, scrollback=10, **kw)
                for d in data:
                    apply(s, d)
                return s

            return build

        def ring():
            s = self.screen(cols=5, lines=4, scrollback=0, pixel_scroll=False)
            pb(s, '\r\n'.join(['אב 12', 'א2 3(', 'ab אב', 'אבגד.', 'א 12b', '(12ב', 'אב cd', 'a אבג']))
            for up in (True, False):
                s.scroll(s.historybuf.count, up)
                frame(s)
            return s

        def up(n=1):
            return lambda s: s.scroll(n, True)

        def down(s):
            s.scroll(1, False)

        def pause(s):
            s.pause_rendering(True, 1000)

        def unpause(s):
            s.pause_rendering(False)

        for name, build, ops in (
            ('level change', lambda: self.paragraph('אבגד.', '12 34'), ('\x1b[Habcd',)),
            ('split', lambda: self.paragraph('אבגדה', '(12)'), ('\x1b[1;5Hx',)),
            ('split by ED 1', lambda: self.paragraph('אבגדה', '(12)'), ('\x1b[1;5H\x1b[1J',)),
            ('split by IL', lambda: self.paragraph('אבגדה', '(12)'), ('\x1b[2H\x1b[L',)),
            ('first strong char', lambda: self.screen(AUTO, cols=5, text='אבגדהab 1'), ('\x1b[Hx', '\x1b[Hא')),
            ('split, LTR', lambda: self.screen(AUTO, cols=5, text='abcde' + A), ('\x1b[1;5Hx',)),
            ('split, RTL', lambda: self.screen(AUTO, cols=5, text='אבגדהabc'), ('\x1b[1;5Hx',)),
            ('history sibling', history('top\r\nאבגד(12 34\r\nab\r\ncd'), (up(), '\x1b[Ha')),
            ('below the view', history('h1\r\nh2\r\nh3\r\nאבגד (12)'), (up(), '\x1b[2Habcd', down)),
            ('above the view', history('אבגד(12 34\r\nab\r\ncd', pixel_scroll=False), (up(), down, '\x1b[Ha', up())),
            ('dirty below the view', history('אבגדהאבגדה12 3!abcde'), (up(), '\x1b[3Hב')),
            ('split off view', history('x\r\ny\r\nאבגדה(12ב'), (up(), '\x1b[2;5Hו', down)),
            ('split off view, paused', history('x\r\ny\r\nאבגדה(12ב'), (up(), '\x1b[2;5Hו', down, pause)),
            ('paused, continues from history', history('אבגדה(12ב\r\nx\r\ny', pixel_scroll=False), (pause, unpause)),
            (
                'erased prompt in history',
                history(AUTO + '12 34א' + PROMPT + '$ c\r\no1\r\no2\r\no3\r\n' + PROMPT + '$ ', pixel_scroll=False),
                (up(5), lambda s: s.erase_last_command(), up(5)),
            ),
            ('ring', ring, (up(2), '\x1b[4H\n')),
            ('ring, scrolled past it', ring, (up(10), '\x1b[4H\n')),
            ('cleared under a new base', lambda: self.screen(AUTO, text='abc ' + A), ('\x1b[2J\x1b[H' + SCP_RTL + A + ' abc',)),
        ):
            with self.subTest(name):
                self.assert_frames_match_replay(build, *ops)
        # a history line number reused after its line was popped back onto the screen
        for paused in (False, True):
            with self.subTest('history number reused', paused=paused):
                o = history('א2 3ב\r\nh\r\ni\r\nj', pixel_scroll=False)
                ops = [up(), lambda s: s.reverse_scroll(1, True), '\x1b[Hא2 3(\x1b[3H\n', up()]
                if paused:
                    ops[1:1] = [pause]
                    ops[-1:-1] = [unpause]
                self.assert_frames_match_replay(o, *ops)

    def test_split_below_the_view_reshaped_when_scrolled_into_view(self):
        # scrolled back, the last row on view loses its soft wrap: the row below it, off view, is then a paragraph of its own,
        # left-to-right, and is shaped so once shown
        def build():
            s = self.screen(AUTO, cols=5, lines=3, scrollback=10, pixel_scroll=False)
            pb(s, 'x0\r\nx1\r\nאבגדהab אב')
            self.ae((str(s.line(1)), str(s.line(2)), s.linebuf.is_continued(2)), ('אבגדה', 'ab אב', True))
            return s

        self.assert_frames_match_replay(build, lambda s: s.scroll(1, True), '\x1b[2;5Hו', lambda s: s.scroll(1, False))

    def test_paused_frames_show_the_snapshot(self):
        # the split changes row 1 from ב12( to (12ב: paused frames show the snapshot, so a split made during the pause shows once unpaused
        fresh = self.fresh_sprites('(12ב')
        for split_before_pause in (False, True):
            with self.subTest(split_before_pause=split_before_pause):
                s = self.paragraph('אבגדה', '(12ב')
                self.ae(visual(s, 1), 'ב12(')
                self.assertNotEqual(sprites(s, 1), fresh)
                if split_before_pause:
                    self.split(s)
                s.pause_rendering(True, 1000)
                if not split_before_pause:
                    self.split(s)
                self.ae(visual(s, 1), '(12ב' if split_before_pause else 'ב12(')
                self.ae(s.linebuf.dirty_lines(), [0])  # paused frames shape the snapshot's rows, not the live ones
                s.pause_rendering(False)
                self.ae((visual(s, 1), sprites(s, 1)), ('(12ב', fresh))
        # no live frame runs, so only the snapshot's own stamp can give a paused frame maps
        for stamped in (False, True):
            s = self.create_screen(cols=10, lines=5, scrollback=5, options={})
            if stamped:
                pb(s, '\x1b[8h')
            draw_coloured(s, MIXED)
            logical = [s.cell_style_idx(0, x) for x in range(6)]
            s.pause_rendering(True, 1000)
            self.ae(styles(s)[:6], [logical[x] for x in ((0, 1, 2, 5, 4, 3) if stamped else range(6))])

    def selected(self, s, start, end):
        # the visual columns lit on row 0 by a new frame when logical cells start..end are selected; no other row may be lit
        s.clear_selection()
        s.start_selection(start, 0)
        s.update_selection(end, 0)
        return self.lit(s)

    def lit(self, s):
        frame(s)
        raw = s.current_selections()
        offset = len(raw) // s.columns - s.lines  # the extra leading render row of pixel scroll
        rows = [list(raw[s.columns * (y + offset) : s.columns * (y + offset + 1)]) for y in range(s.lines)]
        self.ae(rows[1:], [[0] * s.columns] * (s.lines - 1))
        return [x for x, b in enumerate(rows[0]) if b]

    def test_selection(self):
        # NSI's map is not its own inverse: logical 0 lights visual 4 and logical 3 visual 0
        s = self.screen(cols=5, text=NSI)
        self.ae([self.selected(self.screen(text=MIXED), 3, 4), self.selected(s, 0, 0), self.selected(s, 3, 3)], [[4, 5], [4], [0]])
        self.ae(self.selected(self.screen(AUTO, text=A), 0, 2), [7, 8, 9])
        # extra cursors on logical cells 3 and 0 of row 0 and 3 of row 1
        s = self.screen(text=MIXED)
        pb(s, '\x1b[>1;2:1:4;2:1:1;2:2:4; q')
        frame(s)
        raw, c = s.current_selections(), EXTRA_CURSOR
        offset = len(raw) // s.columns - s.lines
        self.ae(list(raw[10 * offset : 10 * offset + 20]), [c, 0, 0, 0, 0, c, 0, 0, 0, 0] + [0, 0, 0, c, 0, 0, 0, 0, 0, 0])
        # beam extra cursors on logical cells 3, 5 and 0 are at the visual column of their own cell, RTL ones included
        pb(s, '\x1b[>0;4 q\x1b[>2;2:1:4;2:1:6;2:1:1 q')
        frame(s)
        raw, b = s.current_selections(), 2 * EXTRA_CURSOR
        self.ae(list(raw[10 * offset : 10 * offset + 10]), [b, 0, 0, b, 0, b, 0, 0, 0, 0])

    def drawn(self, s, x, shape=CURSOR_BLOCK):
        s.cursor_position(1, x + 1)
        frame(s)
        return s.test_cursor_x(shape)[0]

    def test_beam_on_odd_level_cells(self):
        # Every cursor shape is drawn at the visual column of the cursor's cell, the cell after the text included. A beam on an
        # odd-level cell is on the left edge of the column past the cell's full width, in the last column on the cell's own edge.
        def shapes(s, x):
            return tuple(self.drawn(s, x, shape) for shape in (CURSOR_BLOCK, CURSOR_UNDERLINE, CURSOR_HOLLOW, CURSOR_BEAM))

        s = self.screen(text=MIXED)
        self.ae([shapes(s, x) for x in (0, 1, 3, 4, 5, 6)], [(0,) * 4, (1,) * 4, (5, 5, 5, 6), (4, 4, 4, 5), (3, 3, 3, 4), (6,) * 4])
        self.ae(shapes(self.screen(cols=6, text=MIXED), 3), (5, 5, 5, 5))
        s = self.screen(text='a')
        draw_multicell(s, 'א', width=2)
        draw_multicell(s, 'ב', width=2)
        self.ae([shapes(s, x) for x in (1, 3)], [(3, 3, 3, 5), (1, 1, 1, 3)])
        # until the next frame, maps sized for an old width are not used
        s = self.screen(cols=20, lines=3, text=''.join(chr(0x5D0 + i) for i in range(20)))
        self.ae(self.drawn(s, 2), 17)
        s.resize(3, 10)
        s.cursor_position(1, 3)
        self.ae(s.test_cursor_x(CURSOR_BLOCK)[0], 2)
        self.ae(self.drawn(s, 2), 7)

    def test_ime_anchor_follows_visual_caret(self):
        s = self.screen(text=MIXED)
        s.cursor_position(1, 4)
        frame(s)
        self.ae(s.test_cursor_x(CURSOR_BLOCK)[1], 5)
        # an edit that reorders the row moves the caret's cell, the anchor is read after the upload
        s.draw('x')
        frame(s)
        self.ae((s.cursor.x, s.test_cursor_x(CURSOR_BLOCK)), (4, (5, 5)))

    def test_maps_follow_the_frame(self):
        # without pixel scroll the alternate screen has as many render rows as the main one: only the source and its stamp tell the maps apart
        s = self.screen(text=MIXED, pixel_scroll=False)
        self.ae((self.selected(s, 3, 4), s.test_cursor_x(CURSOR_BLOCK)[0]), ([4, 5], 6))
        pb(s, '\x1b[?1049h\x1b[H')
        s.draw(MIXED)
        self.ae((self.selected(s, 3, 4), s.test_cursor_x(CURSOR_BLOCK)[0]), ([3, 4], 6))
        # once stamped too, the alternate screen has no maps of its own until its next frame, nor the main one after
        pb(s, '\x1b[8h\x1b[H' + MIXED + '\x1b[1;4H')
        self.ae(s.test_cursor_x(CURSOR_BLOCK)[0], 3)
        pb(s, '\x1b[?1049l\x1b[1;4H')
        self.ae(s.test_cursor_x(CURSOR_BLOCK)[0], 3)
        # force_ltr set at runtime: the next frame is uploaded in logical order, has no maps and redraws the selection
        s = self.screen(text=MIXED)
        self.ae(self.selected(s, 3, 4), [4, 5])
        self.set_options({'force_ltr': True})
        frame(s)
        self.ae((order(s), s.test_cursor_x(CURSOR_BLOCK)[0], s.is_selection_dirty()), (None, 6, True))
        self.ae(self.lit(s), [3, 4])

    def test_runs_shaped_in_resolved_direction(self):
        # ( between R letters resolves to an odd level, so the frame shapes it right to left, mirrored, unlike force_ltr
        text = 'abc א(ב'
        plain = self.fresh_sprites(text, cols=10, force_ltr=True)
        s = self.screen(text=text)
        self.ae(visual(s), 'abc ב(א')
        shaped = sprites(s)
        self.ae(shaped[:4], plain[:4])
        self.assertNotEqual(shaped[5], plain[5])

    def test_bdsm_reset_keeps_records(self):
        # CSI 8 l changes only what rows written afterwards are stamped with: overwriting a stamped row keeps its stamp
        s = self.screen(text=NSI)
        pb(s, '\x1b[2H' + NSI)
        before = uploaded(s, 0), uploaded(s, 1)
        self.ae((visual(s, 0), visual(s, 1)), ('12 בא',) * 2)
        plain, reordered = self.guessed_sprites(NSI, cols=10), self.fresh_sprites(NSI, cols=10)
        self.assertNotEqual(plain, reordered)
        pb(s, '\x1b[8l')
        self.ae((uploaded(s, 0), uploaded(s, 1)), before)
        pb(s, '\x1b[H' + NSI + '\x1b[4H' + NSI)
        self.ae((visual(s, 0), sprites(s, 0)), ('12 בא', reordered))
        self.ae((orders(s)[3], sprites(s, 3)), (None, plain))

    def test_paragraph_direction(self):
        # P2-P3: the first strong char (R, AL or L; not EN or AN) sets the direction under ?2501; the blanks of an RTL paragraph take
        # its level (L1), so its text is right-aligned
        for text, cols, seqs, rows in (
            ('שלום!', 10, (AUTO,), ['     !םולש']),
            ('שלום!', 10, (), ['םולש!']),
            ('אבג abc', 10, (AUTO,), ['   abc גבא']),
            ('abc אבג', 10, (AUTO,), ['abc גבא']),
            ('12 אב ab', 10, (AUTO,), ['  ab בא 12']),
            ('(12 aאבגדה', 5, (AUTO,), ['(12 a', 'הדגבא']),
            ('אבגדהabcde', 5, (AUTO,), ['הדגבא', 'abcde']),
            ('مرحبا!', 10, (AUTO,), ['    !ابحرم']),
            ('١٢٣ 45!', 10, (AUTO,), ['١٢٣ 45!']),
            ('שלום abc', 10, (SCP_LTR,), ['םולש abc']),
            ('שלום abc', 10, (AUTO, '\x1b[?2501l'), ['םולש abc']),
            ('abc def', 10, (SCP_RTL,), ['   abc def']),  # SCP 2 alone: an RTL paragraph of Latin text
            ('abc def', 10, (AUTO, SCP_RTL), ['abc def']),  # with ?2501 the first strong char decides
            (A + ' abc', 10, (AUTO, SCP_RTL), ['   abc גבא']),
            ('12 34', 10, (AUTO, SCP_RTL), ['     34 12']),  # and a line with none takes SCP's
            ('abcd efg', 5, (SCP_RTL,), [' abcd', '  efg']),  # L1 resets the space ending row 0 to the paragraph level
        ):
            with self.subTest(text, seqs=seqs):
                s = self.screen(*seqs, cols=cols, text=text)
                self.ae([visual(s, y) for y in range(len(rows))], rows)
        self.assertIsNone(orders(self.screen(AUTO, text='12 34'))[0])
        self.assertIsNone(orders(self.screen(AUTO, '\x1b[8l', text='שלום abc'))[0])
        # an RTL paragraph's blank cells are drawn empty
        s = self.screen(AUTO)
        draw_coloured(s, A)
        logical = [s.cell_style_idx(0, x) for x in range(3)]
        row = uploaded(s)
        self.ae(([c[0] for c in row[:7]], all(c[0] for c in row[7:])), ([0] * 7, True))
        self.ae([c[1] >> CELL_GPU_STYLE_SHIFT for c in row[7:]], logical[::-1])

    def test_blank_rows_under_an_rtl_base(self):
        # a paragraph of one blank row has nothing for FriBidi: under an RTL base it is reversed, under LTR it has no map
        for seqs in ((SCP_RTL,), (AUTO, SCP_RTL)):
            self.ae(orders(self.screen(*seqs, text=' '))[0], tuple(range(9, -1, -1)))
        self.assertIsNone(orders(self.screen(AUTO, text=' '))[0])
        # also a blank last row of a wrapped paragraph
        s = self.screen(SCP_RTL, text='א' * 11)
        pb(s, '\x1b[2H\x1b[X')
        self.assertTrue(s.linebuf.is_continued(1))
        self.ae(orders(s)[:2], [tuple(range(9, -1, -1))] * 2)
        # the blank tail goes first, reversed, ahead of the reordered rest
        s = self.screen(SCP_RTL, text='אב中')
        self.ae(visual(s), ' ' * 6 + '中בא')
        s = self.screen(SCP_RTL, text='אב')
        draw_multicell(s, 'x', width=2)
        self.ae(visual(s), ' ' * 6 + 'xבא')

    def test_controls_change_no_paragraph_with_text(self):
        # records are stamped: toggling the controls, CSI 8 l included, re-resolves no paragraph whose first row has text
        for first in ((), (AUTO,), (SCP_RTL,)):
            with self.subTest(first=first):
                s = self.screen(*first, text=A + ' abc')
                pb(s, '\r\n' + 'abc ' + A)
                want = orders(s)
                for seq in (AUTO, '\x1b[?2501l', SCP_RTL, SCP_LTR, '\x1b[0 k', '\x1b[8l', '\x1b[8h', AUTO, SCP_RTL):
                    pb(s, seq)
                    self.ae(orders(s), want, seq)
        # an overwrite keeps the old base, and so does an EL that leaves text on the row
        s = self.screen(text='abc ' + A)
        pb(s, AUTO + SCP_RTL + '\x1b[Hב')
        self.ae(visual(s), 'בbc גבא')
        s = self.screen(text=A + ' abc')
        pb(s, AUTO + SCP_RTL + '\x1b[1;6H\x1b[K')
        self.ae(visual(s), 'גבא a')
        # a cleared line takes the new base, as does a line inserted and rewritten
        for clear in ('\x1b[2J\x1b[H', '\x1b[H\x1b[L'):
            s = self.screen(text=A + ' abc')
            pb(s, AUTO + SCP_RTL + clear + A + ' abc')
            self.ae(visual(s), '   abc גבא')

    def preedit_reference(self, text, x, preedit='xy'):
        # the row a screen that never sent CSI 8 h uploads for text with the pre-edit at x
        f = self.create_screen(cols=10, lines=5, scrollback=5, options={})
        draw_coloured(f, text)
        f.cursor_position(1, x + 1)
        self.assertTrue(any(sprites(f)))
        f.test_update_overlay_text(preedit)
        return uploaded(f)

    def test_preedit(self):
        # the pre-edit block starts at the visual column of the cursor cell and extends right, the rest of the row stays in visual order
        ref = self.preedit_reference(MIXED, 1)
        s = self.screen()
        draw_coloured(s, MIXED)
        s.cursor_position(1, 2)
        before = uploaded(s)
        self.ae(self.drawn(s, 1, CURSOR_BEAM), 1)
        s.test_update_overlay_text('xy')
        row = uploaded(s)
        self.ae((s.test_cursor_x(CURSOR_BEAM), order(s)), ((3, 1), (0, 1, 2, 5, 4, 3, 6, 7, 8, 9)))
        self.ae((row[:1] + row[3:], row[1:3]), (before[:1] + before[3:], ref[1:3]))
        # also when the cell is at an odd level
        ref = self.preedit_reference(A + ' ' + A, 7, 'xyzw')  # right-aligned there, at columns 6-9
        s = self.screen(AUTO)
        draw_coloured(s, A + ' ' + A)
        before = uploaded(s)
        s.test_update_overlay_text('xyzw')
        row = uploaded(s)
        self.ae((s.test_cursor_x(CURSOR_BEAM), row[2:6]), ((6, 2), ref[6:10]))
        self.ae((row[:2] + row[6:], order(s)), (before[:2] + before[6:], tuple(range(9, -1, -1))))
        # a Latin row has no map: its pre-edit row is uploaded as without bidi, and the line record is left alone
        ref = self.preedit_reference('abc def', 7)
        s = self.screen(AUTO)
        draw_coloured(s, 'abc def')
        frame(s)
        record = s.test_bidi_line_record(0)
        s.test_update_overlay_text('xy')
        self.ae((s.test_cursor_x(CURSOR_BEAM), uploaded(s), order(s)), ((9, 7), ref, None))
        s.test_update_overlay_text(RLM + 'xy')
        frame(s)
        s.test_update_overlay_text('')
        self.ae((s.test_bidi_line_record(0), orders(s)[0]), (record, None))

    def test_leading_mark_layout(self):
        # a leading mark is the first strong char of its paragraph under ?2501; an RLM alone makes an LTR row reorder, as the digits
        # after it stay EN, which N1 treats as R
        for seqs, text, want in (
            ((AUTO,), RLM + 'abc', '       ' + RLM + 'abc'),
            ((AUTO,), LRM + A, 'גב' + LRM + 'א'),
            ((AUTO,), RLM, '         ' + RLM),
            ((AUTO, 'abc\r'), RLM, '       ' + RLM + 'abc'),
            ((), RLM + '1 2', '2 ' + RLM + '1'),
            ((), LRM + RLM + '1 2', '2 ' + LRM + RLM + '1'),
            ((SCP_RTL,), LRM + '1 2', '       ' + LRM + '1 2'),  # by W7 the digits after the LRM are L
            ((), LRM + 'א 1', '1 ' + LRM + 'א'),
        ):
            with self.subTest(seqs=seqs, text=text):
                self.ae(visual(self.screen(*seqs, text=text)), want)
        for seqs, text in (((AUTO,), 'abc'), ((), '1 2'), ((), LRM + '1 2'), ((), LRM), ((), RLM)):
            self.assertIsNone(orders(self.screen(*seqs, text=text))[0], text)
        self.assertIsNone(orders(self.screen(AUTO, text=RLM + 'abc', force_ltr=True))[0])
        # the marks are fed once, before the char: the ! between the RLM and the a is L, so a beam on it is on its own edge
        self.ae(self.drawn(self.screen(text=LRM + RLM + '!a'), 0, CURSOR_BEAM), 0)
        # without bidi mode, or before it, the mark is dropped
        s = self.create_screen(cols=10, options={})
        pb(s, RLM + '\x1b[8h' + AUTO + 'abc')
        self.ae((s.cpu_cells(0, 0)['text'], orders(s)[0]), ('a', None))
        # ICH in column 0 leaves the marks there, before what it shifts, and a char drawn there keeps them
        self.ae(visual(self.written('\r\x1b[@', text=RLM + '1 2')), '2 1' + RLM)
        self.ae(visual(self.written('\r\x1b[@x', text=RLM + '1 2')), RLM + 'x1 2')
        s, f = self.written('\r\x1b[@\x1b[3G1 2', text=RLM), self.written('\r\x1b[@', text=RLM + ' 1 2')
        self.ae(([s.cpu_cells(0, x)['text'] for x in range(2)], [f.cpu_cells(0, x)['text'] for x in range(2)]), ([RLM, ''], [RLM, ' ']))
        self.ae(orders(s), orders(f))
        self.assertIsNotNone(orders(s)[0])
        # a cell that loses its mark is resolved again
        s = self.screen(AUTO, text=RLM + 'abc')
        pb(s, '\r\x1b[Xa')
        self.ae((s.cpu_cells(0, 0)['text'], orders(s)[0]), ('a', None))
        # the pre-edit is drawn over a marks-only cell, which keeps its mark
        for preedit in ('xy', RLM + 'xy'):
            s = self.screen(text=RLM)
            s.test_update_overlay_text(preedit)
            frame(s)
            s.test_update_overlay_text('')
            s.draw('a')
            self.ae(s.cpu_cells(0, 0)['text'], RLM + 'a')

    # The randomized differential: after every frame, the cell data uploaded for a screen that is rendered after each operation
    # equals that of a fresh screen given the same operations and rendered once. Paragraphs longer than a screen are left out, as
    # one longer than the resolve window is laid out from the part of it in the window, which depends on where the view was.
    DIFF_POOL = 'אבגדהוזחט' * 3 + 'abcdefghij' * 3 + '0123456789' * 2 + '  ' * 3 + '()!.-'
    DIFF_KINDS = dict(
        zip(
            'text flip unwrap crlf lf el ed ech dch ich il dl region ri su sd resize alt back fwd pause scp auto bdsm'.split(),
            (30, 6, 4, 6, 6, 3, 2, 2, 2, 2, 6, 6, 5, 4, 3, 3, 1, 2, 3, 3, 2, 3, 3, 1),
        )
    )

    def diff_op(self, rng, s):
        L, C = s.lines, s.columns
        y, x, n = rng.randrange(L) + 1, rng.randrange(C) + 1, rng.randrange(1, 4)
        kind = rng.choices(tuple(self.DIFF_KINDS), tuple(self.DIFF_KINDS.values()))[0]

        def at(data):
            data = f'\x1b[{y};{x}H'.encode() + data
            return lambda s: parse_bytes(s, data)

        if kind == 'text':
            text = ''.join(rng.choices(self.DIFF_POOL, k=rng.randrange(1, 3 * C // 2)))
            if rng.random() < 0.2:
                # an isolate written after a char is stored in its cell
                i = rng.randrange(len(text))
                text = text[: i + 1] + rng.choice(ISOLATES) + text[i + 1 :]
            return kind, at(text.encode() + (b'\r\n' if rng.random() < 0.5 else b''))
        if kind == 'flip':
            return kind, lambda s, ch=rng.choice('אa'): parse_bytes(s, f'\x1b[{y};1H{ch}'.encode())
        if kind == 'unwrap':
            # a char written over the last cell of a row drops its soft wrap, which splits a paragraph below that row
            return kind, lambda s, ch=rng.choice('אa1 '): parse_bytes(s, f'\x1b[{y};{s.columns}H{ch}'.encode())
        if kind == 'crlf':
            return kind, at(b'\r\n')
        if kind == 'lf':
            return kind, lambda s: parse_bytes(s, f'\x1b[{s.lines};1H\n'.encode())
        if kind == 'el':
            return kind, at(f'\x1b[{rng.randrange(3)}K'.encode())
        if kind == 'ed':
            return kind, at(f'\x1b[{rng.randrange(4)}J'.encode())
        if kind in ('ech', 'dch', 'ich'):
            return kind, at(f'\x1b[{n}{"XP@"[("ech", "dch", "ich").index(kind)]}'.encode())
        if kind in ('il', 'dl'):
            return kind, at(f'\x1b[{n}{"LM"[kind == "dl"]}'.encode())
        if kind in ('region', 'ri'):
            top = rng.randrange(1, L)
            bottom = rng.randrange(top + 1, L + 1)
            move = f'\x1b[{bottom};1H\x1bD' if kind == 'region' else f'\x1b[{top};1H\x1bM'
            data = f'\x1b[{top};{bottom}r{move * n}\x1b[r'.encode()
            return kind, lambda s: parse_bytes(s, data)
        if kind in ('su', 'sd'):
            top = rng.randrange(1, L)
            data = f'\x1b[{top};{L}r\x1b[{n}{"ST"[kind == "sd"]}\x1b[r'.encode()
            return kind, lambda s: parse_bytes(s, data)
        if kind == 'resize':
            lines, cols = rng.randrange(4, 8), rng.randrange(6, 11)
            return kind, lambda s: s.resize(lines, cols)
        if kind == 'alt':
            data = rng.choice((b'\x1b[?1049h', b'\x1b[?1049l'))
            return kind, lambda s: parse_bytes(s, data)
        if kind in ('back', 'fwd'):
            return kind, lambda s: s.scroll(n, kind == 'back')
        if kind == 'pause':
            pause = rng.random() < 0.5
            return kind, lambda s: s.pause_rendering(pause, 1000000)
        if kind == 'scp':
            data = rng.choice((SCP_RTL, SCP_LTR, '\x1b[0 k')).encode()
            return kind, lambda s: parse_bytes(s, data)
        if kind == 'auto':
            data = rng.choice((AUTO, '\x1b[?2501l')).encode()
            return kind, lambda s: parse_bytes(s, data)
        data = b'\x1b[8l' if rng.random() < 0.3 else b'\x1b[8h'
        return kind, lambda s: parse_bytes(s, data)

    def longest_paragraph(self, s):
        # in rows, over the history and the screen
        hb, longest, run = s.historybuf, 0, 0
        cont = [hb.is_continued(i) for i in range(hb.count)]
        cont += [y == 0 and s.is_main_linebuf() and hb.count > 0 and hb.endswith_wrap() or y > 0 and s.linebuf.is_continued(y) for y in range(s.lines)]
        for c in cont:
            run = run + 1 if c else 1
            longest = max(longest, run)
        return longest

    # An auto-direction paragraph over four rows, which an edit of its first cell turns from LTR to RTL and back
    FLIP_START = '\x1b[?2501h\x1b[2;1Habc 12 אב de fg 34 hi ג jk lm\r\n'.encode()

    def differential(self, seed, count):
        rng = random.Random(seed)

        def new():
            s = self.screen(cols=8, lines=6, scrollback=2000)
            parse_bytes(s, self.FLIP_START)
            return s

        s, ops, compared = new(), [], 0
        for i in range(count):
            if i in (0, 1):
                name, op = 'flip', lambda s, ch='אa'[i]: parse_bytes(s, f'\x1b[2;1H{ch}'.encode())
            else:
                name, op = self.diff_op(rng, s)
            op(s)
            ops.append(op)
            got = test_screen_update_cell_data(s)
            if self.longest_paragraph(s) > s.lines:
                continue
            f = new()
            for o in ops:
                o(f)
            self.ae(got, test_screen_update_cell_data(f), f'seed {seed}, op {i} ({name})')
            compared += 1
        return compared

    def test_random_differential(self):
        seeds = int(os.environ.get('KITTY_BIDI_FUZZ_SEEDS', 3))
        count = int(os.environ.get('KITTY_BIDI_FUZZ_OPS', 200))
        compared = sum(self.differential(seed, count) for seed in range(seeds))
        self.assertGreater(compared, seeds * count // 2)
