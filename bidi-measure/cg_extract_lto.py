#!/usr/bin/env python3
# Extract inclusive Ir and call counts of named functions from a callgrind
# output file. Inclusive Ir of a function is its self cost plus the inclusive
# cost of each call it makes, which is what callgrind_annotate --inclusive=yes
# prints (the run_callgrind.sh wrapper cross-checks the total with it).
#
#   cg_extract.py FILE.cg VARIANT MODE PAYLOAD COUNT EXTRA RUN  ->  one JSON line on stdout
import json
import re
import sys

FN = re.compile(r'^c?fn=\((\d+)\)(?: (.*))?$')


def parse(path: str) -> 'tuple[int, dict[str, int], dict[str, int], dict[str, int]]':
    names: dict[str, str] = {}
    selfc: dict[str, int] = {}
    callc: dict[str, int] = {}  # inclusive cost of calls made by fn
    calls_to: dict[str, int] = {}
    total = 0
    cur = ''
    pending_call = ''
    pending_n = 0
    with open(path) as f:
        for line in f:
            if line.startswith(('fn=', 'cfn=')):
                m = FN.match(line.rstrip('\n'))
                if not m:
                    continue
                i, name = m.group(1), m.group(2)
                if name is not None:
                    names[i] = name
                if line.startswith('fn='):
                    cur = i
                else:
                    pending_call = i
                continue
            if line.startswith('calls='):
                pending_n = int(line.split()[0][6:])
                continue
            if line.startswith(('summary:', 'totals:')):
                total = max(total, int(line.split()[1]))
                continue
            c = line[:1]
            if not (c.isdigit() or c in '+-*'):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            cost = int(parts[1])
            if pending_call:
                callc[cur] = callc.get(cur, 0) + cost
                calls_to[pending_call] = calls_to.get(pending_call, 0) + pending_n
                pending_call = ''
            else:
                selfc[cur] = selfc.get(cur, 0) + cost
    incl: dict[str, int] = {}
    ncalls: dict[str, int] = {}
    for i, n in names.items():
        incl[n] = incl.get(n, 0) + selfc.get(i, 0) + callc.get(i, 0)
        ncalls[n] = ncalls.get(n, 0) + calls_to.get(i, 0)
    return total, incl, ncalls, {n: 1 for n in names.values()}


SYMS = {'parse_worker': 'parse_worker_ir', 'screen_update_cell_data': 'update_ir', 'render_line': 'render_line_ir'}


def main() -> None:
    # cg_extract.py FILE VARIANT MODE PAYLOAD COUNT EXTRA RUN
    path, variant, mode, payload, count, extra, run = sys.argv[1:8]
    total, incl, ncalls, _ = parse(path)
    out: dict = {
        'variant': variant, 'mode': mode, 'payload': payload, 'count': int(count), 'extra': extra, 'run': int(run), 'total_ir': total,
    }
    found: dict = {}
    # LTO inlines parse_worker; the closest surviving symbol is its caller test_parse_written_data[.lto_priv.N]
    def lookup(prefix: str) -> 'tuple[str, int] | None':
        c = [(n, v) for n, v in incl.items() if n.split('.')[0] == prefix]
        return max(c, key=lambda x: x[1]) if c else None
    for sym, key in SYMS.items():
        hit = lookup(sym)
        found[sym] = hit is not None
        out[key] = hit[1] if hit else None
    if out['parse_worker_ir'] is None:
        hit = lookup('test_parse_written_data')
        out['parse_root_sym'] = hit[0] if hit else None
        out['parse_worker_ir'] = hit[1] if hit else None
    # the spike renders reordered rows through render_line_levels, a copy of render_line
    rl = [s for s in ('render_line', 'render_line_levels') if s in incl]
    out['render_line_calls'] = sum(ncalls.get(s, 0) for s in rl) if rl else None
    if 'render_line_levels' in incl:
        out['render_line_ir'] = (out['render_line_ir'] or 0) + incl['render_line_levels']
        found['render_line'] = True
    # resolves per frame come from this call count; gcc clones (.constprop/.isra) count too
    out['bidi_resolve_calls'] = sum(v for n, v in ncalls.items() if n.split('.')[0] == 'bidi_resolve_paragraph')
    out['symbols_found'] = found
    # write mode never renders: those symbols are legitimately absent there
    required = ['parse_worker'] if mode == 'write' else list(SYMS)
    missing = [s for s in required if not found[s]]
    if missing:
        print(f'WARNING: symbols missing in {path} (inlined or not built with symbols): {missing}', file=sys.stderr)
    print(json.dumps(out))


if __name__ == '__main__':
    main()
