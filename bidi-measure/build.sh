#!/usr/bin/env bash
# Usage: build.sh DIR lto|nolto [gcc|clang]   (DIR relative to this directory; default compiler gcc)
set -u
M=$(dirname "$(readlink -f "$0")")
dir=$M/$1 mode=$2 cc=${3:-gcc}
shell=$M/shell-m.nix
[ "$cc" = clang ] && shell=$M/shell-m-clang.nix
if [ "$mode" = nolto ]; then export KITTY_NO_LTO=1 CFLAGS=-g; fi
(cd "$dir" && nix-shell "$shell" --run 'python3 setup.py build --verbose --ignore-compiler-warnings') > "$dir.build.log" 2>&1
rc=$?
echo $rc > "$dir.build.exit"
[ $rc -eq 0 ] && sha256sum "$dir/kitty/fast_data_types.so" > "$dir.sha"
exit $rc
