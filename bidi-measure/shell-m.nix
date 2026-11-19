# kitty's shell.nix environment plus valgrind (callgrind) and brotli, which the measurement builds need.
{pkgs ? import <nixpkgs> {}}:
let
  base = import ./shell-base.nix {inherit pkgs;};
in
  base.overrideAttrs (o: {
    buildInputs = o.buildInputs ++ [pkgs.brotli pkgs.valgrind];
  })
