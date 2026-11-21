# Overwrite payloads for callgrind, run from a build tree's root: each pass homes the cursor and rewrites the 120x50 alternate
# screen, so from the second pass on every char is drawn over the first cell of a wide char (a nuke per char).
# argv: kind (wide: 中 over 中, narrow: z over 中 then 中 again), passes
import os
import sys

sys.path.insert(0, os.getcwd())
from kitty.fast_data_types import VT_PARSER_BUFFER_SIZE, Screen  # noqa: E402
from kitty.fonts.render import setup_for_testing  # noqa: E402
from kitty_tests.base import Callbacks  # noqa: E402

kind, passes = sys.argv[1], int(sys.argv[2])
font = os.path.join(sys.argv[3], 'LiberationMono-Regular.ttf')
cols, lines = 120, 50
wide = '\x1b[H' + '\r\n'.join(['中' * (cols // 2)] * lines)
narrow = '\x1b[H' + '\r\n'.join(['z' * (cols // 2)] * lines)  # z over the first cell of each 中, then the next 中 over its second
page = {'wide': wide + wide, 'narrow': wide + narrow}[kind]
data = ('\x1b[?1049h' + page * passes).encode()
with setup_for_testing(main_face_path=font) as (_, cw, ch):
    c = Callbacks()
    s = Screen(c, lines, cols, 0, cw, ch, 0, c)
    mv = memoryview(data)
    for i in range(0, len(data), VT_PARSER_BUFFER_SIZE):
        chunk = mv[i:i + VT_PARSER_BUFFER_SIZE]
        while True:
            n = s.test_commit_write_buffer(chunk, s.test_create_write_buffer())
            s.test_parse_written_data()
            if n == len(chunk):
                break
            chunk = chunk[n:]
    print('cursor', s.cursor.x, s.cursor.y, s.cpu_cells(0, 0)['text'])
