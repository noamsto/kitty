#!/usr/bin/env python
# License: GPLv3 Copyright: 2025, Kovid Goyal <kovid at kovidgoyal.net>

# Parse and render timing harness for the bidi row reordering at GPU upload.
# Not a test: all work lives under the __main__ guard because the test runner
# imports every module in kitty_tests/.
#
# Run from a kitty build directory (also works on a build without the
# bidi_reorder option, provided test_screen_update_cell_data is available):
#
#   ./kitty/launcher/kitty +launch /abs/path/to/bidi_bench.py [--bidi on|off] [--runs N] [--count N]
#
# The font is $BIDI_BENCH_FONT, or LiberationMono-Regular.ttf next to this file.
# The shaped-run cache is deliberately not cleared between runs: runs after the
# first measure the warm-cache cost, which is what a terminal sees in steady
# state. Compare the first run separately if cold-cache cost matters.

import os
import sys


def main() -> None:
    import argparse
    import struct
    import time

    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.getcwd(), os.path.dirname(here)):
        if p not in sys.path:
            sys.path.append(p)

    from kitty.fast_data_types import Screen, test_screen_update_cell_data
    from kitty.fonts.render import setup_for_testing
    from kitty.options.types import defaults
    from kitty.fast_data_types import get_options, set_options
    from kitty_tests.base import Callbacks, parse_bytes

    ap = argparse.ArgumentParser(description='bidi parse/render timing harness')
    ap.add_argument('--bidi', choices=('on', 'off'), default='off',
                    help='set bidi_reorder; on is an error on builds without the option, off then means no option')
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--count', type=int, default=2000000, help='codepoints per scenario')
    args = ap.parse_args(sys.argv[1:])

    has_option = hasattr(defaults, 'bidi_reorder')
    if args.bidi == 'on' and not has_option:
        raise SystemExit('error: --bidi on requested but this build has no bidi_reorder option')

    font = os.environ.get('BIDI_BENCH_FONT') or os.path.join(here, 'LiberationMono-Regular.ttf')
    if not os.path.exists(font):
        raise SystemExit(f'error: font not found: {font}')

    COLS, LINES, SCROLLBACK = 120, 50, 2000

    # Data
    latin_words = ['alpha', 'beta', 'gamma', 'delta', 'kitty', 'render', 'cell', 'shape', 'row', 'glyph']
    hebrew_words = ['שלום', 'עולם', 'טרמינל', 'בדיקה', 'ברוכים', 'הבאים', 'שורה', 'תא', 'גופן', 'צבע']

    def fill(words_for: 'list[list[str]]', length: int, n: int) -> str:
        # build a line of exactly `length` characters from alternating word lists
        out: list[str] = []
        total = 0
        i = n
        while total < length:
            w = words_for[i % len(words_for)][(i // len(words_for) + n) % 10]
            i += 1
            out.append(w)
            total += len(w) + 1
        return ' '.join(out)[:length]

    def make_lines(kind: str, count: int) -> list[str]:
        lines: list[str] = []
        total = 0
        n = 0
        while total < count:
            if kind == 'latin_short':
                ln = fill([latin_words], 80, n)
            elif kind == 'latin_soft_wrapped':
                ln = fill([latin_words], 240, n)
            else:
                length = 240 if n % 5 == 4 else 80
                ln = fill([hebrew_words, latin_words], length, n)
                # make sure a line never ends in a space (it would be eaten by nothing, but keep it tidy)
                ln = ln.rstrip() + 'x' * (length - len(ln.rstrip()))
            lines.append(ln)
            total += len(ln)
            n += 1
        return lines

    def make_frames(kind: str) -> tuple[list[bytes], int, int]:
        lines = make_lines(kind, args.count)
        frames: list[bytes] = []
        cps = 0
        nbytes = 0
        for i in range(0, len(lines), LINES):
            text = ''.join(ln + '\r\n' for ln in lines[i:i + LINES])
            b = text.encode('utf-8')
            frames.append(b)
            cps += sum(len(ln) for ln in lines[i:i + LINES])
            nbytes += len(b)
        return frames, cps, nbytes

    scenarios = ['latin_short', 'latin_soft_wrapped', 'bidi_heavy']
    data = {name: make_frames(name) for name in scenarios}

    results: dict[str, list[tuple[float, float, int]]] = {name: [] for name in scenarios}
    sprite_checked = False

    with setup_for_testing(main_face_path=font) as (sprites, cell_width, cell_height):
        if has_option:
            # after setup_for_testing's own set_options, so the font group is untouched
            set_options(get_options()._replace(bidi_reorder=args.bidi == 'on'))
        print(f'bidi_reorder option present={has_option} bidi={args.bidi} cell={cell_width}x{cell_height} font={os.path.basename(font)}')

        def new_screen() -> Screen:
            c = Callbacks()
            return Screen(c, LINES, COLS, SCROLLBACK, cell_width, cell_height, 0, c)

        for name in scenarios:
            frames, cps, nbytes = data[name]
            for run in range(1, args.runs + 1):
                s = new_screen()
                parse_s = render_s = 0.0
                for frame in frames:
                    t0 = time.perf_counter()
                    parse_bytes(s, frame)
                    t1 = time.perf_counter()
                    buf = test_screen_update_cell_data(s)
                    t2 = time.perf_counter()
                    parse_s += t1 - t0
                    render_s += t2 - t1
                    if not sprite_checked:
                        # GPUCell is fg, bg, decoration_fg, sprite_idx, attrs: sprite_idx is the 4th uint32
                        stride = len(buf) // (LINES * COLS)
                        nonzero = sum(1 for off in range(12, len(buf), stride) if struct.unpack_from('<I', buf, off)[0] & 0x7fffffff)
                        print(f'sprite check: {nonzero} of {LINES * COLS} cells have a non-zero sprite index (cell stride {stride} bytes)')
                        if not nonzero:
                            raise SystemExit('error: no sprites in the GPU buffer, font group disturbed?')
                        sprite_checked = True
                nframes = len(frames)
                ms = render_s * 1000 / nframes
                results[name].append((parse_s, render_s, nframes))
                print(f'{name} run={run} parse_s={parse_s:.4f} render_s={render_s:.4f} frames={nframes} ms_per_frame={ms:.4f}')

    print()
    print('scenario            codepoints  utf8_MB  | parse_s min/mean   | render_s min/mean  | frames | ms/frame min/mean')
    for name in scenarios:
        _, cps, nbytes = data[name]
        r = results[name]
        ps = [x[0] for x in r]
        rs = [x[1] for x in r]
        nf = r[0][2]
        mss = [x * 1000 / nf for x in rs]
        print(f'{name:<19} {cps:>10}  {nbytes / 1e6:>7.2f}  | {min(ps):>7.3f} {sum(ps) / len(ps):>8.3f} | {min(rs):>8.3f} {sum(rs) / len(rs):>8.3f} | {nf:>6} | {min(mss):>7.3f} {sum(mss) / len(mss):>7.3f}')


if __name__ == '__main__':
    main()
