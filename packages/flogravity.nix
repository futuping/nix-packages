{
  fetchurl,
  lib,
  stdenvNoCC,
  undmg,
}:

let
  source = builtins.fromJSON (builtins.readFile ./flogravity-source.json);
in
stdenvNoCC.mkDerivation {
  pname = "flogravity";
  inherit (source) version;

  src = fetchurl {
    inherit (source) url hash;
  };

  nativeBuildInputs = [ undmg ];
  sourceRoot = ".";

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/Applications"
    cp -R "浮引.app" "$out/Applications/"

    application="$out/Applications/浮引.app"
    info_plist="$application/Contents/Info.plist"
    executable="$application/Contents/MacOS/浮引"

    test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$info_plist")" = "me.vkr.fg"
    test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$info_plist")" = "${source.version}"
    test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$info_plist")" = "${source.bundleVersion}"
    test "$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "$info_plist")" = "${source.minimumSystemVersion}"
    test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$info_plist")" = "浮引"
    test "$(/usr/libexec/PlistBuddy -c 'Print :SUPublicEDKey' "$info_plist")" = "163IhAWnm86c3DmcO88e+QYx0+uL9DCgGMpz/ERge1M="

    architecture_count=0
    for architecture in $(/usr/bin/lipo -archs "$executable"); do
      case "$architecture" in
        arm64 | x86_64) ;;
        *)
          echo "unexpected FloGravity architecture: $architecture" >&2
          exit 1
          ;;
      esac
      ((architecture_count += 1))
    done
    test "$architecture_count" -eq 2

    signature_details="$(/usr/bin/codesign -d --verbose=4 "$application" 2>&1)"
    printf '%s\n' "$signature_details" | grep -Fqx 'TeamIdentifier=3MFNWTLLFG'
    /usr/bin/codesign --verify --deep --strict "$application"

    runHook postInstall
  '';

  # Preserve upstream's notarized Developer ID signature.
  dontFixup = true;

  meta = {
    description = "macOS productivity utility for quick actions and content workflows";
    homepage = "https://fg.vkr.me/mac";
    license = lib.licenses.unfree;
    platforms = [ "aarch64-darwin" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
}
