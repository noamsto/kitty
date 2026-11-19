#!/usr/bin/env python3
# obj_ir.py CG...: self Ir per object file (ob=) of each callgrind output, whole program
import re
import sys
from collections import defaultdict


def per_object(path):
    names, totals = {}, defaultdict(int)
    ob = None
    in_call = False
    for line in open(path, errors='replace'):
        if line.startswith('cob='):
            m = re.match(r'cob=\((\d+)\) (.+)', line.strip())
            if m:
                names[m.group(1)] = m.group(2)
        if line.startswith('ob='):
            m = re.match(r'ob=\((\d+)\)(?: (.*))?', line.strip())
            if m:
                if m.group(2):
                    names[m.group(1)] = m.group(2)
                ob = names.get(m.group(1), m.group(1))
            else:
                ob = line[3:].strip()
        elif line.startswith('calls='):
            in_call = True
        elif line[:1].isdigit() or line[:1] in '+-*':
            parts = line.split()
            if in_call:
                in_call = False  # the line after calls= is the inclusive cost of the call, not self cost
                continue
            if len(parts) >= 2 and ob is not None:
                totals[ob] += int(parts[1])
    return totals


if __name__ == '__main__':
    for p in sys.argv[1:]:
        t = per_object(p)
        kitty = sum(v for k, v in t.items() if 'fast_data_types' in k)
        print(f'{p.rsplit("/", 1)[-1]}\tfast_data_types.so {kitty}\tother {sum(t.values()) - kitty}')
