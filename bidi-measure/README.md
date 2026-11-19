# kitty callgrind measurement kit

Measures the instruction count (callgrind, Ir) of kitty's own C code on a fixed set of payloads, for any two source
trees: the bidi branch against its base, with and without bidi mode. All commands run from this directory (fish
syntax) and use Nix; `shell-m.nix` provides kitty's build dependencies plus valgrind and brotli. The runs are
deterministic to a few parts per million (`PYTHONHASHSEED=0`, fixed font `LiberationMono-Regular.ttf`, which is the
file `kitty_tests` ships, fixed payload generators in `spike_payloads.py`); the screen is 120x50 cells unless a
scenario says otherwise.

The branch measured is `upstream/bidi` on the fork (`https://github.com/noamsto/kitty`); its base is upstream `gpu` at
`c789a4a96`. `<nixpkgs>` is not pinned, so compiler versions differ between machines (here gcc 16.2.0, clang 21.1.8):
compare the deltas between two trees built on the same machine, not absolute counts.

## Definitions

- **own Ir**: self Ir of `fast_data_types.so` (kitty's own code), summed from the callgrind output with `obj_ir.py`.
- **root Ir**: inclusive Ir of the measured root: `test_parse_written_data` for write (`parse_worker` is inlined),
  `screen_update_cell_data` for frame and idle. It includes HarfBuzz, FreeType, FriBidi and CPython calls made inside
  the root.
- **write**: parse the payload into an alternate screen, no rendering.
- **frame**: parse the payload in frames of at most 49 rows at 120x50 and render each frame with
  `test_screen_update_cell_data`.
- **per frame**: Ir divided by the number of rendered frames (the driver log prints `frames:`).
- **idle upload**: an upload with nothing changed: Ir(201 uploads) - Ir(1 upload), divided by 200, after the screen is
  filled and rendered once.

Root Ir includes CPython allocator and string-decoding work whose cost depends on buffer addresses and varies by up to
0.15% between runs and builds with identical kitty code. Compare own Ir first and read the root column with that in mind.

## Build a tree

A tree is a plain source directory in this directory (no git needed). The frame driver needs a hook the base does not
have (`test_screen_update_cell_data` in `kitty/fonts.c`, which the bidi branch carries), so a base tree gets
`u-shim.patch`.

    ./mktree.sh <REPO> <REF> $PWD/<NAME> [$PWD/u-shim.patch]   # git archive of REF from the checkout REPO, patch -p1
    git -C $REPO fetch https://github.com/noamsto/kitty upstream/bidi   # then FETCH_HEAD is the branch tip
    ./mktree.sh $REPO c789a4a96 $PWD/B $PWD/u-shim.patch; and ./mktree.sh $REPO FETCH_HEAD $PWD/G   # base and branch
    ./build.sh <NAME> lto gcc                                  # LTO build with gcc
    ./build.sh <NAME> lto clang                                # LTO build with clang, via shell-m-clang.nix
    ./build.sh <NAME> nolto                                    # -g, no LTO, for symbol-level work

`<NAME>` is any name; `mktree.sh` needs the directory as an absolute path. Check `<NAME>.build.exit` is `0`;
`<NAME>.sha` is the sha256 of the built `fast_data_types.so`. Use a separate tree per compiler (`B` for gcc, `Bc` for
clang), never rebuild a tree in place with the other compiler. The compiler is in the `.so` comment:

    nix-shell shell-m.nix --run "readelf -p .comment <NAME>/kitty/fast_data_types.so"

## Payloads

`gen_cmds.py` runs 12 payloads (generators in `spike_payloads.py`) with no escape sequences beyond what the text needs:
`plain_lines` (160000), `ascii`, `unicode`, `scrollback`, `unique_unicode`, `csi`, `long_escape_codes`,
`truecolor_text`, `palette16_text`, `halfblocks` (500000), `images`, `cjk`. Counts are for write (1000000 unless
given); frame uses 500000 except `plain_lines` (160000). `render` in the generator table is an alias of `ascii` and is
not run.

Bidi payloads: `hebrew`, `arabic`, `mixed_rows`, `mixed_wrapped`, `nested_brackets`, `para_screen`, `zw_marks`,
`isolate_prompt`, `isolate_lri`, `lrm_lines` (each line starts with LRM: the column-0 LRM path), `fsi_lines` (each line
is FSI at column 0, 80 plain characters, PDI), with `plain_lines` as their control. The driver option `--preamble-bdsm` sends `CSI 8 h`
(bidi mode on) before the payload. In bidi mode only LRM and RLM are kept at column 0, so `fsi_lines` and `isolate_prompt` measure isolates that are
dropped there. `scen.py` builds the idle scenarios: a 1 MB paragraph scrolled back 500 rows with
output below it (200x60), and a bidi line scrolled into the history with 300 plain lines after it (120x50, with
`pixel_scroll` on and off).

## Run

One run (inside `nix-shell shell-m.nix`; appends a JSON line to `out/callgrind.jsonl`):

    RUN=1 ./run_callgrind_lto.sh <NAME> $PWD/<NAME> write ascii 1000000
    RUN=1 ./run_callgrind_lto.sh <NAME> $PWD/<NAME> frame hebrew 500000 --preamble-bdsm
    RUN=1 ./run_scen.sh <NAME> $PWD/<NAME> long_para_scrolled 201

A set of runs for a tree is a command list, run 12 at a time:

    python3 gen_cmds.py <NAME> base > cmds     # 12 payloads x write/frame x 2 runs (48 runs)
    python3 gen_cmds.py <NAME> idle >> cmds    # idle uploads and the scrolled-back / stamped-history cases, bidi mode off
    python3 gen_cmds.py <NAME> off >> cmds     # RTL, mixed, LRM and FSI payloads with bidi mode off
    python3 gen_cmds.py <NAME> bidi >> cmds    # the same with bidi mode on (a tree with bidi support only)
    nice -n 10 nix-shell shell-m.nix --run "bash $PWD/_runset.sh $PWD/cmds $PWD/out"
    test -s out/fail.log; and cat out/fail.log     # must not exist or be empty

Extra driver arguments are appended after the count; the run name then carries them and `compare.py` skips such runs.
A "symbols missing" warning in `out/stderr.log` is expected under LTO (an inlined root, or `render_line` inlined).

## Check one figure

Two trees, one payload, write mode (inside `nix-shell shell-m.nix` for the runs and `obj_ir.py`; `$REPO` is a checkout
with the branch):

    ./mktree.sh $REPO c789a4a96 $PWD/B $PWD/u-shim.patch; and ./mktree.sh $REPO FETCH_HEAD $PWD/G
    ./build.sh B lto gcc; and ./build.sh G lto gcc; cat B.build.exit G.build.exit      # both 0
    nix-shell shell-m.nix --run "RUN=1 ./run_callgrind_lto.sh B $PWD/B write ascii 1000000; RUN=1 ./run_callgrind_lto.sh G $PWD/G write ascii 1000000"
    nix-shell shell-m.nix --run "python3 obj_ir.py out/B.write.ascii.1000000.1.cg out/G.write.ascii.1000000.1.cg"

`obj_ir.py` takes any number of `.cg` files and prints, per file, the self Ir of `fast_data_types.so` (own Ir) and of
everything else; the figure is the difference of the two own Ir.

## Tables

    python3 compare.py <BASE> <NEW> [OUTDIR]   # no-sequence write and frame: own Ir, root Ir, deltas, run spread
    nix-shell shell-m.nix --run "python3 extra_tables.py [OUTDIR]"   # idle, scrolled-back, bidi-on and LRM/FSI tables

`<BASE>` and `<NEW>` are tree names as in `OUTDIR/callgrind.jsonl`. `OUTDIR` defaults to `out` in this directory (the
`OUT` of `run_callgrind_lto.sh` and the second argument of `_runset.sh`). `extra_tables.py` reads the tree names from
the environment: `BASE` (default `B`) and `NEW` (default `F`), for example `BASE=B NEW=G python3 extra_tables.py out`.

Own Ir per `.cg` is cached in `out/own_ir.json`. Function-level diffs (inside `nix-shell shell-m.nix`):

    python3 fn_diff.py out/<BASE>.write.ascii.1000000.1.cg out/<NEW>.write.ascii.1000000.1.cg 15 fast_data_types
    python3 fn_per_upload.py out/<NEW>.scen.<case>.1.1.cg out/<NEW>.scen.<case>.201.1.cg 8   # Ir per idle upload

## Output identity

    nix-shell shell-m.nix --run "./crc_frames.sh B" > crc-B.txt; nix-shell shell-m.nix --run "./crc_frames.sh G" > crc-G.txt
    diff crc-B.txt crc-G.txt                    # no output: the rendered cells of 18 payloads are identical, bidi mode off

`crc_frames.sh` prints, per payload, the crc32 of every frame's `test_screen_update_cell_data` bytes, the frame count and
the number of non-blank cells.

## Disassemble and diff

    nix-shell shell-m.nix --run "$PWD/dis_all.sh <NAME>"      # writes dis-<NAME>-<fn>.txt
    nix-shell shell-m.nix --run "./dis.sh <NAME>/kitty/fast_data_types.so draw_text.lto_priv.0" > dis-<NAME>.txt

`dis.sh` masks addresses and call/jump targets so a diff of two builds shows only instruction changes. LTO suffixes
such as `.lto_priv.0` can change between builds; check with `nm`.
