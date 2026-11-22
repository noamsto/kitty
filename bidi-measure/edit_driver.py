# Editing payloads for callgrind, run from a build tree's root. No bidi sequence is sent: fills a 120x50 alternate screen
# with plain ASCII text and calls one operation N times, each call with what keeps its row full of text (a cursor move
# and the text the call erased or shifted out), cycling over the rows. Per call = (Ir(2N) - Ir(N)) / N; it includes
# that cursor move and text, which are the same on every tree.
# argv: KIND N kit_dir, KIND one of
#   el      CSI K at column 41, then the 80 erased cells drawn again
#   el2     CSI 2 K, then the row drawn again
#   ich     CSI 3 @ at column 20, then 3 characters in the inserted cells
#   dch     CSI 3 P at column 20, then 3 characters at the end of the row
#   ech     CSI 3 X at column 20, then 3 characters in the erased cells
#   il      CSI L at column 1, then the inserted row drawn
#   dl      CSI M at column 1, then the bottom row drawn
#   osc66w  OSC 66 w=2 (2 cells), 50 per row; from the second page on each overwrites one of the previous page
#   osc66s  OSC 66 s=2 (4x2 cells), 25 per row pair; the same
#   ht      HT, 15 per row (to the last column), over text
#   cht     CSI I, the same
#   htb     HT over blank cells, which stores a tab: each row is erased by CSI 2 K before its 15 tabs (1/15 of an el2
#           call without the redraw is in each call)
import os
import sys

sys.path.insert(0, os.getcwd())
from kitty.fast_data_types import VT_PARSER_BUFFER_SIZE, Screen  # noqa: E402
from kitty.fonts.render import setup_for_testing  # noqa: E402
from kitty_tests.base import Callbacks  # noqa: E402

kind, n_calls = sys.argv[1], int(sys.argv[2])
font = os.path.join(sys.argv[3], 'LiberationMono-Regular.ttf')
cols, lines = 120, 50


def text(k: int, width: int) -> str:
    return ''.join(chr(33 + (k + i) % 94) for i in range(width))


def cup(row: int, col: int) -> str:
    return f'\x1b[{row};{col}H'


def per_row(k: int) -> int:
    return k % lines + 1


CALL = {
    'el': lambda k: cup(per_row(k), 41) + '\x1b[K' + text(k, 80),
    'el2': lambda k: cup(per_row(k), 1) + '\x1b[2K' + text(k, cols),
    'ich': lambda k: cup(per_row(k), 20) + '\x1b[3@' + text(k, 3),
    'dch': lambda k: cup(per_row(k), 20) + '\x1b[3P' + cup(per_row(k), cols - 2) + text(k, 3),
    'ech': lambda k: cup(per_row(k), 20) + '\x1b[3X' + text(k, 3),
    'il': lambda k: cup(per_row(k), 1) + '\x1b[L' + text(k, cols),
    'dl': lambda k: cup(per_row(k), 1) + '\x1b[M' + cup(lines, 1) + text(k, cols),
    'osc66w': lambda k: (cup(k // 50 % lines + 1, 1) if k % 50 == 0 else '') + '\x1b]66;w=2;AB\x07',
    'osc66s': lambda k: (cup(2 * (k // 25 % (lines // 2)) + 1, 1) if k % 25 == 0 else '') + '\x1b]66;s=2;AB\x07',
    'ht': lambda k: (cup(k // 15 % lines + 1, 1) if k % 15 == 0 else '') + '\t',
    'cht': lambda k: (cup(k // 15 % lines + 1, 1) if k % 15 == 0 else '') + '\x1b[I',
    'htb': lambda k: (cup(k // 15 % lines + 1, 1) + '\x1b[2K' if k % 15 == 0 else '') + '\t',
}
fill = '\x1b[?1049h' + '\r\n'.join(text(k, cols) for k in range(lines))
data = (fill + ''.join(CALL[kind](k) for k in range(n_calls))).encode()
with setup_for_testing(main_face_path=font) as (_, cw, ch):
    c = Callbacks()
    s = Screen(c, lines, cols, 0, cw, ch, 0, c)
    mv = memoryview(data)
    for i in range(0, len(data), VT_PARSER_BUFFER_SIZE):
        chunk = mv[i:i + VT_PARSER_BUFFER_SIZE]
        while True:
            m = s.test_commit_write_buffer(chunk, s.test_create_write_buffer())
            s.test_parse_written_data()
            if m == len(chunk):
                break
            chunk = chunk[m:]
    print(kind, n_calls, 'bytes', len(data), 'cursor', s.cursor.x, s.cursor.y, repr(str(s.line(0))[:24]), repr(str(s.line(s.cursor.y))[:24]))
