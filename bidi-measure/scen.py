#!/usr/bin/env python3
# License: GPLv3
# Idle-upload scenarios for callgrind, run from the root of a kitty build tree:
#   python3 scen.py CASE FRAMES
# Builds the scenario, renders once, then renders FRAMES uploads with nothing changed (the idle frame is
# (Ir(201) - Ir(1)) / 200). Cases:
#   long_para_scrolled[_bidi]   a 1 MB single paragraph at 200x60, scrolled back 500 rows, 20 lines of output
#                               written below it, rendered; bidi mode on for the _bidi case
#   long_para_bottom[_bidi]     the same without scrolling back (the control for the cost of bidi mode itself)
#   stamped_history[_bidi]      one line written (bidi mode on for _bidi, then off again), scrolled into history,
#                               300 plain lines after, pixel_scroll on, not scrolled back (120x50)
#   stamped_history_nops[_bidi] the same with pixel_scroll off
import sys

sys.path.insert(0, '.')
from kitty.fonts.render import setup_for_testing
from kitty_tests.base import BaseTest, parse_bytes

UNIT = 'אבג def 12, (שלום) xyz. '
BODY = UNIT * (1_000_000 // len(UNIT.encode()) + 1)
PLAIN = ''.join(f'line {i} ' + 'x' * 100 + '\r\n' for i in range(300))


class T(BaseTest):
    def runTest(self):
        pass


def main() -> None:
    case, frames = sys.argv[1], int(sys.argv[2])
    bidi = case.endswith('_bidi')
    mode = b'\x1b[8h' if bidi else b''
    t = T()
    fonts = setup_for_testing()

    def frame(s, repeat=1):
        # the size and dpi of the group setup_for_testing created: any other pair would load a second group
        return s.test_update_cell_data(fonts.size, fonts.dpi, fonts.dpi, repeat)

    with fonts:
        if case.startswith(('long_para_scrolled', 'long_para_bottom')):
            s = t.create_screen(cols=200, lines=60, scrollback=10000, options={'pixel_scroll': False})
            parse_bytes(s, mode + BODY.encode())
            frame(s)
            if 'scrolled' in case:
                s.scroll(500, True)
                frame(s)
            parse_bytes(s, b''.join(b'tail %d\r\n' % i for i in range(20)))
            frame(s)
        elif case.startswith('stamped_history'):
            s = t.create_screen(cols=120, lines=50, scrollback=1000, options={'pixel_scroll': not case.startswith('stamped_history_nops')})
            parse_bytes(s, mode + 'אבג abc\r\n'.encode() + (b'\x1b[8l' if bidi else b'') + PLAIN.encode())
            frame(s)
        else:
            raise SystemExit(f'unknown case {case}')
        print('history', s.historybuf.count, 'scrolled_by', s.scrolled_by)
        frame(s, frames)


main()
