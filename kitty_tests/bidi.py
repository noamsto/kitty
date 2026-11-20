#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, kitty contributors

import random
import re
from itertools import product

from .base import BaseTest, draw_multicell, parse_bytes

NONE, LTR, RTL = (0, False, False), (1, False, False), (1, True, False)
IA = '\x1b[8h\x1b[?2501h'
CUP = '\x1b[{};1H'.format  # 1-based row


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
            (5, 'abcd☺', '\ufe0f', (0, 1)),  # the widened char moves to the next row
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

    def test_row_left_without_text_loses_bidi_bits(self):
        # Whatever removes the last text of a row takes its record, also when the text is part of a scaled or wide char removed
        # through another of its rows, or is in the history
        cup = '\x1b[{};{}H'.format
        s2, s3, vs16, prompt = '\x1b]66;s=2;א\x07', '\x1b]66;s=3;א\x07', '️', '\x1b]133;A\x07'
        w1, s7w2 = '\x1b]66;w=1;א\x07', '\x1b]66;s=7:w=2;א\x07'
        in_history = cup(2, 1) + s2 + cup(3, 1) + '\n\n'  # on three lines the char's top row goes into the history

        def emptied(lines, text, op, rows, pre='', cols=10):
            # text written in bidi mode, then op leaves rows without text
            s = self.screen(cols=cols, lines=lines)
            pb(s, pre + '\x1b[8h' + text + '\x1b[8l')
            op(s) if callable(op) else pb(s, op)
            self.ae(self.records(s, *rows), [NONE] * len(rows), (pre, text, op))

        def erase_command_and_pull(s):
            # the lower row of the char is the erased command's prompt line, its top row stays in the history
            s.erase_last_command()
            pb(s, '\x1b[+T')

        for case in (
            (5, s2, cup(2, 1) + '\x1b[2K', (0,)),
            (5, s2, cup(2, 1) + '\x1b[K', (0,)),
            (5, s2, cup(2, 1) + '\x1b[X', (0,)),
            (5, s2, cup(2, 1) + '\x1b[P', (0,)),
            (5, s2, cup(2, 1) + '\x1b[@', (0,)),
            (5, s3, cup(2, 1) + '\x1b[J', (0,)),
            (5, s3, cup(2, 1) + '\x1b[1J', (2,)),
            (5, s2, cup(2, 1) + '\x1b[M', (0,)),
            (5, cup(2, 1) + s2, cup(1, 1) + '\x1b[2M', (0,)),
            (5, s2, cup(2, 1) + '\x1b[L', (0, 2)),
            (5, cup(2, 1) + s2, '\x1b[1;2r\x1b[L\x1b[r', (2,)),
            (5, cup(3, 1) + s2, '\x1b[1;3r' + cup(3, 1) + '\n\x1b[r', (1, 3)),
            (5, cup(3, 1) + s2, '\x1b[1;3r\x1b[S\x1b[r', (1, 3)),
            (5, cup(2, 1) + s2, '\x1b[3;5r' + cup(3, 1) + '\x1bM\x1b[r', (1, 3)),
            (5, cup(2, 1) + s2, '\x1b[3;5r\x1b[T\x1b[r', (1, 3)),
            (3, in_history, cup(1, 1) + '\x1b[2K\x1b[+T', (0,)),
            (3, in_history, '\x1b[2J\x1b[+T', (0,)),
            (3, in_history, lambda s: s.clear_scrollback(), (0,)),
            (3, prompt + '$ p1\r\n' + s2 + cup(3, 3) + prompt + '\n\n', lambda s: s.erase_last_command(), (0,)),
            (3, prompt + '$ p0\r\n' + s2 + cup(3, 3) + prompt + '$ p1\r\n\r\n\r\n' + prompt, erase_command_and_pull, (0,)),
            (5, s2, cup(1, 1) + 'z', (1,)),
            (5, s2, cup(1, 1) + '中', (1,)),
            (5, s2, cup(1, 1) + '\x1b]66;w=1;z\x07', (1,)),
            (5, 'y' + s2, cup(1, 1) + 'y\x1b[b', (1,)),
            (5, cup(1, 2) + s2, cup(1, 1) + '☺' + vs16, (1,)),
            (5, cup(2, 1) + s2, cup(1, 1) + '\x1b]66;s=2;zz\x07', (2,)),
            (5, cup(1, 10) + '☺', vs16, (0,)),
            (5, cup(1, 10) + '☺', '\x1b[8h' + vs16, (0,)),
            # a wide char does not fit in the last column, it wraps or is discarded
            (5, cup(1, 10) + w1, cup(1, 10) + '中', (0,)),
            # a char written in place of a char written with OSC 66 into the last column records the line afresh
            (5, cup(1, 10) + w1, cup(1, 10) + 'z', (0,)),
            (5, cup(1, 10) + w1, cup(1, 10) + '\x1b[?7l中', (0,)),
            # the region scrolls up for a char that does not fit above its bottom margin, the rows below it are not written
            (5, cup(3, 1) + s2, '\x1b[1;3r' + cup(3, 1) + '\x1b]66;s=2;zz\x07\x1b[r', (3,)),
            (5, cup(3, 1) + s2, '\x1b[1;3r' + cup(3, 1) + '\x1b]66;s=3;z\x07\x1b[r', (3,)),
            # the scroll tears the last char of the row the char lands on first, which is its first write
            (5, cup(3, 1) + '\x1b]66;s=2;ab\x07', '\x1b[1;3r' + cup(3, 1) + '\x1b]66;s=2;z\x07\x1b[r', (1, 2, 3)),
        ):
            emptied(*case)
        # a resize drops a char too wide for the new width, and moves rows left without text into the history
        emptied(9, cup(2, 3) + s7w2, lambda s: s.resize(9, 11), range(1, 8), cols=16)
        emptied(9, s7w2 + cup(8, 1) + 'x' * 32, lambda s: (s.resize(9, 11), pb(s, '\x1b[+T')), (0,), cols=16)
        # a rewrap that leaves the first row of a line blank drops its record there
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 k' + cup(1, 10) + 'ab\x1b[8l')
        s.resize(5, 5)
        self.ae(self.records(s, 0, 1, 2), [NONE, RTL, RTL])
        # the alternate screen resizes without rewrapping, removing the chars split at its new edges
        emptied(5, cup(1, 9) + '中', lambda s: s.resize(5, 9), (0,), pre='\x1b[?1049h')
        emptied(5, cup(4, 1) + s2, lambda s: s.resize(4, 10), (3,), pre='\x1b[?1049h')
        # a char written over the only char of a row leaves it holding text, with its record
        for data, rows, want in (
            ('\x1b[2 k' + s2 + '\x1b[8l' + cup(1, 1) + 'z', (0, 1), [RTL, NONE]),
            ('中\x1b[8l' + cup(1, 1) + 'z', (0,), [LTR]),
            (s2 + '\x1b[8l' + cup(1, 1) + '\x1b]66;s=2;zz\x07', (0, 1), [LTR, LTR]),
            (cup(2, 1) + s2 + '\x1b[8l' + cup(1, 1) + '\x1b]66;s=2;zz\x07', (0, 1, 2), [NONE, LTR, NONE]),
            (cup(2, 1) + s2 + '\x1b[8l' + cup(1, 1) + '\x1b]66;s=3;zz\x07', (0, 1, 2), [NONE, LTR, LTR]),
            # the row the char is written to scrolls up with the cursor
            (cup(3, 1) + s2 + '\x1b[8l\x1b[1;3r' + cup(3, 1) + '\x1b]66;s=2;zz\x07\x1b[r', (1, 2, 3), [LTR, NONE, NONE]),
            # below the region, written where it is or moved up
            (cup(4, 1) + s2 + '\x1b[8l\x1b[1;2r' + cup(4, 1) + '\x1b]66;s=2;zz\x07\x1b[r', (3, 4), [LTR, LTR]),
            (cup(5, 1) + w1 + '\x1b[8l\x1b[1;2r' + cup(5, 1) + '\x1b]66;s=2;z\x07\x1b[r', (3, 4), [NONE, LTR]),
            # at the end of a line without autowrap
            (cup(1, 9) + s2 + '\x1b[8l\x1b[?7l' + cup(1, 10) + '\x1b]66;s=2;z\x07', (0, 1), [LTR, LTR]),
            (cup(1, 9) + w1 + '\x1b[8l' + cup(1, 9) + 'z', (0,), [LTR]),
            (cup(1, 9) + w1 + '\x1b[8l' + cup(1, 9) + '中', (0,), [LTR]),
            (cup(1, 10) + 'a\x1b[8l' + cup(1, 10) + 'z', (0,), [LTR]),
        ):
            self.ae(self.written('\x1b[8h' + data, *rows), want, data)
        # on a one-column screen a widened narrow char has no room and goes, as a wide char does, leaving the row without text
        self.ae(self.written('\x1b[8h☺' + vs16, 0, cols=1), [NONE])
        self.ae(self.written('\x1b[8h' + w1 + '\x1b[8l\x1b[H中', 0, cols=1), [NONE])

    def test_no_row_without_text_keeps_bidi_bits(self):
        # Seeded random streams of the operations that remove text. After each, no row holding no text has a record.
        rng = random.Random(1)
        alt, main, pull = '\x1b[?1049h', '\x1b[?1049l', '\x1b[+T'

        def has_text(s, y):
            return any(c['text'] or c['mcd'] is not None for c in s.cpu_cells(y))

        def op():
            n, y, x = rng.randrange(1, 4), rng.randrange(1, 7), rng.randrange(1, 10)
            return rng.choice(
                (
                    f'\x1b[{y};{x}H',
                    f'\x1b[{y}H',
                    'a',
                    'אב',
                    '中',
                    'z ',
                    'b\x1b[2b',
                    '\t',
                    '\r',
                    '\n',
                    '\x1bM',
                    *(f'\x1b]66;{t}\x07' for t in ('s=2;א', 's=2;ab', 's=3;א', 's=4;z', 'w=1;z', 'w=2;中')),
                    f'\x1b[{y};99H\x1b]66;w=1;z\x07',
                    f'\x1b[{y};99H☺️',
                    '☺️',
                    f'\x1b[{rng.randrange(3)}K',
                    f'\x1b[{n}X',
                    f'\x1b[{n}@',
                    f'\x1b[{n}P',
                    f'\x1b[{rng.randrange(4)}J',
                    f'\x1b[{n}L',
                    f'\x1b[{n}M',
                    f'\x1b[{n}S',
                    f'\x1b[{n}T',
                    pull,
                    f'\x1b[{rng.randrange(1, 3)};{rng.randrange(3, 6)}r',
                    '\x1b[r',
                    '\x1b[8h',
                    '\x1b[8l',
                    IA,
                    alt,
                    main,
                    '\x1b]133;A\x07',
                    ('resize', rng.choice((4, 5, 6)), rng.choice((3, 7, 8, 9))),
                    ('clear_scrollback',),
                    ('erase_last_command',),
                )
            )

        for _ in range(300):
            s = self.create_screen(cols=8, lines=5, scrollback=5, options={})
            self._screens_to_verify_styles.remove(s)
            done = []
            for _ in range(30):
                done.append(o := op())
                getattr(s, o[0])(*o[1:]) if isinstance(o, tuple) else pb(s, o)
                kept = [(y, r) for y in range(s.lines) if (r := s.test_bidi_line_record(y)) != NONE and not has_text(s, y)]
                self.assertFalse(kept, f'rows without text keep bidi bits after {done!r}')
