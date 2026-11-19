#!/usr/bin/env python3
# fn_diff.py OLD.cg NEW.cg [N] [OBJ]: self-Ir per function (callgrind_annotate), top N absolute differences, only functions of objects matching OBJ if given
import re, subprocess, sys

def self_ir(cg):
    out = subprocess.run(['callgrind_annotate', '--inclusive=no', '--threshold=100', cg],
                         capture_output=True, text=True, check=True).stdout
    d = {}
    for line in out.splitlines():
        m = re.match(r'\s*([\d,]+) \([^)]*\)\s+(?:\*\s+)?(\S.*)$', line)
        if m and 'PROGRAM TOTALS' not in line and (len(sys.argv) < 5 or sys.argv[4] in m.group(2)):
            fn = re.sub(r' \[[^]]*\]$', '', m.group(2))
            d[fn] = d.get(fn, 0) + int(m.group(1).replace(',', ''))
    return d

old, new = self_ir(sys.argv[1]), self_ir(sys.argv[2])
n = int(sys.argv[3]) if len(sys.argv) > 3 else 12
diffs = sorted(((new.get(f, 0) - old.get(f, 0), f) for f in set(old) | set(new)), key=lambda t: -abs(t[0]))
print(f'total self old {sum(old.values())} new {sum(new.values())} delta {sum(new.values()) - sum(old.values()):+d}')
for dv, f in diffs[:n]:
    print(f'{dv:+12d}  old {old.get(f, 0):>12d}  new {new.get(f, 0):>12d}  {f}')
