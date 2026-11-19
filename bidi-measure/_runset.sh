#!/usr/bin/env bash
# usage: _runset.sh CMDS OUTDIR  (inside nix-shell)
set -u
export OUT; OUT=$(realpath -m "$2")
mkdir -p "$OUT"
# shellcheck disable=SC2016
xargs -P 12 -d '\n' -I{} bash -c '{} >/dev/null 2>>"$OUT/stderr.log"; rc=$?; [ $rc -eq 0 ] || echo "FAIL rc=$rc: {}" >>"$OUT/fail.log"' <"$1"
