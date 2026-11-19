#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, kitty contributors

import re
from itertools import product

from kitty.fast_data_types import EXTEND_WORD
from kitty.marks import marker_from_regex

from .base import BaseTest, draw_multicell, parse_bytes
from .graphics import put_helpers

NONE, LTR, RTL = (0, False, False), (1, False, False), (1, True, False)
CUP = '\x1b[{};1H'.format  # 1-based row
LRM, RLM = '\u200e', '\u200f'
LRI, RLI, FSI, PDI = '\u2066', '\u2067', '\u2068', '\u2069'


def pb(s, data):
    parse_bytes(s, data.encode())


class TestBidi(BaseTest):
    def screen(self, cols=10, lines=5, **opts):
        return self.create_screen(cols=cols, lines=lines, options=opts)

    def records(self, s, *rows):
        return [s.test_bidi_line_record(y) for y in rows]

    def written(self, data, *rows, cols=10):
        s = self.screen(cols=cols)
        pb(s, data)
        return self.records(s, *rows)

    def modes(self, s):
        # BDSM and ?2501 as DECRQM reports them: 1 set, 2 reset
        s.callbacks.clear()
        pb(s, '\x1b[8$p\x1b[?2501$p')
        return tuple(int(m) for m in re.findall(rb';(\d)\$y', s.callbacks.wtcbuf))

    def test_stamp(self):
        # a row is stamped by the first char drawn into it in bidi mode, with the controls current then; explicit writes keep it
        s = self.screen()
        pb(s, 'a\x1b[8h' + CUP(2) + 'b\x1b[8l' + CUP(2) + 'c' + CUP(4) + 'd')
        self.ae(self.records(s, 0, 1, 3), [NONE, LTR, NONE])
        self.ae(self.written('\x1b[8ha\r\n\x1b[8lb', 0, 1), [LTR, NONE])  # the LF writes nothing to row 1
        self.ae(self.written('\x1b[8h\x1b[8l' + CUP(4) + 'c', 3), [NONE])
        self.ae(self.written('\x1b[8h \x1b[2 k', 0), [LTR])  # a written space is text
        self.ae(self.written('\x1b[8ha\r\n\x1b[2 kb', 0, 1), [LTR, RTL])
        self.ae(self.written('\x1b[8h\x1b[2 ka\x1b[1 k\x1b[?2501h' + CUP(1) + 'b', 0), [RTL])
        self.ae(self.written('\x1b[8h\x1b[2 k\x1b[3 ka', 0), [RTL])  # SCP 3 is ignored
        self.ae(self.written('\x1b[8h\x1b[2 k\x1b[ ka', 0), [LTR])
        self.ae(self.written('\x1b[8h\x1b[?2501ha', 0), [(1, False, True)])
        # mode changes leave records alone
        self.ae(self.written('\x1b[8h\x1b[?2501h\x1b[?s\x1b[?2501la\r\nb\r\x1b[K\x1b[?r', 0, 1), [LTR, NONE])
        self.ae(self.written('\x1b[8h\x1b[?1049h\x1b[8ha\r\nb\r\x1b[K\x1b[?1049l\x1b[?47h', 0, 1), [LTR, NONE])
        # an overlay line writes no record
        s = self.screen()
        pb(s, '\x1b[8h')
        s.test_draw_overlay_line('xy', 0, 2)
        self.ae(self.records(s, 2), [NONE])
        pb(s, CUP(3) + 'z')
        self.ae(self.records(s, 2), [LTR])
        # under force_ltr nothing is recorded or stored, a leading LRM is dropped, and DECRQM answers 4
        s = self.screen(force_ltr=True)
        pb(s, '\x1b[8h\x1b[?2501h\x1b[2 k\u200eabc')
        self.ae((self.records(s, 0), s.cpu_cells(0, 0)['text'], self.modes(s)), ([NONE], 'a', (4, 4)))
        # a multicell char stamps every row it covers, the row a later write lands on below it too
        s = self.screen()
        pb(s, '\x1b[8h' + CUP(2))
        draw_multicell(s, 'a', scale=2)
        self.ae(self.records(s, 0, 1, 2, 3), [NONE, LTR, LTR, NONE])
        s = self.screen(cols=2)
        draw_multicell(s, 'a', scale=2)
        pb(s, CUP(2) + '\x1b[8hX')
        self.ae(self.records(s, 0, 1, 2), [NONE, NONE, LTR])

    def test_stamp_lands_on_the_row_written(self):
        # (columns, data before CSI 8 h, data after it, the rows stamped)
        for cols, pre, data, want in (
            (5, 'abc', '\r', ()),
            (5, 'abc', '\x07', ()),
            (5, 'abc', '\r\n', ()),
            (5, 'abc', '\x1b[2 k\n', ()),
            (5, 'abc', '\u0085', ()),
            (5, 'abc', '\u0080\u009f', ()),
            (5, 'abc', '\x7f', ()),
            (5, 'abc', '\u0085d', (0,)),
            (5, 'abc', '\rdef', (0,)),
            (5, 'abcde', 'X', (1,)),
            (5, 'abcde', '\tX', (1,)),
            (5, 'abcde', '\x07X', (1,)),
            (5, 'abcd', '世', (1,)),
            (5, 'abcde', '\rX', (0,)),
            (5, 'abcde', '\x08X', (0,)),
            (5, '\x1b[?7labcde', 'X', (0,)),
            (5, 'abcde', '\x08世', (1,)),  # BS and HT leave the column at the last one
            (5, 'abc', '\t世', (1,)),
            (9, 'abcdefgh\r', '\t世', (1,)),
            (5, 'abcde', '́', (0,)),  # zero width joins the last cell
            (5, 'abcde', '\u200b', (0,)),
            (5, 'abc\r', '́', ()),  # zero width at the line start is dropped
            (5, 'abc\r', '\u200b', ()),
            (1, 'a\r', '世', ()),  # wider than the line
            (5, 'abc', '世', (0,)),
            (5, 'abc\r\nxyz\r', '\x08', ()),
            (5, 'abc\r\nxyz\r', '\x08Q', (0,)),
            (5, '\r\nxyz' + CUP(1), '\n', ()),
            (5, '\r\nxyz' + CUP(1) + 'abcde', '\t', ()),
            (5, '\x1b(U', '\x01', (0,)),  # the charset maps a control to a glyph
            (10, '\x1b)U', '\x0e\x01', (0,)),
            (10, '\x1b)U', '\x01', ()),
            (5, 'abc\U0001f44d', '\U0001f3fd', (0,)),
            (5, 'abc\U0001f1fa', '\U0001f1f8', (0,)),
            (5, 'abcdef\r', '́', (0,)),
            (5, 'abcdef\r', '\u200b', (0,)),
            (5, 'abcd☺', '️', (0, 1)),  # the widened char moves to the next row
            (10, 'a' + '́' * 23, '́', ()),  # a combining char the cell has no room for
        ):
            with self.subTest(pre=pre, data=data):
                s = self.screen(cols=cols)
                pb(s, pre + '\x1b[8h' + data)
                self.ae(tuple(y for y in range(s.lines) if s.test_bidi_line_record(y)[0]), want)

    def test_controls_mark_nothing_dirty(self):
        s = self.screen()
        pb(s, '\x1b[8ha\r\nb\r\x1b[K')
        s.reset_dirty()
        dirty = s.linebuf.dirty_lines()
        for combo in product(('\x1b[8h', '\x1b[8l', '\x1b[0 k', '\x1b[1 k', '\x1b[2 k', '\x1b[?2501h', '\x1b[?2501l'), repeat=3):
            pb(s, ''.join(combo))
            self.ae(s.linebuf.dirty_lines(), dirty, combo)
        self.ae(self.records(s, 1), [NONE])

    def test_controls_lifetime(self):
        # the alternate screen starts with every control reset, and the main screen's come back on leaving it
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?2501h\x1b[2 k\x1b[?1049h')
        self.ae(self.modes(s), (2, 2))
        pb(s, '\x1b[8ha')
        self.ae((self.modes(s), self.records(s, 0)), ((1, 2), [LTR]))
        pb(s, '\x1b[?1049l' + CUP(2) + 'b')
        self.ae((self.modes(s), self.records(s, 1)), ((1, 1), [(1, True, True)]))
        pb(s, '\x1b[?1049h\x1b[8h\x1b[8l\x1b[?1049l' + CUP(3) + 'c')
        self.ae(self.records(s, 2), [(1, True, True)])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?2501h\x1b[?1049h\x1b[?47h\x1b[?1047h\x1b[8h\x1b[?47l')  # nested switches stash once
        self.ae(self.modes(s), (1, 1))
        pb(s, '\x1b[?1047h\x1b[?1049h\x1b[?1049l')
        self.ae(self.modes(s), (1, 1))
        # RIS and DECSTR reset them on both screens
        for reset, in_alt in product(('\x1bc', '\x1b[!p'), (False, True)):
            with self.subTest(reset=reset, in_alt=in_alt):
                s = self.screen()
                pb(s, '\x1b[8h\x1b[?2501h\x1b[2 k\x1b[?1049h\x1b[8h\x1b[2 k' + ('' if in_alt else '\x1b[?1049l') + reset)
                pb(s, '\x1b[?1049l\x1b[?1049h\x1b[?1049l')
                self.ae(self.modes(s), (2, 2))
                pb(s, '\x1b[8h' + CUP(5) + 'x')
                self.ae(self.records(s, 4), [LTR])
        # XTSAVE and XTRESTORE keep one terminal-global copy of the modes, DECSC and DECRC none
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?2501h\x1b[?s\x1b[8l\x1b[?2501l\x1b[?r')
        self.ae(self.modes(s), (1, 1))
        pb(s, '\x1b[?2501s\x1b[?2501l\x1b[?2501r')
        self.ae(self.modes(s), (1, 1))
        pb(s, '\x1b[2 k\x1b7\x1b[8l\x1b[?2501l\x1b[1 k\x1b8')
        self.ae(self.modes(s), (2, 2))
        pb(s, '\x1b[8h\x1b[?2501h\x1b[?s\x1b[?1049h')
        self.ae(self.modes(s), (2, 2))
        pb(s, '\x1b[?r')
        self.ae(self.modes(s), (1, 1))

    def test_record_lifetime(self):
        # after an erase or edit a row keeps its record if and only if a cell still holds text, and the next write to a row
        # without one records the controls current then
        rtl_then_ltr = '\x1b[8h\x1b[2 ka\x1b[1 k' + CUP(1)
        self.ae(self.written('\x1b[8hאבג\x1b[8l\r\x1b[2Kדהו\r\nדהו', 0, 1), [NONE, NONE])
        self.ae(self.written(rtl_then_ltr + '\x1b[2K', 0), [NONE])
        self.ae(self.written(rtl_then_ltr + '\x1b[2Kb', 0), [LTR])
        self.ae(self.written(rtl_then_ltr + '\x1b[2Jb', 0), [LTR])
        self.ae(self.written(rtl_then_ltr + '\x1b[Lb', 0, 1), [LTR, RTL])
        self.ae(self.written(rtl_then_ltr + '\x1b[M', 0), [NONE])  # DL pulls a fresh line up
        self.ae(self.written('\x1b[8h\x1b[2 ka\r\x1b[K\x1b[8l', 0), [NONE])
        self.ae(self.written('\x1b[8h\x1b[2 k\x1b[4Gab\x1b[4G\x1b[K\x1b[1 k\x1b[1Gc', 0), [LTR])
        for x, op in (('1', '2K'), ('4', 'K'), ('7', '1K'), ('4', '?K'), ('1', '10X'), ('3', '8X'), ('1', '10P'), ('4', '2P'), ('1', '10@'), ('4', '7@')):
            self.ae(self.written(f'\x1b[8h\x1b[2 k\x1b[4Gab\x1b[{x}G\x1b[{op}', 0), [NONE], op)
        for x, op in (('5', 'K'), ('4', '1K'), ('5', '?K'), ('1', '2X'), ('1', '2P'), ('1', '2@'), ('5', 'J'), ('5', '1J'), ('1', '1@')):
            self.ae(self.written(f'\x1b[8h\x1b[2 k\x1b[4Gabc\x1b[8l\x1b[{x}G\x1b[{op}', 0), [RTL], op)
        self.ae(self.written('\x1b[8h\x1b[2 k\x1b[3Gabc\x1b[8G\x1b[1J', 0), [NONE])  # ED 1 leaving the row blank
        self.ae(self.written('\x1b[8h\x1b[2 k\x1b[9Gab\x1b[9G\x1b[2@', 0), [NONE])  # ICH pushing all text off
        # DECSTR keeps records, a resize keeps them with the rows, rewrapped or not
        s = self.screen()
        pb(s, '\x1b[8ha\r\nx\r\x1b[K\x1b[?1049h\x1b[8hb\r\ny\r\x1b[K\x1b[!p')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        pb(s, '\x1b[?1049l')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        for lines, cols in ((5, 6), (8, 10)):
            s = self.screen()
            pb(s, '\x1b[8h\x1b[2 kabc\r\n')
            s.resize(lines, cols)
            self.ae(self.records(s, 0), [RTL], (lines, cols))

    def cell_texts(self, s, y=0, n=4):
        return [s.cpu_cells(y, x)['text'] for x in range(n)]

    def mark_screen(self, data, cols=10, lines=5, bdsm=True):
        s = self.screen(cols=cols, lines=lines)
        pb(s, ('\x1b[8h' if bdsm else '') + data)
        return s

    def test_leading_mark(self):
        # In bidi mode an LRM, RLM, LRI, RLI, FSI or PDI drawn with no previous cell goes in the text of the cell under the
        # cursor, after the controls it already starts with, and the cursor does not move. A char drawn there with no
        # previous cell is stored after them.
        for data, cells, x in (
            ('abc\r' + RLM, [RLM + 'a', 'b', 'c', ''], 0),
            ('abc\r' + RLM + 'xyz', [RLM + 'x', 'y', 'z', ''], 3),
            (RLM + 'a\rb', [RLM + 'b', '', '', ''], 1),
            ('abc\r' + RLM + '\x1b[2CX', [RLM + 'a', 'b', 'X', ''], 3),
            (RLM + '\tx', [RLM, '', '', ''], 9),
            (LRM + RLM + 'a\r' + LRM + 'b', [LRM + RLM + LRM + 'b', '', '', ''], 1),
            (RLM + '中x', [RLM + '中', '', 'x', ''], 3),
            ('中x\r' + RLM + 'y', [RLM + 'y', '', 'x', ''], 1),
            ('ab' + RLM + 'c', ['a', 'b' + RLM, 'c', ''], 3),
            (RLI + 'ab' + PDI, [RLI + 'a', 'b' + PDI, '', ''], 2),
            (RLI + PDI + 'ab', [RLI + PDI + 'a', 'b', '', ''], 2),
            (FSI + LRI + LRM + 'a', [FSI + LRI + LRM + 'a', '', '', ''], 1),
            (RLI + 'é', [RLI + 'é', '', '', ''], 1),
            # VS16 and SARA AM widen the cell, as without the controls
            (LRM + '✖️x', [LRM + '✖️', '', 'x', ''], 3),
            (LRM + 'กำx', [LRM + 'กำ', '', 'x', ''], 3),
            # in insert mode the cell moves right with its controls, as any cell content does
            (LRM + 'abc\r\x1b[4hX', ['X', LRM + 'a', 'b', 'c'], 1),
            (LRM + 'abc\r\x1b[4h中', ['中', '', LRM + 'a', 'b'], 2),
            # and so does ICH
            ('abc\r' + LRM + '\r\x1b[@', ['', LRM + 'a', 'b', 'c'], 0),
            ('\r' + LRM + 'abc\r\x1b[2@', ['', '', LRM + 'a', 'b'], 0),
            ('\r' + LRM + '\x1b[@', ['', LRM, '', ''], 0),
            ('\x1b]66;w=2;a\x07bc\r' + LRM + '\r\x1b[@', ['', LRM + 'a', '', 'b'], 0),
            ('abc\r' + LRM + '\x1b[C\x1b[@', [LRM + 'a', '', 'b', 'c'], 1),
            # an OSC 66 char written into the cell replaces it, controls included
            (LRM + '\x1b]66;w=1;Z\x07', ['Z', '', '', ''], 1),
        ):
            with self.subTest(data=data):
                s = self.mark_screen(data)
                self.ae((self.cell_texts(s), s.cursor.x), (cells, x))
        # over an OSC 66 char, or a wide char, insert mode erases it, as without bidi mode
        for data in (
            '\x1b]66;n=1:d=2;a\x07bc\r' + LRM + '\x1b[4hX',
            '\x1b]66;w=1;中\x07bc\r' + LRM + '\x1b[4hX',
            '\x1b]66;w=1;ab\x07bc\r' + LRM + '\x1b[4hX',
            '\x1b]66;;a\x07bc\r' + LRM + '\x1b[4hX',
            LRM + '中x\r\x1b[4hX',
        ):
            with self.subTest(data=data):
                s, off = self.mark_screen(data), self.mark_screen(data, bdsm=False)
                self.ae((self.cell_texts(s), s.cursor.x), (self.cell_texts(off), off.cursor.x))
        # the OSC 66 char that ICH shifts keeps its shape
        self.ae(self.mark_screen('\x1b]66;n=1:d=2;a\x07\r' + LRM + '\r\x1b[@').cpu_cells(0, 1)['mcd']['subscale_d'], 2)
        # a plain cell holding controls stays a plain cell
        self.assertIsNone(self.mark_screen('abc\r' + RLM).cpu_cells(0, 0)['mcd'])
        # copied text holds the controls
        self.ae(str(self.mark_screen('abc\r' + RLM).line(0)), RLM + 'abc')
        self.ae(str(self.mark_screen(RLM + '\tx').line(0)), RLM + ' ' * 7 + 'x')
        # over a multiline char the mark goes into all its cells, which a char drawn there replaces
        s = self.mark_screen('\x1b]66;s=2;a\x07' + CUP(1) + RLM, cols=4, lines=3)
        self.ae(s.cpu_cells(0, 0)['text'], RLM + 'a')
        pb(s, 'q')
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3), s.cpu_cells(1, 0)['mcd']), ([RLM + 'q', '', ''], ['', '', ''], None))
        # and records every row it is written to, also for a char drawn before bidi mode
        s = self.mark_screen('\x1b]66;s=2;a\x07\x1b[8h\r' + RLM, cols=4, lines=3, bdsm=False)
        self.ae((s.cpu_cells(0, 0)['text'], self.records(s, 0, 1)), (RLM + 'a', [LTR, LTR]))

    def test_leading_mark_dropped(self):
        # on the lower row of a multiline char no cell can take the mark
        s = self.mark_screen('\x1b]66;s=2;a\x07' + CUP(2) + RLM, cols=4, lines=3)
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3)), (['a', '', ''], ['', '', '']))
        # a cell keeps room for one code point after its marks
        marks = (LRM + RLM) * 12
        self.ae(self.cell_texts(self.mark_screen(marks), n=2), [marks[:23], ''])
        self.ae(self.cell_texts(self.mark_screen(marks + 'a'), n=2), [marks[:23] + 'a', ''])
        # a tab cell takes no mark: it is dropped, as without bidi mode
        for data in ('\tX\r' + RLM + '\t', '\x1b[3g\x1b[1;15H\x1bH\r\t\r' + LRM + '\x1b[1;15Hls'):
            with self.subTest(data=data):
                texts = []
                for bdsm in (True, False):
                    s = self.mark_screen(data, cols=20, bdsm=bdsm)
                    text, ansi = [], []
                    s.as_text(text.append, False)
                    s.as_text(ansi.append, True)
                    s.start_selection(0, 0)
                    s.update_selection(19, 0, True)
                    texts.append((str(s.line(0)), ''.join(text), ''.join(ansi), s.text_for_selection(False, False)))
                self.assertIn('\t', texts[1][0])
                self.ae(texts[0], texts[1])

    def test_leading_mark_without_bidi_mode(self):
        # as without bidi support: the mark is dropped
        for data, text in (('abc\r' + RLM, 'abc'), ('abc\r' + RLM + '\x1b[2CX', 'abX'), (RLM + '\tx', '\tx'), (RLM, ''), (RLM + 'hello world', 'hello world')):
            with self.subTest(data=data):
                self.ae(str(self.mark_screen(data, cols=12, bdsm=False).line(0)), text)
        # ICH in column 0 only shifts the cell, marks in its text included
        s = self.mark_screen('\x1b]66;w=1;' + LRM + 'a\x07bc\r\x1b[@', bdsm=False)
        self.ae(self.cell_texts(s), ['', LRM + 'a', 'b', 'c'])
        # a cell written in bidi mode is overwritten as any cell is
        self.ae(self.cell_texts(self.mark_screen(RLM + 'a\x1b[8l\rb'), n=2), ['b', ''])

    def test_leading_mark_word_selection(self):
        def word(s):
            s.start_selection(2, 0, False, EXTEND_WORD)
            s.update_selection(2, 0, True)
            return s.text_for_selection(False, False)

        self.ae(word(self.mark_screen(RLM + 'hello world', cols=20)), (RLM + 'hello',))
        # without bidi mode an OSC 66 cell whose text starts with a mark is read as it is
        self.ae(word(self.mark_screen('\x1b]66;w=1;' + RLM + 'h\x07ello world', cols=20, bdsm=False)), ('ello',))

    def test_leading_mark_marker(self):
        for bdsm, data in ((True, RLM + 'foo bar foo'), (True, RLM + RLI + 'foo bar foo'), (False, 'foo bar foo')):
            with self.subTest(data=data):
                s = self.mark_screen(data, cols=20, bdsm=bdsm)
                s.set_marker(marker_from_regex('foo', 1))
                self.ae(s.marked_cells(), [(x, 0, 1) for x in (0, 1, 2, 8, 9, 10)])
        # a cell holding only controls
        s = self.mark_screen(RLM + '\x1b[Cfoo', cols=20)
        s.set_marker(marker_from_regex('foo', 1))
        self.ae(s.marked_cells(), [(x, 0, 1) for x in (1, 2, 3)])

    def test_leading_mark_image_placeholder(self):
        def refs(marks):
            s, dx, dy, put_image, put_ref, layers, rect_eq = put_helpers(self, 10, 20)
            put_image(s, 20, 20, num_cols=4, num_lines=2, unicode_placeholder=1, id=42)
            pb(s, '\x1b[8h' + marks + '\x1b[38;5;42m\U0010eeee\u0305\u0305\U0010eeee\u0305\u030d')
            s.update_only_line_graphics_data()
            return [(r['src_rect'], r['dest_rect']) for r in layers(s)]

        # the placeholder after the controls in the first cell starts the image there
        expected = refs('')
        self.ae(len(expected), 1)
        for marks in (LRM, RLM + LRI):
            with self.subTest(marks=marks):
                self.ae(refs(marks), expected)
