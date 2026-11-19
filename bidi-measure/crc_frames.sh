#!/usr/bin/env bash
# crc_frames.sh TREE: crc32 of the cell data of every rendered frame (test_screen_update_cell_data), per payload, bidi mode off.
# Run inside nix-shell shell-m.nix; the output of two trees is equal when the rendered cells are identical.
set -u
M=$(dirname "$(readlink -f "$0")")
cd "$M/$1" || exit 1
for p in plain_lines ascii unicode cjk truecolor_text palette16_text halfblocks unique_unicode scrollback csi long_escape_codes images \
         hebrew arabic mixed_rows lrm_lines fsi_lines isolate_prompt; do
  printf '%s ' "$p"
  PYTHONHASHSEED=0 python3 "$M/spike_driver_lto.py" frame --runs 1 --warmup 0 --json --payload "$p" --count 100000 2>/dev/null |
    python3 -c 'import json,sys; d=json.loads(sys.stdin.read().strip().splitlines()[-1]); print(d["crc32"], d["frames"], d.get("nonblank_cells"))'
done
