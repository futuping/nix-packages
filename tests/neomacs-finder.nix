{
  callPackage,
  python314,
  runCommand,
  writeScript,
  writeShellScript,
  upstreamSource,
}:

let
  program = writeScript "neomacs-finder-fixture" ''
    #!${python314}/bin/python3
    ${builtins.readFile ./neomacs-finder-fixture.py}
  '';
  upstream = runCommand "neomacs-finder-fixture" { version = "finder-test"; } ''
    mkdir -p "$out/bin" "$out/share"
    ln -s ${program} "$out/bin/neomacs"
  '';
  application = callPackage ../packages/neomacs.nix {
    inherit upstream upstreamSource;
  };
  eventCheck = callPackage ./neomacs-launcher.nix {
    inherit upstreamSource;
  };
in
writeShellScript "neomacs-finder-check" ''
  set -euo pipefail
  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONNOUSERSITE=1
  exec ${python314}/bin/python3 ${./check_neomacs_finder.py} \
    ${application}/Applications/Neomacs.app \
    ${eventCheck}/bin/neomacs-launcher-events
''
