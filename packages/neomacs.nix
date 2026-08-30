{
  lib,
  libicns,
  replaceVars,
  runCommandCC,
  upstream,
  upstreamSource,
}:

let
  appVersion =
    (builtins.fromTOML (builtins.readFile "${upstreamSource}/Cargo.toml")).workspace.package.version;
  launcher = replaceVars ./neomacs-launcher.m {
    neomacsProgram = "${upstream}/bin/neomacs";
  };
  infoPlist = replaceVars ./neomacs-Info.plist.in {
    appVersion = lib.escapeXML appVersion;
    upstreamVersion = lib.escapeXML upstream.version;
  };
in
runCommandCC "neomacs-${upstream.version}"
  {
    inherit (upstream) version;
    nativeBuildInputs = [ libicns ];
    meta = (upstream.meta or { }) // {
      mainProgram = "neomacs";
      platforms = [ "aarch64-darwin" ];
    };
    passthru = {
      inherit upstream launcher;
    };
  }
  ''
    application="$out/Applications/Neomacs.app"
    mkdir -p "$application/Contents/MacOS" "$application/Contents/Resources"

    # Keep the CLI wrapper, pdump files and runtime resources exactly upstream.
    ln -s ${upstream}/bin "$out/bin"
    ln -s ${upstream}/share "$out/share"
    cp ${infoPlist} "$application/Contents/Info.plist"
    png2icns "$application/Contents/Resources/neomacs.icns" \
      ${upstreamSource}/assets/logo-128.png
    $CC -x objective-c -fobjc-arc -fblocks -Wall -Wextra -Werror -Os ${launcher} \
      -framework Cocoa \
      -o "$application/Contents/MacOS/neomacs-launcher"

    # Sign only our new launcher bundle. Never modify or re-sign Neomacs.
    /usr/bin/codesign --force --sign - --timestamp=none "$application"
    /usr/bin/codesign --verify --deep --strict "$application"
  ''
