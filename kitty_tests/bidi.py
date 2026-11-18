#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

from itertools import product

from .base import BaseTest, draw_multicell, parse_bytes

NONE, LTR, RTL, AUTO = (0, False, False), (1, False, False), (1, True, False), (1, False, True)
POWER_ON = (False, False, False)
CUP = '\x1b[{};1H'.format  # 1-based row
LRM, RLM = '\u200e', '\u200f'


class Dump(list):
    def __call__(self, window_id, *a):
        if a and a[0] != 'bytes':
            self.append(tuple(a))


def pb(s, data):
    parse_bytes(s, data.encode())


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
            ('', '2 k', True), ('', '2;5 k', True), ('2 k', '1 k', False), ('2 k', '0 k', False), ('2 k', ' k', False),
            ('2 k', '1;0 k', False), ('2 k', '1;2;3 k', True), ('2 k', '3 k', True), ('2 k', '?1 k', True),
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
        for first, joiner in (('\U0001F44D', '\U0001F3FD'), ('\U0001F1FA', '\U0001F1F8')):
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
        # at most four are held, the fifth is dropped
        s = self.screen()
        pb(s, LRM + RLM + LRM)
        pb(s, RLM + LRM + 'a')
        self.ae(self.cell_texts(s, n=2), [LRM + RLM + LRM + RLM + 'a', ''])

    def test_other_zero_width_chars_at_line_start(self):
        # still discarded, and they leave a held mark alone
        for ch in '‍‌؜‪‮⁦⁧⁨⁩':
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

    def test_leading_mark_hold(self):
        def kept(*parts, lines=5, y=0):
            s = self.screen(lines=lines)
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
        # the text writes elsewhere first
        self.assertFalse(kept(RLM, '\x1b[1;3Hb\ra'))
        self.assertFalse(kept(RLM, '\tb\ra'))
        # a line feed drops it, also on a one line screen, whose line feed clears the line in place
        self.assertFalse(kept(RLM + '\na', y=1))
        self.assertFalse(kept(RLM, '\n\x1bMa'))
        self.assertFalse(kept(RLM + '\na', lines=1))
        self.assertFalse(kept(RLM, '\na', lines=1))
        # moving the line: the row storage can come back to the same place in a scroll region, so these drop it
        for seq in ('\x1bD\x1bD', '\x1bE\x1bE', '\x1b[S\x1b[S', '\x1b[2S', '\x1b[T\x1b[T', '\x1b[2T', '\x1b[+T\x1b[+T', '\x1b[L\x1b[L', '\x1b[M\x1b[M'):
            with self.subTest(seq=seq):
                self.assertFalse(kept('\x1b[1;2r' + CUP(2) + RLM, seq, CUP(2) + 'a', y=1))
        self.assertFalse(kept('\x1b[1;2r' + RLM, '\x1bM\x1bM', CUP(1) + 'a'))
        self.assertFalse(kept(CUP(5) + RLM, '\x1bD\ra', y=4))
        self.assertFalse(kept(CUP(3) + RLM, lambda s: s.scroll_until_cursor_prompt(), '\x1b[1;1Ha'))
        for y in range(5):
            with self.subTest(y=y):
                self.assertFalse(kept('\r\n'.join('x' * 5), CUP(y + 1) + RLM, '\x1b[22J', CUP(y + 1) + 'a', y=y))
        # reset, the other screen and resizing drop it
        self.assertFalse(kept(RLM, '\x1bc', 'a'))
        self.assertFalse(kept(RLM, '\x1b[?1049h', '\x1b[?1049l', '\x1b[1;1Ha'))
        self.assertFalse(kept(RLM, '\x1b[?1049h', 'a', '\x1b[?1049l', lambda s: self.ae(s.cpu_cells(0, 0)['text'], ''), '\x1b[1;1Ha'))
        self.assertFalse(kept(RLM, lambda s: s.resize(5, 12), '\x1b[1;1Ha'))
        # held with bidi mode off, written with it on
        self.assertTrue(kept(RLM, '\x1b[8h', 'a'))

    def test_leading_mark_hold_across_pre_edit(self):
        for preedit in ('xy', RLM + 'xy'):
            with self.subTest(preedit=preedit):
                s = self.screen()
                pb(s, RLM)
                s.test_draw_overlay_line(preedit, 0, 0)
                self.ae(s.cpu_cells(0, 0)['text'], preedit[:-1])
                pb(s, 'a')
                self.ae(s.cpu_cells(0, 0)['text'], RLM + 'a')

    def test_leading_marks_on_many_lines_in_one_draw(self):
        s = self.create_screen(cols=10, lines=5, scrollback=20)
        s.draw((RLM + 'x\r\n') * 10000)
        self.ae([str(s.line(y)) for y in range(5)], [RLM + 'x'] * 4 + [''])
        self.ae(s.historybuf.count, 20)
        self.ae({str(s.historybuf.line(i)) for i in range(20)}, {RLM + 'x'})
