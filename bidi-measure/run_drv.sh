#!/usr/bin/env bash
# Usage: run_drv.sh VARIANT TREE DRIVER TAG KIND N   (inside nix-shell shell-m.nix; env RUN, OUT as run_callgrind_lto.sh)
# Runs DRIVER KIND N <this directory> (a driver here, such as region_driver.py) under callgrind in TREE, writes
# $OUT/VARIANT.TAG.KIND.N.RUN.cg and appends a JSON line (mode TAG, payload KIND, count N).
set -euo pipefail
variant=$1 tree=$2 driver=$3 tag=$4 kind=$5 n=$6
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out=${OUT:-$here/out}
run=${RUN:-1}
mkdir -p "$out"
name="$variant.$tag.$kind.$n.$run"
cg="$out/$name.cg"
cd "$tree"
PYTHONHASHSEED=0 valgrind --tool=callgrind --callgrind-out-file="$cg" \
    python3 "$here/$driver" "$kind" "$n" "$here" >"$out/$name.driver.log" 2>&1
python3 "$here/cg_extract_lto.py" "$cg" "$variant" "$tag" "$kind" "$n" "" "$run" 2>/dev/null >>"$out/callgrind.jsonl"
