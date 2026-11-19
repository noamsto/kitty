#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import random
import re
from itertools import product

from kitty.window import as_text

from .base import BaseTest, draw_multicell, parse_bytes

NONE, LTR, RTL, AUTO = (0, False, False), (1, False, False), (1, True, False), (1, False, True)
POWER_ON = (False, False, False)
LRM, RLM = '\u200e', '\u200f'
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

    def cell_texts(self, s, y=0, n=4):
        return [s.cpu_cells(y, x)['text'] for x in range(n)]

    def test_leading_mark_goes_to_the_next_cell(self):
        # An LRM or RLM drawn with no previous cell is kept first in the text of the next cell drawn there
        for mark, bdsm in product((LRM, RLM), ('', '\x1b[8h')):
            for parts in ((mark + 'abc',), (mark, 'abc'), (mark, '\x1b[1m', 'abc'), (mark + '\r', 'abc')):
                with self.subTest(mark=mark, bdsm=bdsm, parts=parts):
                    s = self.screen()
                    pb(s, bdsm)
                    for p in parts:
                        pb(s, p)
                    self.ae(self.cell_texts(s), [mark + 'a', 'b', 'c', ''])
                    self.ae(s.cursor.x, 3)
                    self.ae(str(s.line(0)), mark + 'abc')
                    self.ae(self.records(s, 0), [LTR if bdsm else NONE])
                    self.ae(s.test_bidi_state()[3], bool(bdsm))
        # on a line the row above wraps into the mark has a previous cell
        s = self.screen(cols=3)
        pb(s, 'abc' + RLM + 'd')
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 2)), (['a', 'b', 'c' + RLM], ['d', '']))
        # a mark after a cell attaches to it as before
        s = self.screen()
        pb(s, 'ab' + RLM + 'c')
        self.ae(self.cell_texts(s), ['a', 'b' + RLM, 'c', ''])

    def test_leading_marks_in_order(self):
        s = self.screen()
        pb(s, LRM + RLM + 'a')
        self.ae(self.cell_texts(s, n=2), [LRM + RLM + 'a', ''])
        # every mark is kept, in order, across draw calls
        s = self.screen()
        pb(s, LRM + RLM + LRM)
        pb(s, RLM + LRM + 'a')
        self.ae(self.cell_texts(s, n=2), [LRM + RLM + LRM + RLM + LRM + 'a', ''])

    def test_leading_marks_cap(self):
        # a marks-only cell holds one fewer than a cell can, so that a char still fits after them
        cap = 23
        marks = (LRM + RLM) * 12
        s = self.screen()
        pb(s, marks)
        self.ae(self.cell_texts(s, n=2), [marks[:cap], ''])
        pb(s, 'a')
        self.ae(self.cell_texts(s, n=2), [marks[:cap] + 'a', ''])
        self.ae(str(s.line(0)), marks[:cap] + 'a')

    def test_other_zero_width_chars_at_line_start(self):
        # still discarded, ALM included, and they leave a held mark alone. The isolates are leading controls, kept as marks are
        for ch in '‍‌؜‪‮':
            with self.subTest(hex(ord(ch))):
                s = self.screen()
                pb(s, ch + 'a')
                self.ae(self.cell_texts(s, n=2), ['a', ''])
                s = self.screen()
                pb(s, LRM + ch + 'a')
                self.ae(self.cell_texts(s, n=2), [LRM + 'a', ''])

    def test_leading_mark_on_a_wide_char(self):
        s = self.screen()
        pb(s, RLM + '中x')
        self.ae((self.cell_texts(s, n=3), str(s.line(0)), s.cursor.x), ([RLM + '中', '', 'x'], RLM + '中x', 3))
        # a combining char added later keeps the mark first
        s = self.screen()
        pb(s, RLM + 'á')
        self.ae(self.cell_texts(s, n=2), [RLM + 'á', ''])
        pb(s, '̂')
        self.ae(self.cell_texts(s, n=2), [RLM + 'á̂', ''])

    def test_leading_mark_lifetime(self):
        # the mark lives in its cell's text
        for seq, row in (
            ('\rx', ['x', 'b', 'c', '']),
            ('\r\x1b[@', ['', RLM + 'a', 'b', 'c']),
            ('\r\x1b[P', ['b', 'c', '', '']),
            ('\x1b[2K', ['', '', '', '']),
            ('\r\x1b[X', ['', 'b', 'c', '']),
        ):
            with self.subTest(seq=seq):
                s = self.screen()
                pb(s, RLM + 'abc')
                pb(s, seq)
                self.ae(self.cell_texts(s), row)
        # scrolling and history keep it
        s = self.create_screen(cols=10, lines=2, scrollback=10)
        pb(s, RLM + 'abc\n\n')
        self.ae(str(s.historybuf.line(0)), RLM + 'abc')

    def test_leading_mark_kept_with_its_cell(self):
        # The mark lives in its cell, so it is kept or dropped as the cell and its line are
        def kept(*parts, lines=5, y=0, cols=10):
            s = self.screen(cols=cols, lines=lines)
            for p in parts:
                if callable(p):
                    p(s)
                else:
                    pb(s, p)
            t = s.cpu_cells(y, 0)['text']
            self.assertIn(t, (RLM + 'a', 'a'), parts)
            return t == RLM + 'a'

        # the same position, after the cursor went away and back, or a CR
        self.assertTrue(kept(RLM, '\x1b[3;4H', '\x1b[1;1H', 'a'))
        self.assertTrue(kept(RLM, '\r\x07a'))
        self.assertTrue(kept(RLM + '\r', 'a'))
        # control chars back to it before the text
        self.assertTrue(kept(RLM, '\x1b[3G', '\ra'))
        self.assertTrue(kept(RLM, '\t\ra'))
        self.assertTrue(kept(RLM, '\x1b[2G', '\ba'))
        # text written elsewhere first does not touch it
        self.assertTrue(kept(RLM, '\x1b[1;3Hb\ra'))
        self.assertTrue(kept(RLM, '\tb\ra'))
        # a line feed leaves it on its line; a line feed on a one line screen clears the line in place
        self.assertFalse(kept(RLM + '\na', y=1))
        self.assertTrue(kept(RLM, '\n\x1bMa'))
        self.assertFalse(kept(RLM + '\na', lines=1))
        self.assertFalse(kept(RLM, '\na', lines=1))
        # a line moved off its row takes it along, however often the region's storage comes round to the same place
        for seq in ('\x1bD\x1bD', '\x1bE\x1bE', '\x1b[S\x1b[S', '\x1b[2S', '\x1b[T\x1b[T', '\x1b[2T', '\x1b[+T\x1b[+T', '\x1b[L\x1b[L', '\x1b[M\x1b[M'):
            with self.subTest(seq=seq):
                self.assertFalse(kept('\x1b[1;2r' + CUP(2) + RLM, seq, CUP(2) + 'a', y=1))
        self.assertFalse(kept('\x1b[1;2r' + RLM, '\x1bM\x1bM', CUP(1) + 'a'))
        self.assertFalse(kept(CUP(5) + RLM, '\x1bD\ra', y=4))
        # scrolling the cursor line to the top moves the line, with its mark, to row 0
        self.assertTrue(kept(CUP(3) + RLM, lambda s: s.scroll_until_cursor_prompt(), '\x1b[1;1Ha'))
        for y in range(5):
            with self.subTest(y=y):
                self.assertFalse(kept('\r\n'.join('x' * 5), CUP(y + 1) + RLM, '\x1b[22J', CUP(y + 1) + 'a', y=y))
        # an OSC 66 cell written over it replaces it
        self.assertFalse(kept(RLM, '\x1b]66;s=1;x\x07', '\ra'))
        # on the lower row of a multiline char there is no cell to take it, and a char moved on along its row past them
        self.assertFalse(kept(CUP(1) + '\x1b]66;s=2;a\x07', CUP(2) + RLM + 'x', CUP(1) + '\x1b[2K' + CUP(2) + 'a', cols=4, lines=2, y=1))
        # reset drops it, the other screen and resizing keep it
        self.assertFalse(kept(RLM, '\x1bc', 'a'))
        self.assertTrue(kept(RLM, '\x1b[?1049h', '\x1b[?1049l', '\x1b[1;1Ha'))
        self.assertTrue(kept(RLM, '\x1b[?1049h', 'a', '\x1b[?1049l', lambda s: self.ae(s.cpu_cells(0, 0)['text'], RLM), '\x1b[1;1Ha'))
        self.assertTrue(kept(RLM, lambda s: s.resize(5, 12), '\x1b[1;1Ha'))
        self.assertTrue(kept(RLM, lambda s: s.resize(3, 6), '\x1b[1;1Ha'))
        # stored with bidi mode off, written into with it on
        self.assertTrue(kept(RLM, '\x1b[8h', 'a'))

    def test_leading_mark_never_leaves_its_line(self):
        # Each of these once carried a mark to another line: row storage that comes back to the same place after
        # OSC 66 cells or pre-edit text scroll the screen. The mark stays on the line it was written on, which
        # is gone by the time the 'a' is written
        def cell_after(*parts, y, cols=4, lines=2):
            s = self.create_screen(cols=cols, lines=lines)
            for p in parts:
                if callable(p):
                    p(s)
                else:
                    pb(s, p)
            return s.cpu_cells(y, 0)['text']

        osc = CUP(1) + '\x1b[2K\x1b]66;s=2;ab\x07', CUP(2) + '\x1b]66;s=1;y\x07'
        with self.subTest('osc 66 cells scroll the line round'):
            self.ae(cell_after(CUP(2) + 'ORIG\r' + RLM, *osc, *osc, CUP(1) + '\x1b[2K' + CUP(2) + 'a', y=1), 'a')
        with self.subTest('osc 66 cell over the lower rows of a multiline char'):
            self.ae(cell_after(CUP(1) + '\x1b]66;s=2;ab\x07', CUP(2) + RLM + 'x', CUP(1) + '\x1b[2Ka', y=0), 'a')
        with self.subTest('pre-edit text scrolls the screen'):
            self.ae(cell_after('A\r\nB', CUP(1), RLM, lambda s: s.test_draw_overlay_line('\n\nz', 0, 1), CUP(1) + 'a', y=0), 'a')

    def test_leading_mark_in_its_cell_across_pre_edit(self):
        for preedit in ('xy', RLM + 'xy'):
            with self.subTest(preedit=preedit):
                s = self.screen()
                pb(s, RLM)
                s.test_draw_overlay_line(preedit, 0, 0)
                # the mark stays in the cell the pre-edit's first char is drawn into
                self.ae(s.cpu_cells(0, 0)['text'], RLM + preedit[:-1])
        # pre-edit text drawn over the lower rows of a multiline char scrolls the screen
        for held_row, after, y in ((1, CUP(1) + '\x1b[2K' + CUP(2) + 'a', 1), (2, CUP(1) + '\x1b[2Ka', 0)):
            with self.subTest(held_row=held_row):
                s = self.screen(cols=4, lines=2)
                pb(s, CUP(1) + '\x1b]66;s=2;ab\x07' + CUP(held_row) + RLM)
                s.test_draw_overlay_line('z', 0, 1)
                pb(s, after)
                self.ae(s.cpu_cells(y, 0)['text'], 'a')

    def test_leading_mark_over_existing_text(self):
        # the mark takes the cell, as writing a char there would, and the cursor does not move
        s = self.screen()
        pb(s, 'abc\r' + RLM)
        self.ae((self.cell_texts(s), s.cursor.x, str(s.line(0))), ([RLM, 'b', 'c', ''], 0, RLM + ' bc'))
        # over a wide char, both its cells go
        s = self.screen()
        pb(s, '中x\r' + RLM)
        self.ae((self.cell_texts(s, n=3), s.cursor.x, str(s.line(0))), ([RLM, '', 'x'], 0, RLM + '  x'))
        # and over a multiline OSC 66 cell, which goes from the rows below it too
        s = self.screen(cols=4, lines=3)
        pb(s, '\x1b]66;s=2;a\x07' + CUP(1) + RLM)
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3), s.cursor.x), ([RLM, '', ''], ['', '', ''], 0))
        # the lower row of a multiline char has no cell it can take: the mark is discarded
        s = self.screen(cols=4, lines=3)
        pb(s, '\x1b]66;s=2;a\x07' + CUP(2) + RLM)
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3)), (['a', '', ''], ['', '', '']))
        # a char written there moves on past the lower rows
        pb(s, 'q')
        self.ae(self.cell_texts(s, 1, 3), ['', '', 'q'])

    def test_leading_mark_cell_is_replaced_by_what_is_written_into_it_later(self):
        # a char written into the cell with a previous cell, or after ICH moved it off column 0, replaces it
        s = self.screen(cols=3)
        pb(s, CUP(2) + RLM + CUP(1) + 'abcd')
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3)), (['a', 'b', 'c'], ['d', '', '']))
        s = self.screen()
        pb(s, RLM + '\r\x1b[@x')
        self.ae(self.cell_texts(s, n=3), ['x', RLM, ''])
        pb(s, 'y')
        self.ae(self.cell_texts(s, n=3), ['x', 'y', ''])
        # DCH removes it with the cell
        s = self.screen()
        pb(s, 'abc\r' + RLM + '\x1b[P')
        self.ae(self.cell_texts(s), ['b', 'c', '', ''])
        # a char written at column 0 into a cell it did not move from is plain
        s = self.screen()
        pb(s, RLM + '\r\x1b[@' + 'x')
        self.ae(s.cpu_cells(0, 0)['text'], 'x')

    def test_leading_mark_with_insert_mode(self):
        # a mark and the char after it behave as that char would, marks first in its cell
        def drawn(text, n=6):
            s = self.screen(cols=8)
            pb(s, 'xbc\r\x1b[4h' + text)
            return self.cell_texts(s, n=n)

        plain = drawn('a')
        self.ae(plain, ['a', 'x', 'b', 'c', '', ''])
        self.ae(drawn(RLM + 'a'), [RLM + 'a'] + plain[1:])
        self.ae(drawn(RLM + LRM + 'a'), [RLM + LRM + 'a'] + plain[1:])
        plain = drawn('\u4e2d')
        self.ae(drawn(RLM + '\u4e2d'), [RLM + plain[0]] + plain[1:])

    def test_leading_mark_cell_takes_no_combining_chars(self):
        # as an empty cell takes none
        s = self.screen()
        pb(s, RLM + '\x1b[C\u0301\x1b[Da')
        self.ae(self.cell_texts(s, n=2), [RLM + 'a', ''])
        # a second mark after the cell is discarded as after an empty cell
        s = self.screen()
        pb(s, RLM + '\x1b[C' + RLM)
        self.ae(self.cell_texts(s, n=2), [RLM, ''])
        pb(s, '\x1b[Da')
        self.ae(self.cell_texts(s, n=2), [RLM + 'a', ''])

    def test_leading_mark_does_not_change_repeat(self):
        # REP repeats the char before the mark
        s = self.screen()
        pb(s, 'x\r' + RLM + '\x1b[2b')
        self.ae(self.cell_texts(s, n=3), [RLM + 'x', 'x', ''])

    def test_leading_mark_on_a_one_column_screen(self):
        # a wide char does not fit there, so the cell keeps its mark
        s = self.create_screen(cols=1, lines=2, options={})
        pb(s, RLM + '中')
        self.ae(self.cell_texts(s, n=1), [RLM])
        pb(s, 'a')
        self.ae(self.cell_texts(s, n=1), [RLM + 'a'])

    def test_leading_mark_with_a_wide_char_past_a_multiline_char(self):
        # The wide char cannot take the cell, as its second cell is a lower row of a multiline char, so it moves on
        # to the next line. The mark stays on its own line and the char's line has none
        s = self.create_screen(cols=3, lines=2, scrollback=5)
        pb(s, '\x1b[1;2H\x1b]66;s=2;a\x07' + CUP(2) + RLM + '中')
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3)), ([RLM, '', ''], ['中', '', '']))
        for i in range(s.historybuf.count):
            self.assertNotIn(RLM, str(s.historybuf.line(i)))
        # with room below it does not scroll
        s = self.create_screen(cols=3, lines=3, scrollback=5)
        pb(s, '\x1b[1;2H\x1b]66;s=2;a\x07' + CUP(2) + RLM + '中')
        self.ae([self.cell_texts(s, y, 3) for y in range(3)], [['', 'a', ''], [RLM, '', ''], ['中', '', '']])
        self.ae(s.historybuf.count, 0)

    def test_leading_mark_with_a_wide_char_over_a_multiline_char_without_wrapping(self):
        # with DECAWM off the mark is still on its own line, as with wrapping, when it took a cell
        s = self.create_screen(cols=3, lines=2, scrollback=5)
        pb(s, '\x1b[?7l\x1b[1;2H\x1b]66;s=2;a\x07' + CUP(2) + RLM + '中')
        self.ae((self.cell_texts(s, 0, 3), self.cell_texts(s, 1, 3)), ([RLM, '', ''], ['中', '', '']))
        self.ae(s.historybuf.count, 1)
        for i in range(s.historybuf.count):
            self.assertNotIn(RLM, str(s.historybuf.line(i)))
        # on two columns the RLM written at the lower row of the s=2 char is dropped on arrival, as that row has no cell
        # it can take, and the wide char moves past that row onto a new line, scrolling the upper row into history
        s = self.create_screen(cols=2, lines=2, scrollback=5)
        pb(s, '\x1b[?7l\x1b[1;1H\x1b]66;s=2;a\x07' + CUP(2) + RLM + '中')
        self.ae([self.cell_texts(s, y, 2) for y in range(2)], [['', ''], ['中', '']])
        self.ae(s.historybuf.count, 1)
        for i in range(s.historybuf.count):
            self.assertNotIn(RLM, str(s.historybuf.line(i)))

    def test_marks_only_cell_in_copied_text(self):
        # an abandoned mark leaves a cell that exports as an empty one does, so the columns after it are kept
        s = self.screen()
        pb(s, RLM + '\x1b[3Gx')
        self.ae(str(s.line(0)), RLM + '  x')
        self.ae(s.line(0).as_ansi(), RLM + '  x')
        self.ae(as_text(s).split('\n')[0], RLM + '  x')
        # with nothing after it there is no space
        s = self.screen()
        pb(s, RLM)
        self.ae(str(s.line(0)), RLM)
        s = self.screen()
        pb(s, RLM + LRM + '\x1b[Cx')
        self.ae(str(s.line(0)), RLM + LRM + ' x')

    def test_marks_only_cell_marker_highlighting(self):
        from kitty.marks import marker_from_regex

        # the highlight stays on the matched text when copied text has the extra space of a marks-only cell
        s = self.screen()
        pb(s, RLM + '\x1b[1;4Hfoo')
        s.set_marker(marker_from_regex('foo', 3))
        self.ae(s.marked_cells(), [(3, 0, 3), (4, 0, 3), (5, 0, 3)])

    def test_marks_only_cell_edge_colors(self):
        # a marks-only cell is a blank one: it does not make a full block the first char of the line
        def edges(text):
            s = self.create_screen()
            pb(s, '\x1b[31;42m' + text)
            return s.line_edge_colors()

        self.assertNotEqual(edges('\u2588'), edges('\u2588\r\x1b[X'))
        self.ae(edges('\u2588\r' + RLM), edges('\u2588\r\x1b[X'))

    def test_marks_only_cell_url_detection(self):
        s = self.screen(cols=30)
        pb(s, RLM + '\x1b[3Ghttp://moo.com x')
        s.detect_url(6, 0)
        self.ae(''.join(s.text_for_marked_url()), 'http://moo.com')
        self.ae(s.current_url_text(), 'http://moo.com')

    def test_leading_marks_on_many_lines_in_one_draw(self):
        s = self.create_screen(cols=10, lines=5, scrollback=20)
        s.draw((RLM + 'x\r\n') * 10000)
        self.ae([str(s.line(y)) for y in range(5)], [RLM + 'x'] * 4 + [''])
        self.ae(s.historybuf.count, 20)
        self.ae({str(s.historybuf.line(i)) for i in range(20)}, {RLM + 'x'})

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

        def marks_only(y):
            text = s.cpu_cells(y, 0)['text']
            return text if text and not text.strip(LEADING_CONTROLS) else ''

        def draw_at_start(y, data, ch):
            # a char drawn at column 0 of a marks-only cell, the cursor already there, keeps the controls before it
            run = marks_only(y)
            pb(s, data)
            log.append(data)
            stored = s.cpu_cells(y, 0)['text']
            if run and not stored.startswith(run + ch[0]):
                self.fail(f'{where()}: cell (0, {y}) holds {stored!r} after {ch!r} was drawn into a cell holding {run!r}')
            return done

        def write_at_row(text, width=1, drawn=False):
            # drawn: text is a char drawn at column 0
            y = rnd.randrange(s.lines)
            if crowded(y) or (irm and not fits(y, width)):
                return None
            data = CUP(y + 1) + text
            return draw_at_start(y, data, text) if drawn else data

        def label_row():
            nonlocal next_label
            free = [y for y in range(s.lines) if not re.search(f'L\\d{{3}}|[{LEADING_CONTROLS}]', text_of(y))]
            if not free:
                return None
            y = rnd.choice(free)
            next_label += 1
            assert next_label < 1000, 'labels have three digits'
            return f'{CUP(y + 1)}\x1b[6GL{next_label:03d}'

        def wraps_into(y):
            if y:
                return s.linebuf.is_continued(y)
            return not s.is_using_alternate_linebuf() and s.historybuf.count > 0 and s.historybuf.endswith_wrap()

        def control_row():
            # a row continuing a wrapped line has a previous cell, which may take the controls instead; in insert mode the store
            # may insert a cell, moving the label right
            rows = [y for y in range(s.lines) if not wraps_into(y) and (span := label_span(y)) and span[0] > 0 and (not irm or fits(y, 1))]
            if not rows:
                return label_row()
            y = rnd.choice(rows)
            label = re.search(r'L\d{3}', text_of(y)).group()
            labels.add(label)
            cw = leading_controls_codeword(label, controls)
            before = s.cpu_cells(y, 0)
            expected = (marks_only(y) + cw)[:MAX_LEADING_CONTROLS]
            pb(s, CUP(y + 1) + cw)
            log.append(f'controls for {label} at row {y}')
            stored = s.cpu_cells(y, 0)['text']
            if before['mcd'] and before['y'] > 0:
                # the lower row of a multiline multicell: no cell there can take a control without moving the cursor, so it is dropped
                after = s.cpu_cells(y, 0)
                if after != before or any(ch in LEADING_CONTROLS for ch in stored):
                    self.fail(f'{where()}: cell (0, {y}) under a multiline multicell became {after!r} from {before!r} after {cw!r}')
            elif stored != expected:
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
            lines, cols = rnd.randint(2, 6), rnd.randint(10, 14)
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
            return draw_at_start(y, f'{CUP(y + 1)}\x1b[4h{ch}' + ('' if irm else '\x1b[4l'), ch)

        def ich():
            y = rnd.randrange(s.lines)
            return f'{CUP(y + 1)}\x1b[@' if fits(y, 1) else None

        def dch():
            y = rnd.randrange(s.lines)
            span = label_span(y)
            return f'{CUP(y + 1)}\x1b[P' if not span or span[0] > 2 else None

        # (weight, op); an op gives bytes to parse, done when it did its work itself, or None to skip the step
        ops = [
            (2, lambda: f'\x1b[{row()};{rnd.randint(1, s.columns)}H'),
            (3, lambda: rnd.choice(('\n', '\x1bD', '\x1bM', '\x1bE'))),
            (2, lambda: rnd.choice(('\x1b[r', f'\x1b[{rnd.randint(1, 2)};{rnd.randint(3, 6)}r'))),
            (3, lambda: f'\x1b[{rnd.randint(1, 3)}{rnd.choice("STLM")}'),
            (3, lambda: CUP(row()) + rnd.choice(('\x1b[2K', '\x1b[J', '\x1b[1J', '\x1b[2J', f'\x1b[{rnd.randint(1, 12)}X'))),
            (2, overlay),
            (2, lambda: None if irm else write_at_row(f'\x1b]66;w=1:s={rnd.randint(1, 2)};x\x07')),
            (2, lambda: None if irm else write_at_row('\x1b[2G\x1b]66;s=2;a\x07')),
            (3, lambda: write_at_row(rnd.choice(('q', '\u4e2d', 'e\u0301')), 2, drawn=True)),
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
