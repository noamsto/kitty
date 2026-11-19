#!/usr/bin/env bash
# Usage: run_callgrind.sh VARIANT_NAME TREE MODE PAYLOAD COUNT [extra driver args...]
# Run inside nix-shell shell-m.nix. Env: RUN (run index, default 1), OUT (output dir).
# Appends one JSON line to $OUT/callgrind.jsonl.
set -euo pipefail

if [ "$#" -lt 5 ]; then
    echo "usage: $0 VARIANT_NAME TREE MODE PAYLOAD COUNT [extra driver args...]" >&2
    exit 2
fi
variant=$1 tree=$2 mode=$3 payload=$4 count=$5
shift 5

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out=${OUT:-$here/out}
run=${RUN:-1}
mkdir -p "$out"

extra="$*"
# the same name for runs that differ only in extra args must not collide
tag=$(printf '%s' "$extra" | tr -c 'A-Za-z0-9\n' '_')
name="$variant.$mode.$payload.$count${tag:+.$tag}.$run"
cg="$out/$name.cg"

cd "$tree"
PYTHONHASHSEED=0 valgrind --tool=callgrind --callgrind-out-file="$cg" \
    python3 "$here/spike_driver_lto.py" "$mode" --payload "$payload" --count "$count" --no-timing "$@" \
    >"$out/$name.driver.log" 2>&1

# total as callgrind_annotate prints it, to cross-check the .cg parser
ann_total=$(callgrind_annotate --inclusive=yes "$cg" | sed -n 's/^\([0-9,]*\) (100.0%) *PROGRAM TOTALS.*/\1/p' | tr -d ,)
line=$(python3 "$here/cg_extract_lto.py" "$cg" "$variant" "$mode" "$payload" "$count" "$extra" "$run")
cg_total=$(printf '%s' "$line" | sed -n 's/.*"total_ir": \([0-9]*\).*/\1/p')
if [ "$ann_total" != "$cg_total" ]; then
    echo "WARNING: callgrind_annotate total ($ann_total) != parsed total ($cg_total) for $name" >&2
fi
printf '%s\n' "$line" >>"$out/callgrind.jsonl"
printf '%s\n' "$line"
