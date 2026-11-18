#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import random
import re
from itertools import product

from .base import BaseTest, draw_multicell, parse_bytes

NONE, LTR, RTL, AUTO = (0, False, False), (1, False, False), (1, True, False), (1, False, True)
POWER_ON = (False, False, False)
LRM, RLM, ALM = '\u200e', '\u200f', '\u061c'
ISOLATE_CONTROLS = '\u2066\u2067\u2068\u2069'  # LRI RLI FSI PDI
IA = '\x1b[8h\x1b[?2501h'
CUP = '\x1b[{};1H'.format  # 1-based row
LEADING_CONTROLS = LRM + RLM + ISOLATE_CONTROLS
LEADING_CONTROL_POOLS = {'isolates': ISOLATE_CONTROLS, 'all': LEADING_CONTROLS}
MAX_LEADING_CONTROLS = 23  # MAX_NUM_CODEPOINTS_PER_CELL - 1: a char still fits after them


class Dump(list):
    def __call__(self, window_id, *a):
        if a and a[0] != 'bytes':
            self.append(tuple(a))


def pb(s, data):
    parse_bytes(s, data.encode())


def leading_controls_codeword(label, pool):
    # a distinct pattern of 7 controls for each label: the multiplier is coprime to len(pool), so labels map one to one
    n = int(label[1:]) * 2654435761 % len(pool) ** 7
    return ''.join(pool[n // len(pool) ** i % len(pool)] for i in range(7))


def leading_control_faults(rows, labels, pool, strict):
    # rows: (name, raw text of each cell). A cell holding a leading control holds it first, as the start of what was written
    # for the label its row carries. strict: only in the row's column 0 cell
    faults = []
    for name, cells in rows:
        label = re.search(r'L\d{3}', ''.join(cells))
        for x, text in enumerate(cells):
            if not any(ch in LEADING_CONTROLS for ch in text):
                continue
            where = f'{name} column {x} {text!r} of {"".join(cells)!r}'
            run = text[: len(text) - len(text.lstrip(LEADING_CONTROLS))]
            if not label or label.group() not in labels:
                faults.append(f'{where}: holds a control but its row carries no label controls were written for')
            elif not (leading_controls_codeword(label.group(), pool) * 4)[:MAX_LEADING_CONTROLS].startswith(run):
                faults.append(f'{where}: holds controls that were not written for its label')
            if any(ch in LEADING_CONTROLS for ch in text[len(run) :]):
                faults.append(f'{where}: holds a control after another char')
            if strict and x:
                faults.append(f'{where}: holds a control away from column 0')
    return faults


class TestBidi(BaseTest):
    def screen(self, cols=10, lines=5, **opts):
        return self.create_screen(cols=cols, lines=lines, options=opts)

    def records(self, s, *rows):
        return [s.test_bidi_line_record(y) for y in rows]

    def reply(self, s, data):
        s.callbacks.clear()
        pb(s, data)
        return s.callbacks.wtcbuf

    def test_autodetect_decrqm(self):
        s = self.screen()
        self.ae(self.reply(s, '\x1b[?2501$p'), b'\x1b[?2501;2$y')
        for setup, ans in (('\x1b[?2501h', 1), ('\x1b[?2501l', 2), ('\x1b[?2501h', 1)):
            self.ae(self.reply(s, setup + '\x1b[?2501$p'), f'\x1b[?2501;{ans}$y'.encode(), setup)
        s = self.screen(force_ltr=True)
        for setup in ('', '\x1b[?2501h', '\x1b[?2501l'):
            self.ae(self.reply(s, setup + '\x1b[?2501$p'), b'\x1b[?2501;4$y', setup)

    def test_stamp(self):
        s = self.screen()
        pb(s, 'a')
        self.ae(self.records(s, 0), [NONE])
        pb(s, '\x1b[8h' + CUP(2) + 'b')
        self.ae(self.records(s, 0, 1), [NONE, LTR])
        pb(s, '\x1b[8l' + CUP(2) + 'c')  # explicit write keeps the record
        self.ae(self.records(s, 1), [LTR])
        pb(s, CUP(4) + 'd')
        self.ae(self.records(s, 3), [NONE])
        s = self.screen()
        pb(s, '\x1b[8ha\r\n\x1b[8lb')  # the LF writes nothing to row 1 and b is explicit
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        pb(s, '\x1b[8h\x1b[8l' + CUP(4) + 'c')
        self.ae(self.records(s, 3), [NONE])

    def test_control_only_draw_does_not_stamp(self):
        for x in ('\r', '\x07', '\n', '\r\n', '\x1b[2 k\n'):
            with self.subTest(x=x):
                s = self.screen()
                pb(s, 'abc')
                pb(s, '\x1b[8h' + x)
                self.ae(self.records(s, 0), [NONE])
                if '\n' in x:
                    self.ae(self.records(s, 1), [NONE])
        for x in ('def', '\rdef'):
            with self.subTest(x=x):
                s = self.screen()
                pb(s, 'abc')
                pb(s, '\x1b[8h' + x)
                self.ae(self.records(s, 0), [LTR])

    def test_c1_only_draw_does_not_stamp(self):
        for x in ('\u0085', '\u0080\u009f', '\x7f'):
            with self.subTest(x=x):
                s = self.screen()
                pb(s, 'abc')
                pb(s, '\x1b[8h' + x)
                self.ae(self.records(s, 0), [NONE])
        s = self.screen()
        pb(s, 'abc\x1b[8h\u0085d')
        self.ae(self.records(s, 0), [LTR])

    def test_wrapping_first_char_stamps_the_row_it_lands_on(self):
        for x in ('X', '\tX', '\x07X'):
            with self.subTest(x=x):
                s = self.screen(cols=5)
                pb(s, 'abcde\x1b[8h' + x)
                self.ae(self.records(s, 0, 1), [NONE, LTR])
        with self.subTest(x='wide'):
            s = self.screen(cols=5)
            pb(s, 'abcd\x1b[8h\u4e16')
            self.ae(self.records(s, 0, 1), [NONE, LTR])
        for x in ('\rX', '\x08X'):
            with self.subTest(x=x):  # still lands on row 0
                s = self.screen(cols=5)
                pb(s, 'abcde\x1b[8h' + x)
                self.ae(self.records(s, 0, 1), [LTR, NONE])
        with self.subTest(x='no autowrap'):
            s = self.screen(cols=5)
            pb(s, '\x1b[?7labcde\x1b[8hX')
            self.ae(self.records(s, 0, 1), [LTR, NONE])
        for pre, x in (('abcde', '\x08\u4e16'), ('abc', '\t\u4e16'), ('abcdefgh\r', '\t\u4e16')):
            with self.subTest(pre=pre, x=x):  # BS and HT leave the column at the last one
                s = self.screen(cols=9 if pre.startswith('abcdefgh') else 5)
                pb(s, pre + '\x1b[8h' + x)
                self.ae(self.records(s, 0, 1), [NONE, LTR])
        for x in ('\u0301', '\u200b'):
            with self.subTest(x=x, case='zero width joins the last cell'):
                s = self.screen(cols=5)
                pb(s, 'abcde\x1b[8h' + x)
                self.ae(self.records(s, 0, 1), [LTR, NONE])
            with self.subTest(x=x, case='zero width at the line start is dropped'):
                s = self.screen(cols=5)
                pb(s, 'abc\r\x1b[8h' + x)
                self.ae(self.records(s, 0), [NONE])
        with self.subTest(x='wider than the line'):
            s = self.screen(cols=1)
            pb(s, 'a\r\x1b[8h\u4e16')
            self.ae(self.records(s, 0), [NONE])
        with self.subTest(x='BS to the previous line'):
            s = self.screen(cols=5)
            pb(s, 'abc\r\nxyz\r\x1b[8h\x08Q')
            self.ae(self.records(s, 1), [NONE])
        with self.subTest(x='charset maps a control to a glyph'):
            s = self.screen(cols=5)
            pb(s, '\x1b(U\x1b[8h\x01')
            self.ae(self.records(s, 0), [LTR])
        with self.subTest(x='wide fits'):
            s = self.screen(cols=5)
            pb(s, 'abc\x1b[8h\u4e16')
            self.ae(self.records(s, 0, 1), [LTR, NONE])

    def test_bidi_control_parse(self):
        # (setup, SCP sequence, direction after)
        for setup, seq, rtl in (
            ('', '2 k', True),
            ('', '2;5 k', True),
            ('2 k', '1 k', False),
            ('2 k', '0 k', False),
            ('2 k', ' k', False),
            ('2 k', '1;0 k', False),
            ('2 k', '1;2;3 k', True),
            ('2 k', '3 k', True),
            ('2 k', '?1 k', True),
        ):
            with self.subTest(setup=setup, seq=seq):
                s = self.screen()
                pb(s, '\x1b[8h' + (f'\x1b[{setup}' if setup else '') + f'\x1b[{seq}x')
                self.ae(s.test_bidi_state()[2], rtl)
                self.ae(self.records(s, 0), [(1, rtl, False)])
        s = self.screen()
        self.ae(self.reply(s, '\x1b[4$p'), b'\x1b[4;2$y')
        pb(s, '\x1b[4;8h')
        self.ae(self.reply(s, '\x1b[4$p'), b'\x1b[4;1$y')
        self.assertTrue(s.test_bidi_state()[0])
        pb(s, '\x1b[4;8l')
        self.ae(self.reply(s, '\x1b[4$p'), b'\x1b[4;2$y')
        self.assertFalse(s.test_bidi_state()[0])
        self.ae(self.reply(s, '\x1b[8$p'), b'\x1b[8;2$y')
        self.ae(self.reply(s, '\x1b[?2501$p'), b'\x1b[?2501;2$y')
        for seq, bdsm, auto in (('8h', 1, 2), ('?2501h', 1, 1), ('8l', 2, 1), ('?2501l', 2, 2)):
            pb(s, f'\x1b[{seq}')
            self.ae(self.reply(s, '\x1b[8$p'), f'\x1b[8;{bdsm}$y'.encode(), seq)
            self.ae(self.reply(s, '\x1b[?2501$p'), f'\x1b[?2501;{auto}$y'.encode(), seq)
        s = self.screen(force_ltr=True)
        for seq in ('8h', '8l', '?2501h', '?2501l'):
            pb(s, f'\x1b[{seq}')
            self.ae(self.reply(s, '\x1b[8$p'), b'\x1b[8;4$y', seq)
            self.ae(self.reply(s, '\x1b[?2501$p'), b'\x1b[?2501;4$y', seq)
        s = self.screen()
        d = Dump()
        parse_bytes(s, b'\x1b[2 k', d)
        self.ae(list(d), [('screen_set_bidi_direction', 2)])
        self.assertTrue(s.test_bidi_state()[2])

    def test_bidi_control_change_marks_dirty(self):
        s = self.screen()
        pb(s, 'a')
        s.reset_dirty()
        dirty = s.linebuf.dirty_lines()
        seqs = ('\x1b[8h', '\x1b[8l', '\x1b[0 k', '\x1b[1 k', '\x1b[2 k', '\x1b[?2501h', '\x1b[?2501l')
        for combo in product(seqs, repeat=3):
            pb(s, ''.join(combo))
            self.assertFalse(s.is_dirty, combo)
            self.ae(s.linebuf.dirty_lines(), dirty, combo)
        s = self.screen()
        pb(s, '\x1b[8ha\r\nb\r\x1b[K')
        s.reset_dirty()
        dirty = s.linebuf.dirty_lines()
        pb(s, '\x1b[2 k')
        self.ae(self.records(s, 1), [RTL])
        self.assertFalse(s.is_dirty)
        self.ae(s.linebuf.dirty_lines(), dirty)

    def test_bidi_control_lifetime(self):
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?2501h\x1b[2 k')
        pb(s, '\x1b[?1049h')
        self.ae(s.test_bidi_state()[:3], POWER_ON)
        pb(s, '\x1b[8h')
        self.ae(s.test_bidi_state()[:3], (True, False, False))
        pb(s, '\x1b[?1049l')
        self.ae(s.test_bidi_state()[:3], (True, True, True))
        for reset in ('\x1bc', '\x1b[!p'):
            for in_alt in (False, True):
                with self.subTest(reset=reset, in_alt=in_alt):
                    s = self.screen()
                    pb(s, '\x1b[8h\x1b[?2501h\x1b[2 k\x1b[?1049h\x1b[8h\x1b[2 k')
                    if not in_alt:
                        pb(s, '\x1b[?1049l')
                    pb(s, reset)
                    self.ae(s.test_bidi_state()[:3], POWER_ON)
                    pb(s, '\x1b[?1049l')
                    self.ae(s.test_bidi_state()[:3], POWER_ON)
                    pb(s, '\x1b[?1049h')
                    self.ae(s.test_bidi_state()[:3], POWER_ON)
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?2501h\x1b[?s\x1b[8l\x1b[?2501l\x1b[?r')
        self.ae(s.test_bidi_state()[:2], (True, True))
        pb(s, '\x1b[?2501s\x1b[?2501l\x1b[?2501r')
        self.ae(s.test_bidi_state()[:2], (True, True))
        pb(s, '\x1b[8l\x1b[?2501l\x1b[?r')
        self.ae(s.test_bidi_state()[:2], (True, True))
        pb(s, '\x1b[2 k\x1b7\x1b[8l\x1b[?2501l\x1b[1 k\x1b8')  # DECSC/DECRC save none of it
        self.ae(s.test_bidi_state()[:3], POWER_ON)
        pb(s, '\x1b[8h\x1b[?2501h\x1b[?s\x1b[?1049h')  # one terminal-global copy
        self.ae(s.test_bidi_state()[:3], POWER_ON)
        pb(s, '\x1b[?r')
        self.ae(s.test_bidi_state()[:3], (True, True, False))

    def test_multicell_stamp(self):
        s = self.screen()
        pb(s, '\x1b[8h' + CUP(2))
        draw_multicell(s, 'a', scale=2)
        self.ae(self.records(s, 0, 1, 2, 3), [NONE, LTR, LTR, NONE])
        s = self.screen()
        draw_multicell(s, 'a', scale=2)
        self.ae(self.records(s, 0, 1), [NONE, NONE])

    def test_alt_screen_stash_nesting(self):
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?2501h\x1b[2 k\x1b[?1049h\x1b[?47h\x1b[?1047h')
        self.ae(s.test_bidi_state()[:3], POWER_ON)
        pb(s, '\x1b[8h')
        pb(s, '\x1b[?47l')
        self.ae(s.test_bidi_state()[:3], (True, True, True))
        pb(s, '\x1b[?1047h\x1b[?1049h\x1b[?1049l')
        self.ae(s.test_bidi_state()[:3], (True, True, True))

    def test_record_written_once(self):
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka')
        pb(s, '\x1b[1 k\x1b[?2501h')
        self.ae(self.records(s, 0), [RTL])
        pb(s, CUP(1) + 'b')
        self.ae(self.records(s, 0), [RTL])

    def test_record_lifetime_el_ed_il(self):
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\x1b[1 k' + CUP(1) + '\x1b[2K')
        self.ae(self.records(s, 0), [RTL])
        pb(s, '\x1b[2J' + CUP(1) + 'b')
        self.ae(self.records(s, 0), [LTR])
        pb(s, '\x1b[2 k' + CUP(1) + '\x1b[2K\x1b[2 k' + CUP(1) + 'c')
        self.ae(self.records(s, 0), [LTR])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\r\x1b[2K\x1b[1 kb')  # EL leaves no text, so the change re-derives
        self.ae(self.records(s, 0), [LTR])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\r\x1b[K\x1b[8l')
        self.ae(self.records(s, 0), [NONE])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\x1b[1 k' + CUP(1) + '\x1b[L' + CUP(1) + 'b')
        self.ae(self.records(s, 0, 1), [LTR, RTL])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\x1b[1 k' + CUP(1) + '\x1b[M')  # DL pulls a fresh line up
        self.ae(self.records(s, 0), [NONE])

    def test_stamped_flag_per_buffer(self):
        s = self.screen()
        self.ae(s.test_bidi_state()[3:], (False, False))
        pb(s, '\x1b[8ha')
        self.ae(s.test_bidi_state()[3:], (True, False))
        s = self.screen()
        pb(s, '\x1b[?1049h\x1b[8ha')
        self.ae(s.test_bidi_state()[3:], (False, True))
        pb(s, '\x1b[?1049l\x1b[8ha')
        self.ae(s.test_bidi_state()[3:], (True, True))

    def test_reset_flags(self):
        s = self.screen()
        pb(s, '\x1b[8ha\x1b[?1049h\x1b[8hb\x1bc')
        self.ae(s.test_bidi_state(), (False, False, False, False, False))
        pb(s, '\x1b[?1049l')
        self.ae(self.records(s, 0), [NONE])
        s = self.screen()
        pb(s, '\x1b[8ha\r\nx\r\x1b[K\x1b[?1049h\x1b[8hb\r\ny\r\x1b[K')
        self.ae(self.records(s, 0, 1), [LTR, LTR])
        pb(s, '\x1b[!p')
        self.ae(s.test_bidi_state(), (False, False, False, True, True))
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        pb(s, '\x1b[?1049l')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        self.ae(s.test_bidi_state()[3:], (True, True))
        s = self.screen()  # DECSTR in main re-derives main's erased lines itself
        pb(s, '\x1b[8ha\r\nx\r\x1b[K')
        self.ae(self.records(s, 0, 1), [LTR, LTR])
        pb(s, '\x1b[!p')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        # resize, with and without rewrapping
        for lines, cols in ((5, 10), (8, 10), (5, 6), (3, 12)):
            s = self.screen()
            pb(s, '\x1b[8ha\x1b[?1049h\x1b[8hb')
            s.resize(lines, cols)
            self.ae(s.test_bidi_state()[3:], (True, True), (lines, cols))
        for lines, cols in ((5, 6), (8, 10)):
            s = self.screen()
            pb(s, '\x1b[8h\x1b[2 kabc\r\n')
            s.resize(lines, cols)
            self.ae(self.records(s, 0), [RTL], (lines, cols))

    def test_overlay_does_not_stamp(self):
        s = self.screen()
        pb(s, '\x1b[8h')
        s.test_draw_overlay_line('xy', 0, 2)
        self.ae(self.records(s, 2), [NONE])
        self.ae(s.test_bidi_state(), (True, False, False, False, False))
        pb(s, CUP(3) + 'z')
        self.ae(self.records(s, 2), [LTR])

    def test_force_ltr_still_stamps(self):
        s = self.screen(force_ltr=True)
        pb(s, '\x1b[8h\x1b[2 ka')
        self.ae(self.records(s, 0), [RTL])
        self.ae(s.test_bidi_state()[3], True)

    def test_provisional_records(self):
        s = self.screen()
        pb(s, '\x1b[8ha\r\n\x1b[2 kb')
        self.ae(self.records(s, 0, 1), [LTR, RTL])
        s = self.screen()
        pb(s, '\x1b[8ha\r\nb\r\x1b[K\r\nc\r\x1b[K\x1b[?2501h')
        self.ae(self.records(s, 0, 1, 2), [LTR, AUTO, AUTO])
        s = self.screen()
        pb(s, '\x1b[8ha\r\n\r\n\x1b[?2501h')
        self.ae(self.records(s, 0, 1, 2), [LTR, NONE, NONE])
        s = self.screen()
        pb(s, '\x1b[8h \x1b[2 k')  # a written space is text
        self.ae(self.records(s, 0), [LTR])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?2501h\x1b[?s\x1b[?2501la\r\nb\r\x1b[K')
        self.ae(self.records(s, 0, 1), [LTR, LTR])
        pb(s, '\x1b[?r')
        self.ae(self.records(s, 0, 1), [LTR, AUTO])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?1049h\x1b[8ha\r\nb\r\x1b[K')
        self.ae(self.records(s, 0, 1), [LTR, LTR])
        pb(s, '\x1b[?1049l')
        pb(s, '\x1b[?47h')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        s = self.screen()
        pb(s, '\x1b[8ha\r\nb\r\x1b[K\x1b[8l')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\x1b[1 k' + CUP(1) + '\x1b[2K\x1b[1 k')  # a no-op set re-derives nothing
        self.ae(self.records(s, 0), [RTL])

    def test_joining_char_stamps_the_row_it_joins(self):
        for first, joiner in (('\U0001f44d', '\U0001f3fd'), ('\U0001f1fa', '\U0001f1f8')):
            with self.subTest(first=first):
                s = self.screen(cols=5)
                pb(s, 'abc' + first)
                pb(s, '\x1b[8h' + joiner)
                self.ae(self.records(s, 0, 1), [LTR, NONE])
        with self.subTest('zero width after wrap'):
            s = self.screen(cols=5)
            pb(s, 'abcdef\r')
            pb(s, '\x1b[8h\u0301')
            self.ae(self.records(s, 0, 1), [LTR, NONE])
        with self.subTest('zero width non-joining after wrap'):
            s = self.screen(cols=5)
            pb(s, 'abcdef\r')
            pb(s, '\x1b[8h\u200b')
            self.ae(self.records(s, 0, 1), [LTR, NONE])

    def test_multicell_move_stamps_the_row_written(self):
        s = self.screen(cols=2)
        draw_multicell(s, 'a', scale=2)
        pb(s, CUP(2) + '\x1b[8hX')
        self.ae(self.records(s, 0, 1, 2), [NONE, NONE, LTR])

    def test_shift_out_in_one_draw_call(self):
        s = self.screen()
        pb(s, '\x1b)U\x1b[8h\x0e\x01')
        self.ae(self.records(s, 0), [LTR])
        s = self.screen()
        pb(s, '\x1b)U\x1b[8h\x01')
        self.ae(self.records(s, 0), [NONE])

    def test_arrival_does_not_stamp(self):
        with self.subTest('LF'):
            s = self.screen()
            pb(s, '\r\nxyz' + CUP(1))
            pb(s, '\x1b[8h\n')
            self.ae(self.records(s, 1), [NONE])
        with self.subTest('BS'):
            s = self.screen(cols=5)
            pb(s, 'abc\r\nxyz\r')
            pb(s, '\x1b[8h\x08')
            self.ae(self.records(s, 0), [NONE])
        with self.subTest('BS then write'):
            s = self.screen(cols=5)
            pb(s, 'abc\r\nxyz\r')
            pb(s, '\x1b[8h\x08Q')
            self.ae(self.records(s, 0), [LTR])
        with self.subTest('HT wrap'):
            s = self.screen(cols=5)
            pb(s, '\r\nxyz' + CUP(1) + 'abcde')
            pb(s, '\x1b[8h\t')
            self.ae(self.records(s, 0, 1), [NONE, NONE])

    def test_widened_char_move_stamps_both_rows(self):
        s = self.screen(cols=5)
        pb(s, 'abcd\u263a')
        pb(s, '\x1b[8h\ufe0f')
        self.ae(self.records(s, 0, 1), [LTR, LTR])

    def test_refused_combining_char_does_not_stamp(self):
        s = self.screen(cols=10)
        pb(s, 'a' + '\u0301' * 23)
        pb(s, '\x1b[8h\u0301')
        self.ae(self.records(s, 0), [NONE])

    def test_draw_follows_mode(self):
        def stamped(s, setup, row=0):
            pb(s, setup + CUP(row + 1) + 'a')
            return self.records(s, row)[0][0] != 0

        s = self.screen()
        self.assertFalse(stamped(s, ''))
        self.assertTrue(stamped(s, '\x1b[8h', 1))
        self.assertFalse(stamped(s, '\x1b[8l', 2))
        self.assertTrue(stamped(s, '\x1b[8h', 3))
        pb(s, '\x1b[?1049h')
        self.assertFalse(stamped(s, '', 0))
        pb(s, '\x1b[?1049l')
        self.assertTrue(stamped(s, '', 4))
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?s\x1b[8l')
        self.assertFalse(stamped(s, ''))
        pb(s, '\x1b[?r')
        self.assertTrue(stamped(s, '', 1))
        pb(s, '\x1b[?s\x1b[8l\x1b[?r')
        self.assertTrue(stamped(s, '', 2))
        for reset in ('\x1bc', '\x1b[!p'):
            with self.subTest(reset=reset):
                s = self.screen()
                self.assertTrue(stamped(s, '\x1b[8h'))
                self.assertFalse(stamped(s, reset, 1))
        # The implicit draw loop run with the mode off writes record 0, which
        # line records cannot tell apart, but it sets the buffer's stamped
        # flag, so check the flags on fresh screens.
        for reset in ('\x1bc', '\x1b[!p'):
            with self.subTest('flags after', reset=reset):
                s = self.screen()
                pb(s, '\x1b[8h' + reset + 'a')
                self.ae(s.test_bidi_state()[3:], (False, False))
        with self.subTest('flags in alt'):
            s = self.screen()
            pb(s, '\x1b[8h\x1b[?1049ha')
            self.assertFalse(s.test_bidi_state()[4])
        with self.subTest('alt toggles the mode on and off, main restores it'):
            s = self.screen()
            pb(s, '\x1b[8h\x1b[?1049h\x1b[8h\x1b[8l\x1b[?1049l')
            self.assertTrue(stamped(s, '', 1))

    def draw_marked(self, s, mark, text, split):
        if split:
            pb(s, mark)
            pb(s, text)
        else:
            pb(s, mark + text)

    def test_leading_mark_converts_record(self):
        for split in (False, True):
            with self.subTest(split=split):
                for mark, want in ((LRM, LTR), (RLM, RTL), (ALM, RTL)):
                    for text in ('abc', ''):
                        s = self.screen()
                        pb(s, IA)
                        self.draw_marked(s, mark, text, split)
                        self.ae(self.records(s, 0), [want], (mark, text))
                s = self.screen()
                pb(s, IA + '\x1b[2 k')
                self.draw_marked(s, LRM, 'abc', split)
                self.ae(self.records(s, 0), [LTR])

    def test_leading_mark_mid_line(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.screen()
                pb(s, IA)
                self.draw_marked(s, 'ab', RLM + 'c', split)
                self.ae(self.records(s, 0), [AUTO])
                s = self.screen()
                pb(s, IA + 'abc')
                self.draw_marked(s, '\x1b[1;3H', RLM, split)
                self.ae(self.records(s, 0), [AUTO])
                s = self.screen()
                pb(s, IA + '\x1b[1;3H')
                self.draw_marked(s, RLM, 'x', split)
                self.ae(self.records(s, 0), [AUTO])

    def cell_texts(self, s, n):
        return [s.line(0)[x] for x in range(n)]

    def test_leading_mark_cell_splitting(self):
        for split in (False, True):
            with self.subTest(split=split):
                for mark, want in ((LRM, LTR), (RLM, RTL), (ALM, RTL)):
                    s = self.screen()
                    pb(s, IA)
                    self.draw_marked(s, mark, 'abc', split)
                    self.ae(self.cell_texts(s, 4), ['a', 'b', 'c', '\0'], mark)
                    self.ae(self.records(s, 0), [want], mark)
                    s = self.screen()
                    pb(s, IA + 'ab')
                    self.draw_marked(s, mark, 'c', split)
                    self.ae(self.cell_texts(s, 4), ['a', 'b' + mark, 'c', '\0'], mark)
                    self.ae(self.records(s, 0), [AUTO], mark)
                for mark in ('\u200c', '\u200d', '\u202a', '\u202b', '\u202c', '\u202d', '\u202e', '\u2066', '\u2067', '\u2068', '\u2069'):
                    s = self.screen()
                    pb(s, IA)
                    self.draw_marked(s, mark, 'abc', split)
                    # an isolate control with no previous cell is stored in the cell, before the char written next
                    first = mark + 'a' if mark in ISOLATE_CONTROLS else 'a'
                    self.ae(self.cell_texts(s, 4), [first, 'b', 'c', '\0'], mark)
                    self.ae(self.records(s, 0), [AUTO], mark)
                    s = self.screen()
                    pb(s, IA + 'ab')
                    self.draw_marked(s, mark, 'c', split)
                    self.ae(self.cell_texts(s, 4), ['a', 'b' + mark, 'c', '\0'], mark)
                    self.ae(self.records(s, 0), [AUTO], mark)

    def test_leading_mark_needs_auto(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.screen()
                pb(s, '\x1b[8h')
                self.draw_marked(s, RLM, 'abc', split)
                self.ae(self.records(s, 0), [LTR])
                s = self.screen()
                pb(s, '\x1b[8habc\x1b[?2501h\r')
                self.draw_marked(s, RLM, 'x', split)
                self.ae(self.records(s, 0), [LTR])
                s = self.screen()
                pb(s, '\x1b[8h\x1b[2 k')
                self.draw_marked(s, LRM, 'abc', split)
                self.ae(self.records(s, 0), [RTL])

    def test_leading_mark_first_wins(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.screen()
                pb(s, IA)
                self.draw_marked(s, RLM, LRM + 'abc', split)
                self.ae(self.records(s, 0), [RTL])
                pb(s, '\r')
                self.draw_marked(s, LRM, '', split)
                self.ae(self.records(s, 0), [RTL])

    def test_leading_mark_other_zero_width(self):
        for ch in ('\u200b', '\u200c', '\u200d', '\u202a', '\u202b', '\u202c', '\u2066', '\u2067', '\u2068', '\u2069'):
            for split in (False, True):
                with self.subTest(ch=ch, split=split):
                    s = self.screen()
                    pb(s, IA)
                    self.draw_marked(s, ch, 'abc', split)
                    self.ae(self.records(s, 0), [AUTO])
                    s = self.screen()
                    pb(s, IA + 'abc\r')
                    pb(s, ch)
                    self.ae(self.records(s, 0), [AUTO])

    def test_leading_mark_explicit_mode(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.screen()
                pb(s, IA + 'abc')
                self.ae(self.records(s, 0), [AUTO])
                pb(s, '\x1b[8l\r')
                self.draw_marked(s, RLM, 'x', split)
                self.ae(self.records(s, 0), [AUTO])

    def test_leading_mark_lifetime(self):
        for split in (False, True):
            with self.subTest(split=split):

                def marked():
                    s = self.screen()
                    pb(s, IA)
                    self.draw_marked(s, RLM, 'abc', split)
                    self.ae(self.records(s, 0), [RTL])
                    return s

                for el in ('0K', '1K', '2K'):
                    s = marked()
                    pb(s, CUP(1) + '\x1b[' + el)
                    self.ae(self.records(s, 0), [RTL], el)
                    pb(s, CUP(1) + 'xyz')
                    self.ae(self.records(s, 0), [RTL], el)
                # ED 0 at the cursor row erases it as EL does, so it keeps the record; ED 1 and ED 2 clear the row's attributes
                for ed, want in (('J', RTL), ('1J', AUTO), ('2J', AUTO)):
                    s = marked()
                    pb(s, CUP(1) + '\x1b[' + ed + CUP(1) + 'xyz')
                    self.ae(self.records(s, 0), [want], ed)
                s = marked()
                pb(s, CUP(1) + '\x1b[L' + CUP(1) + 'xyz')
                self.ae(self.records(s, 0, 1), [AUTO, RTL])
                s = marked()
                pb(s, CUP(1) + '\x1b[M')
                self.ae(self.records(s, 0), [NONE])
                s = marked()
                pb(s, '\x1bc')
                self.ae(self.records(s, 0), [NONE])
                s = self.screen(lines=5)
                pb(s, IA + CUP(5))
                self.draw_marked(s, RLM, 'abc', split)
                pb(s, '\n')
                self.ae(self.records(s, 3, 4), [RTL, NONE])

    def test_leading_mark_provisional_limit(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.screen()
                pb(s, IA)
                self.draw_marked(s, RLM, '', split)
                self.ae(self.records(s, 0), [RTL])
                pb(s, '\x1b[2 k')  # a text-less row re-derives, losing the fixed direction
                self.ae(self.records(s, 0), [(1, True, True)])

    def test_leading_mark_continuation_row(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.screen(cols=5)
                pb(s, IA + 'abcdefg')
                self.ae(self.records(s, 0, 1), [AUTO, AUTO])
                self.draw_marked(s, CUP(2), RLM, split)
                self.ae(self.records(s, 0, 1), [AUTO, AUTO])

    def test_leading_mark_force_ltr(self):
        for split in (False, True):
            with self.subTest(split=split):
                s = self.screen(force_ltr=True)
                pb(s, IA)
                self.draw_marked(s, RLM, 'abc', split)
                self.ae(self.records(s, 0), [RTL])

    def run_leading_controls_seed(self, pool, opset, seed, steps=200):
        # Leading controls are written only at column 0 of a row carrying a label, each label with its own codeword, and ops that
        # move, erase, scroll, resize and overdraw rows, and toggle modes, run between them. Opset 'A' keeps every cell in its
        # column, so the controls stay in column 0; opset 'B' adds ICH, DCH and insert mode, which move the cells of a row
        controls = LEADING_CONTROL_POOLS[pool]
        rnd = random.Random(f'{pool}:{opset}:{seed}')
        s = self.create_screen(cols=rnd.randint(10, 14), lines=rnd.randint(2, 6), scrollback=20)
        labels = set()
        log = []
        irm = False
        next_label = 0
        done = object()  # an op that did its work itself

        def where():
            return f'pool {pool} set {opset} seed {seed} step {step}, ops (latest last): {log!r}'

        def label_span(y):
            texts = [c['text'] for c in s.cpu_cells(y)]
            for x in range(s.columns - 3):
                if re.fullmatch(r'L\d{3}', ''.join(texts[x : x + 4])):
                    return x, x + 4
            return None

        def text_of(y):
            return ''.join(c['text'] for c in s.cpu_cells(y))

        def crowded(y):
            # a multiline or wide cell near the start of a row moves a write or overlay right, onto the label, and DCH can
            # bring the label itself within reach of the write
            for yy in (y, y + 1):
                if yy >= s.lines:
                    continue
                span = label_span(yy)
                if span and span[0] < 3:
                    return True
                for x in range(5):
                    cell = s.cpu_cells(yy, x)
                    mcd = cell['mcd']
                    if cell['text'] == '\u4e2d' or (mcd and (mcd['scale'] > 1 or mcd['width'] > 1)):
                        return True
            return False

        def fits(y, extra):
            # the content of the row, label included, stays within the narrowest width after being moved right
            span = label_span(y)
            return not span or span[1] + extra <= 10

        def write_at_row(write, width=1):
            y = rnd.randrange(s.lines)
            if crowded(y) or (irm and not fits(y, width)):
                return None
            return CUP(y + 1) + write()

        def label_row():
            nonlocal next_label
            free = [y for y in range(s.lines) if not re.search(f'L\\d{{3}}|[{LEADING_CONTROLS}]', text_of(y))]
            if not free:
                return None
            y = rnd.choice(free)
            next_label += 1
            assert next_label < 1000, 'labels have three digits'
            return f'{CUP(y + 1)}\x1b[6GL{next_label:03d}'

        def control_row():
            # in insert mode the store may insert a cell, moving the label right
            rows = [y for y in range(s.lines) if label_span(y) and (not irm or fits(y, 1))]
            if not rows:
                return label_row()
            y = rnd.choice(rows)
            label = re.search(r'L\d{3}', text_of(y)).group()
            labels.add(label)
            cw = leading_controls_codeword(label, controls)
            before = s.cpu_cells(y, 0)
            pb(s, CUP(y + 1) + cw)
            log.append(f'controls for {label} at row {y}')
            stored = s.cpu_cells(y, 0)['text']
            if before['mcd'] and before['y'] > 0:
                # the lower row of a multiline multicell: no cell there can take a control without moving the cursor, so it is dropped
                after = s.cpu_cells(y, 0)
                if after != before or any(ch in LEADING_CONTROLS for ch in stored):
                    self.fail(f'{where()}: cell (0, {y}) under a multiline multicell became {after!r} from {before!r} after {cw!r}')
            elif not stored.startswith(cw):
                self.fail(f'{where()}: cell (0, {y}) holds {stored!r} right after {cw!r} was written there')
            return done

        def row():
            return rnd.randint(1, s.lines)

        def overlay():
            if irm or any(crowded(y) for y in range(s.lines)):
                return None
            y = rnd.randrange(s.lines)
            text = rnd.choice(('z', '\nz', '\n\nz', 'z\nz', 'xy\n', '\n\n\n\nz'))
            s.test_draw_overlay_line(text, 0, y)
            s.test_update_overlay_text('')
            log.append(f'overlay {text!r} at row {y}')
            return done

        def resize():
            # opset A never widens, so no reflow moves a cell
            lines, cols = rnd.randint(2, 6), rnd.randint(10, 14 if opset == 'B' else s.columns)
            s.resize(lines, cols)
            log.append(f'resize {lines}x{cols}')
            return done

        def toggle():
            nonlocal irm
            modes = ['\x1b[8h', '\x1b[8l', '\x1b[?7h', '\x1b[?7l', '\x1b[?1049h', '\x1b[?1049l', '\x1b[?2501h', '\x1b[?2501l']
            if opset == 'B':
                modes += ['\x1b[4h', '\x1b[4l']
            mode = rnd.choice(modes)
            if mode in ('\x1b[4h', '\x1b[4l'):
                irm = mode == '\x1b[4h'
            return mode

        def insert_char():
            y = rnd.randrange(s.lines)
            ch = rnd.choice(('q', '\u4e2d'))
            if not fits(y, 1 + (ch == '\u4e2d')):
                return None
            return f'{CUP(y + 1)}\x1b[4h{ch}' + ('' if irm else '\x1b[4l')

        def ich():
            y = rnd.randrange(s.lines)
            return f'{CUP(y + 1)}\x1b[@' if fits(y, 1) else None

        def dch():
            y = rnd.randrange(s.lines)
            span = label_span(y)
            return f'{CUP(y + 1)}\x1b[P' if not span or span[0] > 0 else None

        # (weight, op); an op gives bytes to parse, done when it did its work itself, or None to skip the step
        ops = [
            (2, lambda: f'\x1b[{row()};{rnd.randint(1, s.columns)}H'),
            (3, lambda: rnd.choice(('\n', '\x1bD', '\x1bM', '\x1bE'))),
            (2, lambda: rnd.choice(('\x1b[r', f'\x1b[{rnd.randint(1, 2)};{rnd.randint(3, 6)}r'))),
            (3, lambda: f'\x1b[{rnd.randint(1, 3)}{rnd.choice("STLM")}'),
            (3, lambda: CUP(row()) + rnd.choice(('\x1b[2K', '\x1b[J', '\x1b[1J', '\x1b[2J', f'\x1b[{rnd.randint(1, 12)}X'))),
            (2, overlay),
            (2, lambda: None if irm else write_at_row(lambda: f'\x1b]66;w=1:s={rnd.randint(1, 2)};x\x07')),
            (2, lambda: None if irm else write_at_row(lambda: '\x1b[2G\x1b]66;s=2;a\x07')),
            (3, lambda: write_at_row(lambda: rnd.choice(('q', '\u4e2d', 'e\u0301')), 2)),
            (3, label_row),
            (5, control_row),
            (1, resize),
            (3, toggle),
        ]
        if opset == 'B':
            ops += [(2, ich), (2, dch), (3, insert_char)]
        weights = [w for w, _ in ops]
        for y in range(s.lines):
            pb(s, f'{CUP(y + 1)}\x1b[6GL{y + 1:03d}')
        next_label = s.lines
        for step in range(steps):
            result = rnd.choices(ops, weights)[0][1]()
            if result is None:
                continue
            if result is not done:
                log.append(result)
                pb(s, result)
            rows = [(f'row {y}', [c['text'] for c in s.cpu_cells(y)]) for y in range(s.lines)]
            if not s.is_using_alternate_linebuf():
                rows += [(f'history {i}', [c['text'] for c in s.cpu_cells(-(i + 1))]) for i in range(s.historybuf.count)]
            faults = leading_control_faults(rows, labels, controls, strict=opset == 'A')
            if faults:
                self.fail(f'{where()}: ' + '; '.join(faults))

    def leading_controls_randomized(self, pool):
        for opset in ('A', 'B'):
            for seed in range(1, 51):
                with self.subTest(opset=opset, seed=seed):
                    self.run_leading_controls_seed(pool, opset, seed)

    def test_leading_controls_randomized_isolates(self):
        self.leading_controls_randomized('isolates')

    def test_leading_controls_randomized_all(self):
        self.leading_controls_randomized('all')
