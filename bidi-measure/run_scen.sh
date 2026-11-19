#!/usr/bin/env bash
# Usage: run_scen.sh VARIANT TREE CASE FRAMES   (inside nix-shell shell-m.nix; env RUN, OUT as run_callgrind_lto.sh)
# Runs scen.py CASE FRAMES under callgrind in TREE and appends a JSON line (mode scen, payload CASE, count FRAMES).
set -euo pipefail
variant=$1 tree=$2 case=$3 frames=$4
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out=${OUT:-$here/out}
run=${RUN:-1}
mkdir -p "$out"
name="$variant.scen.$case.$frames.$run"
cg="$out/$name.cg"
cd "$tree"
PYTHONHASHSEED=0 valgrind --tool=callgrind --callgrind-out-file="$cg" \
    python3 "$here/scen.py" "$case" "$frames" >"$out/$name.driver.log" 2>&1
python3 "$here/cg_extract_lto.py" "$cg" "$variant" scen "$case" "$frames" "" "$run" 2>/dev/null >>"$out/callgrind.jsonl"
