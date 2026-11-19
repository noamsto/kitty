#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import random
import re
from itertools import product

from kitty.fast_data_types import EXTEND_WORD

from .base import BaseTest, draw_multicell, parse_bytes

NONE, LTR, RTL = (0, False, False), (1, False, False), (1, True, False)
CUP = '\x1b[{};1H'.format  # 1-based row
LRM, RLM = '‎', '‏'


def pb(s, data):
    parse_bytes(s, data.encode())


def leading_marks_codeword(label):
    # a distinct 7-bit pattern of marks for each label
    return ''.join((LRM, RLM)[int(label[1:]) >> bit & 1] for bit in range(6, -1, -1))


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
        # under force_ltr the modes stay reset, as DECRQM reports them, so a leading LRM is dropped
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
            (5, 'abcde', '​', (0,)),
            (5, 'abc\r', '́', ()),  # zero width at the line start is dropped
            (5, 'abc\r', '​', ()),
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
            (5, 'abcdef\r', '​', (0,)),
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
        # In bidi mode an LRM or RLM drawn with no previous cell goes first in the text of the cell under the cursor,
        # which does not move, and a char drawn there with no previous cell replaces what follows the marks
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
            # in insert mode a narrow char after the marks moves right, and a wide one is replaced, as without marks
            (LRM + 'abc\r\x1b[4hX', [LRM + 'X', 'a', 'b', 'c'], 1),
            (LRM + 'abc\r\x1b[4h中', [LRM + '中', '', 'a', 'b'], 2),
            (LRM + '中x\r\x1b[4hX', [LRM + 'X', '', '', 'x'], 1),
            # an OSC 66 char under the marks is erased, as without marks
            ('\x1b]66;n=1:d=2;a\x07bc\r' + LRM + '\x1b[4hX', [LRM + 'X', '', 'b', 'c'], 1),
            ('\x1b]66;w=1;中\x07bc\r' + LRM + '\x1b[4hX', [LRM + 'X', '', 'b', 'c'], 1),
            ('\x1b]66;w=1;ab\x07bc\r' + LRM + '\x1b[4hX', [LRM + 'X', '', 'b', 'c'], 1),
            # ICH in column 0 leaves the marks there and shifts what follows them
            ('abc\r' + LRM + '\r\x1b[@', [LRM, 'a', 'b', 'c'], 0),
            ('\r' + LRM + 'abc\r\x1b[2@', [LRM, '', 'a', 'b'], 0),
            ('\r' + LRM + '\x1b[@', [LRM, '', '', ''], 0),
            ('\x1b]66;n=1:d=2;a\x07bc\r' + LRM + '\r\x1b[@', [LRM, 'a', 'b', 'c'], 0),
            ('\x1b]66;w=2;a\x07bc\r' + LRM + '\r\x1b[@', [LRM, 'a', '', 'b'], 0),
            ('\x1b]66;s=2;a\x07bc\r' + LRM + '\r\x1b[@', [LRM, '', '', 'b'], 0),
            ('abc\r' + LRM + '\x1b[C\x1b[@', [LRM + 'a', '', 'b', 'c'], 1),
        ):
            with self.subTest(data=data):
                s = self.mark_screen(data)
                self.ae((self.cell_texts(s), s.cursor.x), (cells, x))
        # the OSC 66 char that ICH shifts keeps its shape
        self.ae(self.mark_screen('\x1b]66;n=1:d=2;a\x07\r' + LRM + '\r\x1b[@').cpu_cells(0, 1)['mcd']['subscale_d'], 2)
        # copied text holds the marks
        self.ae(str(self.mark_screen('abc\r' + RLM).line(0)), RLM + 'abc')
        self.ae(str(self.mark_screen(RLM + '\tx').line(0)), RLM + ' ' * 7 + 'x')
        # over a multiline char the mark goes into all its cells, which a char drawn there replaces
        s = self.mark_screen('\x1b]66;s=2;a\x07' + CUP(1) + RLM, cols=4, lines=3)
        self.ae(s.cpu_cells(0, 0)['text'], RLM + 'a')
        pb(s, 'q')
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3), s.cpu_cells(1, 0)['mcd']), ([RLM + 'q', '', ''], ['', '', ''], None))

    def test_leading_mark_dropped(self):
        # on the lower row of a multiline char no cell can take the mark
        s = self.mark_screen('\x1b]66;s=2;a\x07' + CUP(2) + RLM, cols=4, lines=3)
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3)), (['a', '', ''], ['', '', '']))
        # a cell keeps room for one code point after its marks
        marks = (LRM + RLM) * 12
        self.ae(self.cell_texts(self.mark_screen(marks), n=2), [marks[:23], ''])
        self.ae(self.cell_texts(self.mark_screen(marks + 'a'), n=2), [marks[:23] + 'a', ''])

    def test_leading_mark_without_bidi_mode(self):
        # as without bidi support: the mark is dropped
        for data, text in (('abc\r' + RLM, 'abc'), ('abc\r' + RLM + '\x1b[2CX', 'abX'), (RLM + '\tx', '\tx'), (RLM, ''), (RLM + 'hello world', 'hello world')):
            with self.subTest(data=data):
                self.ae(str(self.mark_screen(data, cols=12, bdsm=False).line(0)), text)
        # ICH in column 0 only shifts the cell, marks in its text included
        s = self.mark_screen('\x1b]66;w=1;' + LRM + 'a\x07bc\r\x1b[@', bdsm=False)
        self.ae(self.cell_texts(s), ['', LRM + 'a', 'b', 'c'])
        # a cell written in bidi mode is overwritten as any multicell cell is
        self.ae(self.cell_texts(self.mark_screen(RLM + 'a\x1b[8l\rb'), n=2), ['b', ''])

    def test_leading_mark_word_selection(self):
        s = self.mark_screen(RLM + 'hello world', cols=20)
        s.start_selection(2, 0, False, EXTEND_WORD)
        s.update_selection(2, 0, True)
        self.ae(s.text_for_selection(False, False), (RLM + 'hello',))

    def test_leading_marks_randomized(self):
        # Marks are written only at column 0 of a line that carries a label, and ops that move, erase, scroll, resize and
        # overdraw lines run between them: a mark never ends up on a line without its label, nor away from column 0
        for seed in range(1, 3):
            with self.subTest(seed=seed):
                self.run_leading_marks_seed(seed)

    def check_marks_on_their_lines(self, s, labels, where):
        # Each label has its own codeword, so the marks of a line are a stretch of what was written for its label
        marks = LRM + RLM
        lines = [(f'row {y}', str(s.line(y))) for y in range(s.lines)]
        lines += [(f'history {i}', str(s.historybuf.line(i))) for i in range(s.historybuf.count)]
        for name, text in lines:
            if not any(m in text for m in marks):
                continue
            label = re.search(r'L\d{3}', text)
            if not label or label.group() not in labels:
                self.fail(f'{where}: {name} {text!r} holds a mark but its label was never marked')
            if any(m in text.lstrip(marks) for m in marks):
                self.fail(f'{where}: {name} {text!r} holds a mark away from column 0')
            cw = leading_marks_codeword(label.group()) * 4

            def cells_of(run):
                # a run holds the marks of adjacent cells, each a prefix of the label's codewords
                return not run or any(cw.startswith(run[:i]) and cells_of(run[i:]) for i in range(min(len(run), 23), 0, -1))

            for run in re.findall(f'[{marks}]+', text):
                if not cells_of(run):
                    self.fail(f'{where}: {name} {text!r} holds marks that were not written for its label')

    def run_leading_marks_seed(self, seed, steps=300):
        rnd = random.Random(f'1:{seed}')
        s = self.create_screen(cols=rnd.randint(10, 14), lines=rnd.randint(2, 6), scrollback=20)
        pb(s, '\x1b[8h')
        labels, log = set(), []

        def label_span(y):
            texts = [s.cpu_cells(y, x)['text'] for x in range(s.columns)]
            for x in range(s.columns - 3):
                if re.fullmatch(r'L\d{3}', ''.join(texts[x : x + 4])):
                    return x
            return None

        def crowded(y):
            # a multiline or wide cell near the start of a row moves a write or overlay right, onto the label
            for yy in range(y, min(y + 2, s.lines)):
                span = label_span(yy)
                if span is not None and span < 3:
                    return True
                for x in range(5):
                    cell = s.cpu_cells(yy, x)
                    mcd = cell['mcd']
                    if cell['text'] == '中' or (mcd and (mcd['scale'] > 1 or mcd['width'] > 1)):
                        return True
            return False

        def write_at_row(write):
            y = rnd.randrange(s.lines)
            return None if crowded(y) else CUP(y + 1) + write()

        def label_row():
            nonlocal next_label
            free = [y for y in range(s.lines) if not re.search(r'L\d{3}|[‎‏]', str(s.line(y)))]
            if not free:
                return None
            next_label += 1
            assert next_label < 128, 'labels would alias in the 7-bit codeword'
            return f'{CUP(rnd.choice(free) + 1)}\x1b[6GL{next_label:03d}'

        def mark_row():
            rows = [y for y in range(s.lines) if label_span(y) is not None]
            if not rows:
                return label_row()
            y = rnd.choice(rows)
            label = re.search(r'L\d{3}', str(s.line(y))).group()
            labels.add(label)
            return CUP(y + 1) + leading_marks_codeword(label)

        def overlay():
            if any(crowded(y) for y in range(s.lines)):
                return None
            y = rnd.randrange(s.lines)
            text = rnd.choice(('z', '\nz', '\n\nz', 'z\nz', 'xy\n', '\n\n\n\nz'))
            s.test_draw_overlay_line(text, 0, y)
            s.test_update_overlay_text('')
            return f'overlay {text!r} at row {y}'

        def insert_chars():
            # ICH, which must not push a label past the 10 columns a resize can narrow its row to
            y, n = rnd.randrange(s.lines), rnd.randint(1, 2)
            span = label_span(y)
            if crowded(y) or (span is not None and span + 4 + n > 10):
                return None
            return f'{CUP(y + 1)}\x1b[{rnd.choice((1, 1, 2, 3))}G\x1b[{n}@'

        def resize():
            lines, cols = rnd.randint(2, 6), rnd.randint(10, 14)
            s.resize(lines, cols)
            return f'resize {lines}x{cols}'

        # (weight, op); an op gives bytes to parse, a description of what it did itself, or None to skip the step
        ops = [
            (2, lambda: f'\x1b[{rnd.randint(1, s.lines)};{rnd.randint(1, s.columns)}H'),
            (3, lambda: rnd.choice(('\n', '\x1bD', '\x1bM', '\x1bE'))),
            (2, lambda: rnd.choice(('\x1b[r', f'\x1b[{rnd.randint(1, 2)};{rnd.randint(3, 6)}r'))),
            (3, lambda: f'\x1b[{rnd.randint(1, 3)}{rnd.choice("STLM")}'),
            (3, lambda: CUP(rnd.randint(1, s.lines)) + rnd.choice(('\x1b[2K', '\x1b[J', '\x1b[1J', '\x1b[2J', f'\x1b[{rnd.randint(1, 12)}X'))),
            (2, overlay),
            (2, lambda: write_at_row(lambda: f'\x1b]66;w=1:s={rnd.randint(1, 2)};x\x07')),
            (2, lambda: write_at_row(lambda: '\x1b[2G\x1b]66;s=2;a\x07')),
            (3, lambda: write_at_row(lambda: rnd.choice(('q', '中', 'é')))),
            (2, insert_chars),
            (3, label_row),
            (5, mark_row),
            (1, resize),
        ]
        for y in range(s.lines):
            pb(s, f'{CUP(y + 1)}\x1b[6GL{y + 1:03d}')
        next_label = s.lines
        for step in range(steps):
            result = rnd.choices([op for _, op in ops], [w for w, _ in ops])[0]()
            if result is None:
                continue
            log.append(result)
            if not result.startswith(('overlay', 'resize')):
                pb(s, result)
            self.check_marks_on_their_lines(s, labels, f'seed {seed} step {step}, ops (latest last): {log!r}')
