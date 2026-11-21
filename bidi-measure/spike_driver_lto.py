#!/usr/bin/env python
# License: GPLv3

# Measurement driver. Not a test: all work lives in main()
# because the test runner imports every module in kitty_tests/. The same file
# runs unchanged on every build being compared.
#
# Run from the root of a kitty build tree:
#
#   python3 spike_driver_lto.py MODE [options]
#
# Modes (payloads are the generators in spike_payloads.py, --count their size):
#   write   parse the payload through the parser only, no rendering
#   frame   parse the payload in frames of at most LINES-1 rows and render each
#           frame with Screen.test_update_cell_data, timing parse and render
#           separately; crc32 is over the bytes of all rendered frames
#   idle    fill the screen, render once, then time frames with nothing changed
#           and frames in which one cell changed
#   scrolled fill the scrollback, scroll the view back, render once, then render
#           --frames frames with nothing changed. --render-before-scroll renders
#           one frame after the fill and before the scroll, as output is
#           rendered before a user scrolls back; without it the rows below the
#           scrolled-back view were never rendered and stay dirty
#   stream  fill like idle (3*cols*lines cells, so history exists) and render once, then --frames
#           times: write the next --lines-per-frame lines of the payload (the
#           lines after the fill's), each followed by CR LF, and render (timed). --scroll-back K scrolls
#           the view back K rows (s.scroll(K, True)) after the first render,
#           renders once, then streams; kitty bumps scrolled_by on every push,
#           so the view stays anchored on the same history lines
#
# --preamble-bdsm sends CSI 8 h before the payload, after CSI ? 1049 h when the
# alternate screen is in use. A build that does not know the mode logs and
# ignores it. --preamble-direction sends, right after that (so after any alternate
# screen switch), CSI 2 SPACE k (rtl), CSI ? 2501 h (auto) or both (autortl); it needs
# --preamble-bdsm. --no-timing runs a mode once, with no warmup and no clock or
# checksum calls, for use under callgrind.
#
# The screen is the alternate screen for write mode (the main screen for the
# scrollback payload) and the main screen for the other modes, see --screen.
#
# The font is --font, else $BIDI_BENCH_FONT, else LiberationMono-Regular.ttf
# next to this file, so every build uses the same bytes. The shaped-run cache is not
# cleared between runs, so runs after the first measure the warm-cache cost.

import os
import sys
from typing import Any


DEFAULT_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'LiberationMono-Regular.ttf')


def is_strong(ch: str) -> bool:
    return 'א' <= ch <= 'ת' or ('a' <= ch <= 'z') or ('A' <= ch <= 'Z')


def main() -> None:
    import argparse
    import json
    import statistics
    import time
    import zlib

    # This copy lives outside the build trees: the tree under test is the cwd.
    sys.path.insert(0, os.getcwd())

    from kitty.fast_data_types import VT_PARSER_BUFFER_SIZE, Screen
    from kitty.fonts.render import setup_for_testing
    from kitty_tests.base import Callbacks
    from spike_payloads import GENERATORS, set_screen

    ap = argparse.ArgumentParser(description='kitty measurement driver')
    ap.add_argument('mode', choices=('write', 'frame', 'idle', 'scrolled', 'stream'))
    ap.add_argument('--payload', choices=sorted(GENERATORS), default='ascii')
    ap.add_argument('--count', type=int, help='payload size, see spike_payloads.py (default: 200000, idle: cols*lines, scrolled: 100000)')
    ap.add_argument('--runs', type=int, default=5, help='timed runs of write and frame mode')
    ap.add_argument('--warmup', type=int, default=1, help='untimed runs before the timed runs of write and frame mode')
    ap.add_argument('--frames', type=int, default=200, help='frames per probe in idle and scrolled mode')
    ap.add_argument('--cols', type=int, default=120)
    ap.add_argument('--lines', type=int, default=50)
    ap.add_argument('--scrollback', type=int, default=2000)
    ap.add_argument('--scroll-by', type=int, help='rows to scroll the view back in scrolled mode (default: lines/2)')
    ap.add_argument('--render-before-scroll', action='store_true', help='scrolled mode: render once before scrolling back')
    ap.add_argument('--scroll-back', type=int, default=0, help='stream mode: rows to scroll the view back before streaming')
    ap.add_argument('--lines-per-frame', type=int, default=1, help='stream mode: payload lines written per frame')
    ap.add_argument('--screen', choices=('auto', 'main', 'alt'), default='auto')
    ap.add_argument('--only', choices=('unchanged', 'onecell'), help='idle mode: run only this frame type')
    ap.add_argument('--edit', choices=('latin', 'same', 'diff', 'flipfirst'), default='latin', help='idle onecell: latin writes a-z, same writes Hebrew letters (class R each frame), diff alternates a Hebrew and a Latin letter, flipfirst alternates a Hebrew alef and an x in the first strong (Hebrew or Latin letter) cell found scanning the screen from the top-left after the fill, so under ?2501 the direction of that paragraph flips every frame')
    ap.add_argument('--preamble-bdsm', action='store_true')
    ap.add_argument('--preamble-direction', choices=('ltr', 'rtl', 'auto', 'autortl'), default='ltr', help='sent after the BDSM preamble: rtl CSI 2 SPACE k, auto CSI ? 2501 h, autortl both; needs --preamble-bdsm')
    ap.add_argument('--no-timing', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--font', default=os.environ.get('BIDI_BENCH_FONT') or DEFAULT_FONT)
    args = ap.parse_args(sys.argv[1:])
    if not os.path.exists(args.font):
        raise SystemExit(f'error: font not found: {args.font}')

    if args.preamble_direction != 'ltr' and not args.preamble_bdsm:
        raise SystemExit('error: --preamble-direction needs --preamble-bdsm')
    if args.edit == 'flipfirst' and (args.mode != 'idle' or args.only != 'onecell'):
        raise SystemExit('error: --edit flipfirst needs idle --only onecell')

    cols, lines = args.cols, args.lines
    count = args.count or {'idle': cols * lines, 'stream': 3 * cols * lines, 'scrolled': 100000}.get(args.mode, 200000)
    set_screen(cols, lines)
    payload = GENERATORS[args.payload](count)
    use_alt = args.screen == 'alt' or (args.screen == 'auto' and args.mode == 'write' and not payload.main_screen)
    preamble = (b'\x1b[?1049h' if use_alt else b'') + (b'\x1b[8h' if args.preamble_bdsm else b'')
    preamble += {'ltr': b'', 'rtl': b'\x1b[2 k', 'auto': b'\x1b[?2501h', 'autortl': b'\x1b[2 k\x1b[?2501h'}[args.preamble_direction]
    timed = not args.no_timing
    clock = time.perf_counter_ns

    def feed(s: Screen, chunks: 'list[Any]') -> None:
        # what kitty_tests.base.parse_bytes does, without slicing the data when the write buffer takes a whole chunk
        for chunk in chunks:
            while True:
                n = s.test_commit_write_buffer(chunk, s.test_create_write_buffer())
                s.test_parse_written_data()
                if n == len(chunk):
                    break
                chunk = chunk[n:]

    def summary(prefix: str, ns: 'list[int]') -> 'dict[str, Any]':
        return {f'{prefix}_best_ns': min(ns), f'{prefix}_median_ns': statistics.median(ns), f'{prefix}_all_ns': ns}

    def per_frame_ms(ns: 'list[int]') -> 'dict[str, float]':
        return {'best': min(ns) / 1e6, 'median': statistics.median(ns) / 1e6, 'mean': statistics.fmean(ns) / 1e6}

    def split(data: bytes) -> 'list[Any]':
        mv = memoryview(data)
        return [mv[i : i + VT_PARSER_BUFFER_SIZE] for i in range(0, len(data), VT_PARSER_BUFFER_SIZE)]

    result: 'dict[str, Any]' = {
        'mode': args.mode, 'payload': args.payload, 'count': count, 'cells': payload.cells, 'bytes': sum(map(len, payload.pieces)),
        'cols': cols, 'lines': lines, 'preamble_bdsm': args.preamble_bdsm, 'preamble_direction': args.preamble_direction, 'alt_screen': use_alt, 'font': args.font,
    }  # fmt: skip

    fonts = setup_for_testing(main_face_path=args.font)
    with fonts as (_, cell_width, cell_height):
        result['cell'] = f'{cell_width}x{cell_height}'

        def new_screen(num_lines: int = lines, scrollback: int = args.scrollback) -> Screen:
            c = Callbacks()
            s = Screen(c, num_lines, cols, scrollback, cell_width, cell_height, 0, c)
            if preamble:
                feed(s, [preamble])
            return s

        def render(s: Screen) -> bytes:
            # the size and dpi of the group setup_for_testing created: any other pair would load a second group
            return s.test_update_cell_data(fonts.size, fonts.dpi, fonts.dpi)

        def make_frames() -> 'tuple[list[bytes], int]':
            # A frame holds at most lines - 1 screen rows: with the row the
            # cursor ends on, all of them are still on screen when the frame is
            # rendered, so every parsed row is rendered exactly once. The rows
            # are counted on a probe screen twice as tall, which never scrolls.
            probe = new_screen(2 * lines, 0)
            frames: list[bytes] = []
            current: list[bytes] = []
            rows = 0
            for piece in payload.pieces:
                y = probe.cursor.y
                feed(probe, [piece])
                if probe.cursor.y > lines - 1 and current:
                    frames.append(b''.join(current))
                    current.clear()
                    rows += y
                    feed(probe, [b'\x1b[H\x1b[2J', piece])
                current.append(piece)
            frames.append(b''.join(current))
            rows += probe.cursor.y
            return frames, rows * cols

        if args.mode == 'write':
            chunks = split(b''.join(payload.pieces))
            ns: list[int] = []
            for i in range(1 if not timed else args.warmup + args.runs):
                s = new_screen()
                if not timed:
                    feed(s, chunks)
                    continue
                t0 = clock()
                feed(s, chunks)
                t1 = clock()
                if i >= args.warmup:
                    ns.append(t1 - t0)
            if timed:
                result.update(summary('write', ns))
                if payload.cells:
                    result['ns_per_cell_best'] = min(ns) / payload.cells

        elif args.mode == 'frame':
            frames, cells = make_frames()
            result.update(frames=len(frames), rendered_cells=cells)
            parse_ns: list[int] = []
            render_ns: list[int] = []
            crcs: list[int] = []
            for i in range(1 if not timed else args.warmup + args.runs):
                s = new_screen()
                parse_t = render_t = crc = 0
                for frame in frames:
                    if not timed:
                        feed(s, [frame])
                        render(s)
                        continue
                    t0 = clock()
                    feed(s, [frame])
                    t1 = clock()
                    buf = render(s)
                    t2 = clock()
                    parse_t += t1 - t0
                    render_t += t2 - t1
                    crc = zlib.crc32(buf, crc)
                    if not i:
                        result['nonblank_cells'] = result.get('nonblank_cells', 0) + sum(1 for sp in memoryview(buf).cast('I')[0::2] if sp & 0x7FFFFFFF)
                if timed:
                    crcs.append(crc)
                    if i >= args.warmup:
                        parse_ns.append(parse_t)
                        render_ns.append(render_t)
            if timed:
                result.update(summary('parse', parse_ns))
                result.update(summary('render', render_ns))
                if cells:
                    result['render_ns_per_cell_best'] = min(render_ns) / cells
                result['crc32'] = crcs[0]
                result['crc32_stable'] = len(set(crcs)) == 1

        elif args.mode == 'idle':
            s = new_screen()
            feed(s, split(b''.join(payload.pieces)))
            render(s)

            edit_y, edit_x = lines // 2, cols // 2
            if args.edit == 'flipfirst':
                strong = {y: [x for x in range(cols) if is_strong(s.line(y)[x])] for y in range(lines)}
                edit_y, edit_x = next((y + 1, xs[0] + 1) for y, xs in strong.items() if xs)

            def edit_char(i: int) -> str:
                if args.edit == 'same':
                    return 'אבגדהוזחטי'[i % 10]
                if args.edit == 'flipfirst':
                    return 'א' if i % 2 == 0 else 'x'
                if args.edit == 'diff':
                    return 'א' if i % 2 == 0 else 'a'
                return chr(ord('a') + i % 26)

            if args.edit == 'same' and args.only == 'onecell':
                feed(s, [b'\x1b[%d;%dH%s' % (lines // 2, cols // 2, 'א'.encode())])
                render(s)
            unchanged: list[int] = []
            changed: list[int] = []
            for _ in range(0 if args.only == 'onecell' else args.frames):
                t0 = clock() if timed else 0
                render(s)
                if timed:
                    unchanged.append(clock() - t0)
            for i in range(0 if args.only == 'unchanged' else args.frames):
                feed(s, [b'\x1b[%d;%dH%s' % (edit_y, edit_x, edit_char(i).encode())])
                t0 = clock() if timed else 0
                render(s)
                if timed:
                    changed.append(clock() - t0)
            result['frames'] = args.frames
            if timed:
                if unchanged:
                    result['unchanged_ms_per_frame'] = per_frame_ms(unchanged)
                if changed:
                    result['one_cell_ms_per_frame'] = per_frame_ms(changed)

        elif args.mode == 'stream':
            s = new_screen()
            feed(s, split(b''.join(payload.pieces)))
            render(s)
            if args.scroll_back:
                s.scroll(args.scroll_back, True)
                render(s)
            # the lines that follow the fill's pieces in a longer payload of the same generator
            longer = GENERATORS[args.payload](count + args.frames * args.lines_per_frame * cols * 4).pieces
            rest = [p.removesuffix(b'\r\n') for p in longer[len(payload.pieces) :]] or [b'x']
            ns = []
            n = args.lines_per_frame
            for i in range(args.frames):
                feed(s, [rest[(i * n + j) % len(rest)] + b'\r\n' for j in range(n)])
                t0 = clock() if timed else 0
                render(s)
                if timed:
                    ns.append(clock() - t0)
            result.update(scrolled_by=s.scrolled_by, frames=args.frames)
            if timed:
                result['ms_per_frame'] = per_frame_ms(ns)

        else:
            s = new_screen()
            feed(s, split(b''.join(payload.pieces)))
            if args.render_before_scroll:
                render(s)
            s.scroll(args.scroll_by or lines // 2, True)
            render(s)
            ns = []
            for _ in range(args.frames):
                t0 = clock() if timed else 0
                render(s)
                if timed:
                    ns.append(clock() - t0)
            result.update(scrolled_by=s.scrolled_by, frames=args.frames)
            if timed:
                result['ms_per_frame'] = per_frame_ms(ns)

    if args.json:
        print(json.dumps(result))
        return
    for k, v in result.items():
        print(f'{k}: {v}')


if __name__ == '__main__':
    main()
