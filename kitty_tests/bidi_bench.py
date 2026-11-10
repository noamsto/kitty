#!/usr/bin/env python
# License: GPLv3

# Parse and render timing harness for the bidi row reordering at GPU upload.
# Not a test: all work lives in main() because the test runner imports every
# module in kitty_tests/.
#
# Run from a kitty build directory:
#
#   ./kitty/launcher/kitty +launch /abs/path/to/bidi_bench.py [--bidi on|off] [--runs N] [--count N]
#
# The font is $BIDI_BENCH_FONT, or LiberationMono-Regular.ttf next to this file.
# The shaped-run cache is not cleared between runs, so runs after the first
# measure the warm-cache cost.
#
# Each frame is rendered once and holds at most LINES - 1 screen rows, so every
# parsed row is rendered exactly once. cells is the number of cells in those
# rows and ns/cell the render time per cell.

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

    from kitty.fast_data_types import Screen, get_options, set_options, test_screen_update_cell_data
    from kitty.fonts.render import setup_for_testing
    from kitty.options.types import defaults
    from kitty_tests.base import Callbacks, parse_bytes

    ap = argparse.ArgumentParser(description='bidi parse/render timing harness')
    ap.add_argument(
        '--bidi', choices=('on', 'off'), default='off', help='set bidi_reorder; on is an error on builds without the option, off then means no option'
    )
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
                # bidi_heavy and latin_mixed share the 80/240 length schedule
                length = 240 if n % 5 == 4 else 80
                words = [hebrew_words, latin_words] if kind == 'bidi_heavy' else [latin_words]
                ln = fill(words, length, n)
                # pad with x so a line never ends in a space and has exactly `length` characters
                ln = ln.rstrip() + 'x' * (length - len(ln.rstrip()))
            lines.append(ln)
            total += len(ln)
            n += 1
        return lines

    def screen_rows(ln: str) -> int:
        # every character is one cell wide, a line of exactly COLS cells takes one row
        return max(1, -(-len(ln) // COLS))

    def make_frames(kind: str) -> tuple[list[bytes], list[int], int, int]:
        # A frame holds at most LINES - 1 screen rows: with the row the cursor
        # ends on, all of them are still on screen when the frame is rendered,
        # so every parsed row is rendered exactly once.
        lines = make_lines(kind, args.count)
        frames: list[bytes] = []
        frame_rows: list[int] = []
        cps = nbytes = 0
        current: list[str] = []
        rows = 0

        def flush() -> None:
            nonlocal cps, nbytes
            b = ''.join(ln + '\r\n' for ln in current).encode('utf-8')
            frames.append(b)
            frame_rows.append(rows)
            cps += sum(len(ln) for ln in current)
            nbytes += len(b)

        for ln in lines:
            r = screen_rows(ln)
            if r > LINES - 1:
                raise SystemExit(f'error: a {len(ln)} character line does not fit on the screen')
            if rows + r > LINES - 1:
                flush()
                current, rows = [], 0
            current.append(ln)
            rows += r
        if current:
            flush()
        return frames, frame_rows, cps, nbytes

    scenarios = ['latin_short', 'latin_soft_wrapped', 'latin_mixed', 'bidi_heavy']
    data = {name: make_frames(name) for name in scenarios}

    # parse_s, render_s, frames, rendered cells
    results: dict[str, list[tuple[float, float, int, int]]] = {name: [] for name in scenarios}
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
            frames, frame_rows, cps, nbytes = data[name]
            cells = sum(frame_rows) * COLS
            for run in range(1, args.runs + 1):
                s = new_screen()
                parse_s = render_s = 0.0
                for i, frame in enumerate(frames):
                    t0 = time.perf_counter()
                    parse_bytes(s, frame)
                    t1 = time.perf_counter()
                    if run == 1 and i == 1:
                        # outside the timed region: the rows to render are the
                        # frame's, plus the blank row the cursor ends on
                        dirty = len(s.linebuf.dirty_lines())
                        if dirty not in (frame_rows[i], frame_rows[i] + 1):
                            raise SystemExit(f'error: {name}: {dirty} dirty rows for a frame of {frame_rows[i]} rows')
                        t1 = time.perf_counter()
                    buf = test_screen_update_cell_data(s)
                    t2 = time.perf_counter()
                    parse_s += t1 - t0
                    render_s += t2 - t1
                    if not sprite_checked:
                        # GPUCell is fg, bg, decoration_fg, sprite_idx, attrs: sprite_idx is the 4th uint32
                        stride = len(buf) // (LINES * COLS)
                        nonzero = sum(1 for off in range(12, len(buf), stride) if struct.unpack_from('<I', buf, off)[0] & 0x7FFFFFFF)
                        print(f'sprite check: {nonzero} of {LINES * COLS} cells have a non-zero sprite index (cell stride {stride} bytes)')
                        if not nonzero:
                            raise SystemExit('error: no sprites in the GPU buffer, font group disturbed?')
                        sprite_checked = True
                nframes = len(frames)
                ms = render_s * 1000 / nframes
                ns = render_s * 1e9 / cells
                results[name].append((parse_s, render_s, nframes, cells))
                print(
                    f'{name} run={run} parse_s={parse_s:.4f} render_s={render_s:.4f} frames={nframes} ms_per_frame={ms:.4f} cells={cells} ns_per_cell={ns:.2f}'
                )

    def min_mean(xs: 'list[float]', width: int, prec: int) -> str:
        return f'{min(xs):>{width}.{prec}f} {sum(xs) / len(xs):>{width}.{prec}f}'

    print()
    print('scenario            codepoints  utf8_MB  | parse_s min/mean  | render_s min/mean | frames | ms/frame min/mean |     cells | ns/cell min/mean')
    for name in scenarios:
        _, _, cps, nbytes = data[name]
        r = results[name]
        ps = [x[0] for x in r]
        rs = [x[1] for x in r]
        nf, nc = r[0][2], r[0][3]
        mss = [x * 1000 / nf for x in rs]
        nss = [x * 1e9 / nc for x in rs]
        print(
            f'{name:<19} {cps:>10}  {nbytes / 1e6:>7.2f}  | {min_mean(ps, 8, 3)} | {min_mean(rs, 8, 3)} | {nf:>6}'
            f' | {min_mean(mss, 8, 3)} | {nc:>9} | {min_mean(nss, 7, 2)}'
        )


if __name__ == '__main__':
    main()
