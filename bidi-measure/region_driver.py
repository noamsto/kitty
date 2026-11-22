# Smooth scroll region payload for callgrind, run from a build tree's root. No bidi sequence is sent: fills a 120x50 main
# screen (2,000-line scrollback) with plain ASCII text, defines one smooth scroll region (OSC 2254, capacity 4) and
# scrolls it N times in cycles of 8 scrolls forward by 3/8 of a cell and 8 back, so that sub-cell shifts, whole-row
# (column) moves into the kept rows and restores from them all occur. After each scroll it draws the rows (columns) that
# moved into the rectangle, computed as the protocol says, with no synchronized output.
# argv: KIND N kit_dir, KIND = MODE_VARIANT, N a multiple of 16; per scroll = (Ir(2N) - Ir(N)) / N
#   MODE    vtop        vertical, full width, rows 1-40: the rows moving out of its top go into the scrollback
#           vfull       vertical, full width, rows 2-41
#           vnarrow     vertical, columns 10-89, rows 2-41
#           horizontal  horizontal, columns 21-100, rows 16-35
#   VARIANT write       parse only
#           frame       one Screen.test_update_cell_data upload after each scroll (the strip upload of the shifted region)
import os
import sys

sys.path.insert(0, os.getcwd())
from kitty.fast_data_types import VT_PARSER_BUFFER_SIZE, Screen  # noqa: E402
from kitty.fonts.render import setup_for_testing  # noqa: E402
from kitty_tests.base import Callbacks  # noqa: E402

REGIONS = {  # x, y, w, h (1-based, cells), horizontal
    'vtop': (1, 1, 120, 40, False),
    'vfull': (1, 2, 120, 40, False),
    'vnarrow': (10, 2, 80, 40, False),
    'horizontal': (21, 16, 80, 20, True),
}
mode, variant = sys.argv[1].rsplit('_', 1)
n_scrolls = int(sys.argv[2])
font = os.path.join(sys.argv[3], 'LiberationMono-Regular.ttf')
if variant not in ('write', 'frame') or n_scrolls % 16:
    raise SystemExit(f'usage: {sys.argv[0]} MODE_write|MODE_frame N(multiple of 16) KIT_DIR')
x, y, w, h, horizontal = REGIONS[mode]
cols, lines, hist = 120, 50, 2000
counter = 0


def text(width: int) -> str:
    global counter
    counter += 1
    return ''.join(chr(33 + (counter + i) % 94) for i in range(width))


def cup(row: int, col: int) -> str:
    return f'\x1b[{row};{col}H'


def moved_in(n: int) -> str:
    # draw the |n| rows (columns) at the bottom (right) of the rectangle for n > 0, at its top (left) for n < 0
    k = min(abs(n), w if horizontal else h)
    first = (x + w - k if horizontal else y + h - k) if n > 0 else (x if horizontal else y)
    if not horizontal:
        return ''.join(cup(r, x) + text(w) for r in range(first, first + k))
    return ''.join(cup(r, c) + text(1) for c in range(first, first + k) for r in range(y, y + h))


def feed(s: Screen, data: bytes) -> None:
    mv = memoryview(data)
    for i in range(0, len(data), VT_PARSER_BUFFER_SIZE):
        chunk = mv[i:i + VT_PARSER_BUFFER_SIZE]
        while True:
            m = s.test_commit_write_buffer(chunk, s.test_create_write_buffer())
            s.test_parse_written_data()
            if m == len(chunk):
                break
            chunk = chunk[m:]


fonts = setup_for_testing(main_face_path=font)
with fonts as (_, cw, ch):
    c = Callbacks()
    s = Screen(c, lines, cols, hist, cw, ch, 0, c)
    cell = cw if horizontal else ch
    d = max(1, 3 * cell // 8)
    fill = '\r\n'.join(text(80 + k % 40) for k in range(lines))
    define = f'\x1b]2254;a=d:i=1:x={x}:y={y}:w={w}:h={h}:k=4{":o=h" if horizontal else ""}\x1b\\'
    feed(s, (fill + define).encode())

    def render() -> bytes:
        # the size and dpi of the group setup_for_testing created: any other pair would load a second group
        return s.test_update_cell_data(fonts.size, fonts.dpi, fonts.dpi)

    r = shifted = 0
    steps = []
    for i in range(n_scrolls):
        delta = d if i % 16 < 8 else -d
        m = r - delta
        if delta > 0:
            n = -(m // cell) if m < 0 else 0  # ceil(-m / cell)
        else:
            n = -m // cell if m > 0 else 0  # -ceil(m / cell)
        r = m + n * cell
        shifted += abs(n)
        steps.append(f'\x1b]2254;a=s:i=1:d={delta}\x1b\\' + moved_in(n))
    if variant == 'write':
        feed(s, ''.join(steps).encode())
    else:
        size = len(render())
        for step in steps:
            feed(s, step.encode())
            size = len(render())
        print('upload bytes', size)
    (reg,) = s.smooth_scroll_regions()
    print('cell', cw, ch, 'delta', d, 'scrolls', n_scrolls, 'moves', shifted, 'offset', reg['offset'], 'expected', r)
    print('history', s.historybuf.count, 'cursor', s.cursor.x, s.cursor.y, 'region', reg)
    if reg['offset'] != r:
        raise SystemExit('region offset differs from the protocol calculation')
