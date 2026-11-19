#!/usr/bin/env python3
# compare.py BASE NEW [OUTDIR]: markdown table, BASE vs NEW variant (names as in OUTDIR/*.cg and callgrind.jsonl).
# Own self Ir = fast_data_types.so self Ir (obj_ir.per_object over every .cg), median of runs.
# Own self Ir values are cached in OUTDIR/own_ir.json (cg file name -> Ir).
import json, os, statistics as st, sys
from collections import defaultdict
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
from obj_ir import per_object

base, new = sys.argv[1], sys.argv[2]
out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(here, 'out')
P = ['plain_lines', 'ascii', 'unicode', 'scrollback', 'unique_unicode', 'csi', 'long_escape_codes', 'truecolor_text',
     'palette16_text', 'halfblocks', 'images', 'cjk']
ROOT = {'write': 'parse_worker_ir', 'frame': 'update_ir'}

cache_path = os.path.join(out, 'own_ir.json')
try:
    cache = json.load(open(cache_path))
except (OSError, ValueError):
    cache = {}
d = defaultdict(lambda: defaultdict(list))
for line in open(os.path.join(out, 'callgrind.jsonl')):
    if not line.strip():
        continue
    r = json.loads(line)
    if r['variant'] not in (base, new) or r.get('extra'):
        continue
    cg = f"{r['variant']}.{r['mode']}.{r['payload']}.{r['count']}.{r['run']}.cg"
    if cg not in cache:
        t = per_object(os.path.join(out, cg))
        cache[cg] = sum(v for k, v in t.items() if 'fast_data_types' in k)
    r['own'] = cache[cg]
    d[(r['mode'], r['payload'])][r['variant']].append(r)
json.dump(cache, open(cache_path, 'w'), indent=1, sort_keys=True)

med = lambda rs, k: st.median(r[k] for r in rs)
spread = lambda rs, k: (max(r[k] for r in rs) - min(r[k] for r in rs)) / min(r[k] for r in rs) * 100
print(f'kitty own self Ir = `fast_data_types.so` self Ir (callgrind, `obj_ir.py`), median of runs per tree. {base} = base, {new} = new.\n')
print(f'| payload | mode | {base} own Ir | {new} own Ir | delta own | {base} root Ir | {new} root Ir | root Ir delta | runs | run spread |')
print('|---|---|---|---|---|---|---|---|---|---|')
for p in P:
    for mode in ('write', 'frame'):
        a, b = d[(mode, p)][base], d[(mode, p)][new]
        if not a or not b:
            continue
        ma, mb = med(a, 'own'), med(b, 'own')
        ra, rb = med(a, ROOT[mode]), med(b, ROOT[mode])
        sp = max(spread(a, 'own'), spread(b, 'own'), spread(a, ROOT[mode]), spread(b, ROOT[mode]))
        print(f'| {p} | {mode} | {ma:.0f} | {mb:.0f} | {(mb-ma)/ma*100:+.4f}% | {ra:.0f} | {rb:.0f} | {(rb-ra)/ra*100:+.4f}% | {len(a)}/{len(b)} | {sp:.4f}% |')
