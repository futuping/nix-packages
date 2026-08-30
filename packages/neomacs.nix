{
  lib,
  libicns,
  replaceVars,
  runCommandCC,
  writeText,
  upstream,
  upstreamSource,
}:

let
  appVersion =
    (builtins.fromTOML (builtins.readFile "${upstreamSource}/Cargo.toml")).workspace.package.version;
  launcher = replaceVars ./neomacs-launcher.c {
    neomacsProgram = "${upstream}/bin/neomacs";
  };
  infoPlist = writeText "neomacs-Info.plist" ''
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>CFBundleName</key><string>Neomacs</string>
      <key>CFBundleDisplayName</key><string>Neomacs</string>
      <key>CFBundleIdentifier</key><string>org.neomacs.nix</string>
      <key>CFBundleExecutable</key><string>neomacs-launcher</string>
      <key>CFBundlePackageType</key><string>APPL</string>
      <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
      <key>CFBundleVersion</key><string>${lib.escapeXML appVersion}</string>
      <key>CFBundleShortVersionString</key><string>${lib.escapeXML appVersion}</string>
      <key>CFBundleGetInfoString</key><string>Neomacs ${lib.escapeXML upstream.version} (Nix)</string>
      <key>CFBundleIconFile</key><string>neomacs</string>
      <key>LSMinimumSystemVersion</key><string>12.0</string>
      <key>NSHighResolutionCapable</key><true/>
    </dict>
    </plist>
  '';
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
      inherit upstream;
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
    $CC -std=c99 -Wall -Wextra -Werror -Os ${launcher} \
      -o "$application/Contents/MacOS/neomacs-launcher"

    # Sign only our new launcher bundle. Never modify or re-sign Neomacs.
    /usr/bin/codesign --force --sign - --timestamp=none "$application"
    /usr/bin/codesign --verify --deep --strict "$application"
  ''
