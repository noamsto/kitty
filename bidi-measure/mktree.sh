#!/usr/bin/env bash
# Usage: mktree.sh REPO REF DIR [PATCH]. Extracts REF of the git checkout REPO into DIR and applies PATCH (-p1).
set -u
WT=$1
ref=$2 dir=$3 patch=${4:-}
commit=$(git -C "$WT" rev-parse "$ref") || exit 1
mkdir "$dir" || exit 1
git -C "$WT" archive "$ref" | tar -x -C "$dir" || exit 1
if [ -n "$patch" ]; then
  patch=$(readlink -f "$patch")
  patch -p1 --no-backup-if-mismatch -d "$dir" < "$patch" || { echo "patch failed" >&2; exit 1; }
  find "$dir" -name '*.rej' -o -name '*.orig' | grep -q . && { echo "rejects present" >&2; exit 1; }
fi
{ echo "ref: $ref"; echo "commit: $commit"; echo "patch: ${patch:-none}"; } > "$dir.src.txt"
