#!/usr/bin/env python3
# fn_per_resize.py OLD20.cg OLD40.cg NEW20.cg NEW40.cg [N]: per function of fast_data_types, self Ir per resize
# ((NEW40 - NEW20) - (OLD40 - OLD20)) / 20, top N by absolute value; run inside nix-shell shell-m.nix
import os, subprocess, re, sys


def self_ir(cg):
    out = subprocess.run(['callgrind_annotate', '--inclusive=no', '--threshold=100', cg], capture_output=True, text=True, check=True).stdout
    d = {}
    for line in out.splitlines():
        m = re.match(r'\s*([\d,]+) \([^)]*\)\s+(?:\*\s+)?(\S.*)$', line)
        if m and 'PROGRAM TOTALS' not in line and 'fast_data_types' in m.group(2):
            fn = re.sub(r' \[[^]]*\]$', '', m.group(2))
            d[fn] = d.get(fn, 0) + int(m.group(1).replace(',', ''))
    return d


o20, o40, n20, n40 = (self_ir(p) for p in sys.argv[1:5])
n = int(sys.argv[5]) if len(sys.argv) > 5 else 10
fns = set(o20) | set(o40) | set(n20) | set(n40)
po = {f: (o40.get(f, 0) - o20.get(f, 0)) / 20 for f in fns}
pn = {f: (n40.get(f, 0) - n20.get(f, 0)) / 20 for f in fns}
d = sorted(((pn[f] - po[f], f) for f in fns), key=lambda t: -abs(t[0]))
print(f'per resize: old {sum(po.values()):,.1f} new {sum(pn.values()):,.1f} delta {sum(pn.values()) - sum(po.values()):+,.1f}')
for dv, f in d[:n]:
    if dv:
        print(f'{dv:+12,.1f}  old {po[f]:>12,.1f}  new {pn[f]:>12,.1f}  {f}')
