# shell-m.nix with clang (and lld/llvm for LTO) selected through CC.
{pkgs ? import <nixpkgs> {}}:
let
  base = import ./shell-m.nix {inherit pkgs;};
in
  base.overrideAttrs (o: {
    nativeBuildInputs = o.nativeBuildInputs ++ [pkgs.clang pkgs.lld pkgs.llvm];
    shellHook = o.shellHook + "\nexport CC=clang\n";
  })
