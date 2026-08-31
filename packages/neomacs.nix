{
  fetchurl,
  imagemagick,
  lib,
  libicns,
  replaceVars,
  runCommandCC,
  upstream,
  upstreamSource,
}:

let
  # User-selected artwork; pin the bytes independently of Neomacs updates.
  icon = fetchurl {
    url = "https://raw.githubusercontent.com/VSCodeEmacs/Emacs/8df69304a75d4a1a86894c6529331da406157333/images/icon.png";
    hash = "sha256-MXdRZJv1DKOnNKrOQcs85bYKFRRSRE3cTf8LrrmfI8w=";
  };
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
    nativeBuildInputs = [
      (lib.getBin imagemagick)
      libicns
    ];
    meta = (upstream.meta or { }) // {
      mainProgram = "neomacs";
      platforms = [ "aarch64-darwin" ];
    };
    passthru = {
      inherit upstream launcher icon;
    };
  }
  ''
    application="$out/Applications/Neomacs.app"
    mkdir -p "$application/Contents/MacOS" "$application/Contents/Resources"

    # Keep the CLI wrapper, pdump files and runtime resources exactly upstream.
    ln -s ${upstream}/bin "$out/bin"
    ln -s ${upstream}/share "$out/share"
    cp ${infoPlist} "$application/Contents/Info.plist"
    cp ${./neomacs-icon-NOTICE.txt} "$application/Contents/Resources/icon-NOTICE.txt"
    # The original PNG is 960x960; ICNS requires a supported square size.
    # Keep the converter pinned too, and exclude unrelated image metadata.
    magick ${icon} -resize 512x512 -strip PNG32:neomacs-icon-512.png
    png2icns "$application/Contents/Resources/neomacs.icns" \
      neomacs-icon-512.png
    $CC -x objective-c -fobjc-arc -fblocks -Wall -Wextra -Werror -Os ${launcher} \
      -framework Cocoa \
      -o "$application/Contents/MacOS/neomacs-launcher"

    # Sign only our new launcher bundle. Never modify or re-sign Neomacs.
    /usr/bin/codesign --force --sign - --timestamp=none "$application"
    /usr/bin/codesign --verify --deep --strict "$application"
  ''
