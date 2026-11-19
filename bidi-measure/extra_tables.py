#!/usr/bin/env python3
# extra_tables.py OUTDIR : markdown tables for the idle, scrolled-back / stamped-history and bidi payload runs made by
# `gen_cmds.py VARIANT idle|off|bidi`. Numbers are callgrind Ir: own = fast_data_types.so self Ir, root = inclusive
# Ir of screen_update_cell_data (frame, idle) or of the parse root (write), fribidi = self Ir of libfribidi.
import json, os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
from obj_ir import per_object
from cg_extract_lto import parse

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, 'out')
rows = [json.loads(line) for line in open(os.path.join(out, 'callgrind.jsonl')) if line.strip()]
cache_path = os.path.join(out, 'obj_ir_cache.json')
try:
    cache = json.load(open(cache_path))
except (OSError, ValueError):
    cache = {}


def cgname(r):
    tag = re.sub(r'[^A-Za-z0-9\n]', '_', r['extra'])
    return f"{r['variant']}.{r['mode']}.{r['payload']}.{r['count']}{'.' + tag if tag else ''}.{r['run']}.cg"


def irs(r):
    cg = cgname(r)
    if cg not in cache:
        t = per_object(os.path.join(out, cg))
        ncalls = parse(os.path.join(out, cg))[2]
        cache[cg] = {'own': sum(v for k, v in t.items() if 'fast_data_types' in k), 'fribidi': sum(v for k, v in t.items() if 'fribidi' in k),
                     'resolves': ncalls.get('fribidi_get_par_embedding_levels_ex', 0)}
    return cache[cg]


def frames(r):
    for line in open(os.path.join(out, cgname(r)[:-3] + '.driver.log')):
        if line.startswith('frames:'):
            return int(line.split()[1])


def find(variant, mode, payload, count, extra='', run=1):
    for r in rows:
        if (r['variant'], r['mode'], r['payload'], r['count'], r['extra'], r['run']) == (variant, mode, payload, count, extra, run):
            return r


def idle_cost(variant, mode, payload, count, extra):
    # per-upload cost: (201 uploads - 1 upload) / 200
    def with_frames(n):
        if mode == 'scen':
            return find(variant, mode, payload, n)
        return find(variant, mode, payload, count, extra.replace('@N', str(n)))

    a, b = with_frames(1), with_frames(201)
    if not a or not b:
        return None
    ia, ib = irs(a), irs(b)
    return {'own': (ib['own'] - ia['own']) / 200, 'root': (b['update_ir'] - a['update_ir']) / 200, 'fribidi': (ib['fribidi'] - ia['fribidi']) / 200,
            'resolves': (ib['resolves'] - ia['resolves']) / 200}


def f(v):
    return '-' if v is None else f'{v:,.0f}'


def idle_table(title, cases):
    print(f'\n### {title}\n')
    print('| case | tree | bidi mode | own Ir / upload | root Ir / upload | FriBidi Ir / upload | FriBidi resolves / upload |')
    print('|---|---|---|---|---|---|---|')
    for label, variant, bidi, mode, payload, count, extra in cases:
        c = idle_cost(variant, mode, payload, count, extra)
        if c:
            print(f"| {label} | {variant} | {bidi} | {f(c['own'])} | {f(c['root'])} | {f(c['fribidi'])} | {c['resolves']:.2f} |")


def run_row(label, variant, bidi, mode, payload, count, extra=''):
    r = find(variant, mode, payload, count, extra)
    if not r:
        return None
    i = irs(r)
    root = r['update_ir'] if mode == 'frame' else r['parse_worker_ir']
    n = frames(r) if mode == 'frame' else None
    return label, variant, bidi, mode, i['own'], root, i['fribidi'], n


def payload_table(title, specs):
    print(f'\n### {title}\n')
    print('| payload | tree | bidi mode | mode | own Ir | root Ir | FriBidi Ir | frames | own Ir / frame | root Ir / frame |')
    print('|---|---|---|---|---|---|---|---|---|---|')
    for spec in specs:
        row = run_row(*spec)
        if not row:
            continue
        label, variant, bidi, mode, own, root, fb, n = row
        pf = (f(own / n), f(root / n)) if n else ('-', '-')
        print(f'| {label} | {variant} | {bidi} | {mode} | {f(own)} | {f(root)} | {f(fb)} | {n or "-"} | {pf[0]} | {pf[1]} |')


if __name__ == '__main__':
    B, F = os.environ.get('BASE', 'B'), os.environ.get('NEW', 'F')
    IDLE = '--only unchanged --frames @N'
    BD = ' --preamble-bdsm'
    idle_table('Idle upload (nothing changed)', [
        ('ascii screen', B, 'n/a', 'idle', 'ascii', 6000, IDLE),
        ('ascii screen', F, 'off', 'idle', 'ascii', 6000, IDLE),
        ('ascii screen', F, 'on', 'idle', 'ascii', 6000, IDLE + BD),
        ('hebrew screen', F, 'on', 'idle', 'hebrew', 6000, IDLE + BD),
        ('mixed_rows screen', F, 'on', 'idle', 'mixed_rows', 6000, IDLE + BD),
    ])
    idle_table('Scrolled back and stamped history (idle upload)', [
        ('1 MB paragraph, scrolled back 500, output below', B, 'n/a', 'scen', 'long_para_scrolled', 0, ''),
        ('1 MB paragraph, scrolled back 500, output below', F, 'off', 'scen', 'long_para_scrolled', 0, ''),
        ('1 MB paragraph, scrolled back 500, output below', F, 'on', 'scen', 'long_para_scrolled_bidi', 0, ''),
        ('same paragraph, not scrolled back', B, 'n/a', 'scen', 'long_para_bottom', 0, ''),
        ('same paragraph, not scrolled back', F, 'off', 'scen', 'long_para_bottom', 0, ''),
        ('same paragraph, not scrolled back', F, 'on', 'scen', 'long_para_bottom_bidi', 0, ''),
        ('bidi line in history, 300 plain lines, pixel_scroll on', B, 'n/a', 'scen', 'stamped_history', 0, ''),
        ('bidi line in history, 300 plain lines, pixel_scroll on', F, 'off (never bidi)', 'scen', 'stamped_history', 0, ''),
        ('bidi line in history, 300 plain lines, pixel_scroll on', F, 'on, then off', 'scen', 'stamped_history_bidi', 0, ''),
        ('same, pixel_scroll off', B, 'n/a', 'scen', 'stamped_history_nops', 0, ''),
        ('same, pixel_scroll off', F, 'off (never bidi)', 'scen', 'stamped_history_nops', 0, ''),
        ('same, pixel_scroll off', F, 'on, then off', 'scen', 'stamped_history_nops_bidi', 0, ''),
    ])
    specs = []
    for p, n in (('hebrew', 500000), ('arabic', 500000), ('mixed_rows', 500000), ('ascii', 500000)):
        specs.append((p, B, 'n/a', 'frame', p, n, ''))
        specs.append((p, F, 'off', 'frame', p, n, ''))
        specs.append((p, F, 'on', 'frame', p, n, BD.strip()))
    payload_table('Frames with bidi mode on, RTL and mixed (120x50)', specs)
    specs = []
    for p, n in (('plain_lines', 160000), ('lrm_lines', 160000), ('fsi_lines', 160000), ('isolate_prompt', 160000)):
        for mode in ('write', 'frame'):
            specs.append((p, B, 'n/a', mode, p, n, ''))
            specs.append((p, F, 'off', mode, p, n, ''))
            if p != 'plain_lines':
                specs.append((p, F, 'on', mode, p, n, BD.strip()))
    payload_table('LRM- and FSI-prefixed lines', specs)
    json.dump(cache, open(cache_path, 'w'), indent=1, sort_keys=True)
