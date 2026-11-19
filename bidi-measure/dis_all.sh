#!/usr/bin/env bash
# dis_all.sh VARIANT: masked disassembly of the draw-text functions of the kit directory/VARIANT -> dis-VARIANT-<fn>.txt. Run inside nix-shell (needs objdump).
set -u
M=$(dirname "$(readlink -f "$0")")
v=$1 so=$M/$1/kitty/fast_data_types.so
for f in draw_text.lto_priv.0 draw_text_implicit.lto_priv.0 draw_text_implicit draw_text_loop_implicit draw_text_loop_implicit.cold screen_draw_text add_combining_chars add_combining_char.lto_priv.0 draw_text_slow; do
  "$M/dis.sh" "$so" "$f" > "$M/dis-$v-$f.txt"
  [ -s "$M/dis-$v-$f.txt" ] || rm "$M/dis-$v-$f.txt"
done
