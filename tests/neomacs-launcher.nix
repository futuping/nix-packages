{
  callPackage,
  runCommand,
  runCommandCC,
  writeShellScript,
  upstreamSource,
}:

let
  makeFixture =
    version:
    let
      program = writeShellScript "neomacs-fixture" ''
        printf '%s\n' ${version}
        printf '<%s>\n' "$@"
      '';
    in
    runCommand "neomacs-fixture-${version}" { inherit version; } ''
      mkdir -p "$out/bin" "$out/share"
      ln -s ${program} "$out/bin/neomacs"
    '';
  firstUpstream = makeFixture "first";
  secondUpstream = makeFixture "second";
  makeApplication =
    upstream:
    callPackage ../packages/neomacs.nix {
      inherit upstream upstreamSource;
    };
  first = makeApplication firstUpstream;
  second = makeApplication secondUpstream;
in
runCommandCC "neomacs-launcher-check" { } ''
  first="${first}/Applications/Neomacs.app"
  second="${second}/Applications/Neomacs.app"
  test "${first}" != "${second}"
  test "$(readlink "${first}/bin")" = "${firstUpstream}/bin"
  test "$(readlink "${second}/bin")" = "${secondUpstream}/bin"
  test "$(readlink "${first}/share")" = "${firstUpstream}/share"

  # Exercise argument boundaries and a simulated upstream-path change.
  test "$(PATH=/nonexistent "$first/Contents/MacOS/neomacs-launcher" 'space in argument' '--flag')" = \
    "$(printf '%s\n' first '<space in argument>' '<--flag>')"
  test "$(PATH=/nonexistent "$second/Contents/MacOS/neomacs-launcher" 'space in argument' '--flag')" = \
    "$(printf '%s\n' second '<space in argument>' '<--flag>')"
  for application in "$first" "$second"; do
    /usr/bin/plutil -lint "$application/Contents/Info.plist"
    /usr/bin/codesign --verify --deep --strict "$application"
    test -s "$application/Contents/Resources/neomacs.icns"
  done

  cp ${first.launcher} neomacs-launcher.m
  cp ${./neomacs-launcher-events.m} neomacs-launcher-events.m
  $CC -fobjc-arc -fblocks -Wall -Wextra -Werror -Os neomacs-launcher-events.m \
    -framework Cocoa -framework CoreServices -o events-check
  ./events-check
  mkdir -p "$out/bin"
  cp events-check "$out/bin/neomacs-launcher-events"
''
