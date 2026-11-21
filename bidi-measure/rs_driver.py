# Resize payload for callgrind, run from a build tree's root: fills a 120x50 main screen and its 2,000-line history with
# plain_lines text (80 ASCII chars per line, no sequence), then resizes the screen N times between 100x40 and 120x50.
# argv: kind (plain: no sequence at all; stamped: the first line is written under CSI 8 h, then CSI 8 l), N, measure dir,
# [-v: print the history count and cursor after each resize]
import os
import sys

sys.path.insert(0, os.getcwd())
from kitty.fast_data_types import VT_PARSER_BUFFER_SIZE, Screen  # noqa: E402
from kitty.fonts.render import setup_for_testing  # noqa: E402
from kitty_tests.base import Callbacks  # noqa: E402

kind, n = sys.argv[1], int(sys.argv[2])
font = os.path.join(sys.argv[3], 'LiberationMono-Regular.ttf')
verbose = '-v' in sys.argv[4:]
cols, lines, hist = 120, 50, 2000
text = [''.join(chr(33 + (k + i) % 94) for i in range(80)) for k in range(lines + hist)]
if kind == 'stamped':
    text[0] = '\x1b[8h' + text[0] + '\x1b[8l'
data = '\r\n'.join(text).encode()
with setup_for_testing(main_face_path=font) as (_, cw, ch):
    c = Callbacks()
    s = Screen(c, lines, cols, hist, cw, ch, 0, c)
    mv = memoryview(data)
    for i in range(0, len(data), VT_PARSER_BUFFER_SIZE):
        chunk = mv[i:i + VT_PARSER_BUFFER_SIZE]
        while True:
            m = s.test_commit_write_buffer(chunk, s.test_create_write_buffer())
            s.test_parse_written_data()
            if m == len(chunk):
                break
            chunk = chunk[m:]
    print('filled', s.historybuf.count, s.cursor.x, s.cursor.y)
    for k in range(n):
        if k % 2:
            s.resize(lines, cols)
        else:
            s.resize(40, 100)
        if verbose:
            print('resize', k + 1, s.lines, s.columns, s.historybuf.count, s.cursor.x, s.cursor.y)
    print('end', s.lines, s.columns, s.historybuf.count, s.cursor.x, s.cursor.y, s.cpu_cells(0, 0)['text'])
