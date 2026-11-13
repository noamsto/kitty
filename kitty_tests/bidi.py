#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

from itertools import product

from .base import BaseTest, draw_multicell, parse_bytes

NONE, LTR, RTL, AUTO = (0, False, False), (1, False, False), (1, True, False), (1, False, True)
POWER_ON = (False, False, False)
CUP = '\x1b[{};1H'.format  # 1-based row


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
        for opts in ({}, {'force_ltr': True}):
            s = self.screen(**opts)
            for setup in ('', '\x1b[?2501h', '\x1b[?2501l'):
                self.ae(self.reply(s, setup + '\x1b[?2501$p'), b'\x1b[?2501;4$y', (opts, setup))

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
        pb(s, '\x1b[8ha\r\n\x1b[8lb')  # the LF stamped row 1, 8l removes that blank record
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
                    self.assertNotEqual(self.records(s, 1), [NONE])
        for x in ('def', '\rdef'):
            with self.subTest(x=x):
                s = self.screen()
                pb(s, 'abc')
                pb(s, '\x1b[8h' + x)
                self.ae(self.records(s, 0), [LTR])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\r\x1b[2K\x1b[1 kb')
        self.ae(self.records(s, 0), [LTR])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\r\x1b[K\x1b[8l')
        self.ae(self.records(s, 0), [NONE])

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
        for seq in ('8h', '8l', '?2501h', '?2501l'):
            pb(s, f'\x1b[{seq}')
            self.ae(self.reply(s, '\x1b[8$p'), b'\x1b[8;4$y', seq)
            self.ae(self.reply(s, '\x1b[?2501$p'), b'\x1b[?2501;4$y', seq)
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
        pb(s, '\x1b[8ha\r\n')
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
        pb(s, '\x1b[8ha\r\n\x1b[?1049h\x1b[8hb\r\n\x1b[!p')
        self.ae(s.test_bidi_state(), (False, False, False, True, True))
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        pb(s, '\x1b[?1049l')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        self.ae(s.test_bidi_state()[3:], (True, True))
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
        pb(s, '\x1b[8ha\r\n\r\n\x1b[?2501h')
        self.ae(self.records(s, 0, 1, 2), [LTR, AUTO, AUTO])
        s = self.screen()
        pb(s, '\x1b[8h \x1b[2 k')  # a written space is text
        self.ae(self.records(s, 0), [LTR])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?2501h\x1b[?s\x1b[?2501la\r\n')
        self.ae(self.records(s, 0, 1), [LTR, LTR])
        pb(s, '\x1b[?r')
        self.ae(self.records(s, 0, 1), [LTR, AUTO])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[?1049h\x1b[8ha\r\n\x1b[?1049l')
        pb(s, '\x1b[?47h')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        s = self.screen()
        pb(s, '\x1b[8ha\r\n\x1b[8l')
        self.ae(self.records(s, 0, 1), [LTR, NONE])
        s = self.screen()
        pb(s, '\x1b[8h\x1b[2 ka\x1b[1 k' + CUP(1) + '\x1b[2K\x1b[1 k')  # a no-op set re-derives nothing
        self.ae(self.records(s, 0), [RTL])
