#!/usr/bin/env python3
# perop.py OUTDIR TAG LO HI TREE... : own Ir per call (per scroll) of run_drv.sh runs, (own(HI) - own(LO)) / (HI - LO),
# per tree and KIND found in OUTDIR, for RUN=1 and RUN=2, with their spread
import glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from obj_ir import per_object

out, tag, lo, hi = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])


def own(path):
    return sum(v for k, v in per_object(path).items() if 'fast_data_types' in k)


for tree in sys.argv[5:]:
    kinds = sorted({os.path.basename(p).split('.')[2] for p in glob.glob(os.path.join(out, f'{tree}.{tag}.*.{lo}.1.cg'))})
    for kind in kinds:
        per = []
        for run in (1, 2):
            a, b = (os.path.join(out, f'{tree}.{tag}.{kind}.{n}.{run}.cg') for n in (lo, hi))
            if os.path.exists(a) and os.path.exists(b):
                per.append((own(b) - own(a)) / (hi - lo))
        spread = (max(per) - min(per)) / min(per) * 100 if len(per) > 1 and min(per) > 0 else 0
        print(f'{tree}\t{tag}.{kind}\t' + '\t'.join(f'{p:,.2f}' for p in per) + f'\tspread {spread:.4f}%')
