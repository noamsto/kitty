#!/usr/bin/env python3
# fn_per_upload.py CG1 CG201 [N] : per-upload self Ir of each function, (CG201 - CG1) / 200, top N (run inside nix-shell)
import subprocess, sys
sys.argv = [sys.argv[0], *sys.argv[1:]]
import importlib.util, os
spec = importlib.util.spec_from_file_location('fn_diff_lib', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fn_diff.py'))
src = open(spec.origin).read().split('old, new = ')[0]
ns: dict = {}
exec(src, ns)
a, b = ns['self_ir'](sys.argv[1]), ns['self_ir'](sys.argv[2])
n = int(sys.argv[3]) if len(sys.argv) > 3 else 10
rows = sorted(((b.get(f, 0) - a.get(f, 0)) / 200, f) for f in set(a) | set(b))
for v, f in sorted(rows, key=lambda t: -t[0])[:n]:
    print(f'{v:12.1f}  {f}')
