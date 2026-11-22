#!/usr/bin/env python3
# gen_cmds.py VARIANT [SET] > cmds : callgrind command list for the tree VARIANT in this directory.
#   SET base (default)  12 payloads x write/frame x RUN=1,2, no escape sequences
#   SET idle            idle uploads (1 and 201 frames, nothing changed) and the scrolled-back / stamped-history cases,
#                       bidi mode off; one run each
#   SET bidi            for trees with bidi support: the same with bidi mode on (CSI 8 h), the RTL, mixed and LRM / FSI
#                       payloads write and frame with bidi mode on, one run each; lrm_lines and its control
#                       plain_lines also with auto direction (CSI ? 2501 h), where the leading LRM sets the direction
#   SET off             the RTL, mixed and LRM / FSI payloads write and frame with bidi mode off (every tree), one run
#   SET sticky          the 12 payloads write and frame after CSI 8 h CSI 8 l (--preamble-bdsm-off), RUN=1,2; on a tree
#                       without bidi support the sequences are ignored
#   SET regions         region_driver.py, every mode write and frame, N and 2N scrolls, RUN=1,2
#   SET edits           edit_driver.py, every operation, N and 2N calls, RUN=1,2
import os, sys
art = os.path.dirname(os.path.abspath(__file__))
v = sys.argv[1]
which = sys.argv[2] if len(sys.argv) > 2 else 'base'
P = ['plain_lines', 'ascii', 'unicode', 'scrollback', 'unique_unicode', 'csi', 'long_escape_codes', 'truecolor_text',
     'palette16_text', 'halfblocks', 'images', 'cjk']
COUNT = {'plain_lines': (160000, 160000), 'halfblocks': (500000, 500000)}  # (write, frame); others 1000000, 500000
EXTRA = ['hebrew', 'arabic', 'mixed_rows', 'lrm_lines', 'fsi_lines', 'isolate_prompt']
EXTRA_COUNT = {'lrm_lines': 160000, 'fsi_lines': 160000, 'isolate_prompt': 160000}
BIDI_ONLY_LTR = ['ascii']  # an all-LTR payload in bidi mode
AUTO = ['lrm_lines', 'plain_lines']  # under auto direction: the leading LRM sets the direction, plain_lines the control
REGIONS = ['vtop', 'vfull', 'vnarrow', 'horizontal']
REGION_N = (400, 800)
EDITS = ['el', 'el2', 'ich', 'dch', 'ech', 'il', 'dl', 'osc66w', 'osc66s', 'ht', 'cht', 'htb']
EDIT_N = (3000, 6000)


def cg(mode, payload, cnt, *extra, run=1):
    return f'RUN={run} {art}/run_callgrind_lto.sh {v} {art}/{v} {mode} {payload} {cnt} {" ".join(extra)}'.rstrip()


def scen(case, frames):
    return f'RUN=1 {art}/run_scen.sh {v} {art}/{v} {case} {frames}'


def drv(driver, tag, kind, n, run):
    return f'RUN={run} {art}/run_drv.sh {v} {art}/{v} {driver} {tag} {kind} {n}'


if which in ('base', 'sticky'):
    extra = ('--preamble-bdsm-off',) if which == 'sticky' else ()
    for p in P:
        w, f = COUNT.get(p, (1000000, 500000))
        for mode, cnt in (('write', w), ('frame', f)):
            for run in (1, 2):
                print(cg(mode, p, cnt, *extra, run=run))
elif which == 'regions':
    for m in REGIONS:
        for variant in ('write', 'frame'):
            for n in REGION_N:
                for run in (1, 2):
                    print(drv('region_driver.py', 'region', f'{m}_{variant}', n, run))
elif which == 'edits':
    for k in EDITS:
        for n in EDIT_N:
            for run in (1, 2):
                print(drv('edit_driver.py', 'edit', k, n, run))
elif which == 'idle':
    for n in (1, 201):
        print(cg('idle', 'ascii', 6000, '--only', 'unchanged', '--frames', str(n)))
        for case in ('long_para_scrolled', 'long_para_bottom', 'stamped_history', 'stamped_history_nops'):
            print(scen(case, n))
elif which == 'bidi':
    for n in (1, 201):
        for p in ('hebrew', 'mixed_rows', 'ascii'):
            print(cg('idle', p, 6000, '--only', 'unchanged', '--frames', str(n), '--preamble-bdsm'))
        for case in ('long_para_scrolled_bidi', 'long_para_bottom_bidi', 'stamped_history_bidi', 'stamped_history_nops_bidi'):
            print(scen(case, n))
    for p in EXTRA + BIDI_ONLY_LTR:
        for mode in ('write', 'frame'):
            print(cg(mode, p, EXTRA_COUNT.get(p, 500000 if mode == 'frame' else 1000000), '--preamble-bdsm'))
    for p in AUTO:
        for mode in ('write', 'frame'):
            print(cg(mode, p, 160000, '--preamble-bdsm', '--preamble-direction', 'auto'))
elif which == 'off':
    for p in EXTRA:
        for mode in ('write', 'frame'):
            print(cg(mode, p, EXTRA_COUNT.get(p, 500000 if mode == 'frame' else 1000000)))
