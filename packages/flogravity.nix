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

    # undmg truncates the high bytes of the two HFS Unicode names in this
    # image. Identify the bundle structurally, then restore the names declared
    # by Info.plist instead of depending on the corrupted extracted names.
    shopt -s nullglob
    source_applications=( ./*.app )
    application_count="''${#source_applications[@]}"
    if (( application_count != 1 )); then
      echo "expected exactly one top-level FloGravity application, found $application_count" >&2
      exit 1
    fi
    source_application="''${source_applications[0]}"

    mkdir -p "$out/Applications"
    application="$out/Applications/浮引.app"
    cp -R "$source_application" "$application"

    info_plist="$application/Contents/Info.plist"
    executable="$application/Contents/MacOS/浮引"

    test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$info_plist")" = "me.vkr.fg"
    test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$info_plist")" = "${source.version}"
    test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$info_plist")" = "${source.bundleVersion}"
    test "$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "$info_plist")" = "${source.minimumSystemVersion}"
    test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$info_plist")" = "浮引"
    test "$(/usr/libexec/PlistBuddy -c 'Print :SUPublicEDKey' "$info_plist")" = "163IhAWnm86c3DmcO88e+QYx0+uL9DCgGMpz/ERge1M="

    extracted_executables=( "$application"/Contents/MacOS/* )
    executable_count="''${#extracted_executables[@]}"
    if (( executable_count != 1 )); then
      echo "expected exactly one FloGravity main executable, found $executable_count" >&2
      exit 1
    fi
    extracted_executable="''${extracted_executables[0]}"
    if [[ ! -f "$extracted_executable" || ! -x "$extracted_executable" ]]; then
      echo "FloGravity main executable is not an executable regular file" >&2
      exit 1
    fi
    if [[ "$extracted_executable" != "$executable" ]]; then
      mv "$extracted_executable" "$executable"
    fi

    # The extracted bytes are unchanged: only undmg's truncated HFS Unicode
    # names need restoring for the original Developer ID seal to validate.
    signature_details="$(
      /usr/bin/codesign -d --verbose=4 "$application" 2>&1
    )"
    if ! printf '%s\n' "$signature_details" | grep -Fqx 'Identifier=me.vkr.fg'; then
      echo "unexpected FloGravity signing identifier" >&2
      exit 1
    fi
    if ! printf '%s\n' "$signature_details" | grep -Fqx 'TeamIdentifier=3MFNWTLLFG'; then
      echo "unexpected FloGravity signing Team ID" >&2
      exit 1
    fi
    if ! printf '%s\n' "$signature_details" | grep -Fqx 'Authority=Developer ID Application: JUN LIU (3MFNWTLLFG)'; then
      echo "unexpected FloGravity signing authority" >&2
      exit 1
    fi
    if printf '%s\n' "$signature_details" | grep -Fqx 'Signature=adhoc'; then
      echo "FloGravity must retain its upstream Developer ID signature" >&2
      exit 1
    fi
    if ! printf '%s\n' "$signature_details" | grep -Eq '^CodeDirectory .*flags=.*runtime'; then
      echo "FloGravity signature is missing the hardened runtime" >&2
      exit 1
    fi

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

    /usr/bin/codesign --verify --deep --strict --verbose=2 "$application"

    # These upstream-signed entitlements back FloGravity's integrations. Keep
    # them intact instead of replacing the signature with an ad-hoc one that
    # macOS would reject for containing restricted entitlements.
    main_entitlements="$(/usr/bin/codesign -d --entitlements - "$application" 2>/dev/null)"
    for entitlement in \
      com.apple.developer.aps-environment \
      com.apple.developer.associated-domains \
      com.apple.developer.icloud-container-identifiers \
      com.apple.developer.icloud-services \
      com.apple.security.application-groups \
      com.apple.security.automation.apple-events \
      com.apple.security.device.audio-input \
      com.apple.security.device.camera
    do
      if ! printf '%s\n' "$main_entitlements" | grep -Fq "$entitlement"; then
        echo "missing FloGravity entitlement: $entitlement" >&2
        exit 1
      fi
    done
    printf '%s\n' "$main_entitlements" | grep -Fq 'group.me.vkr.fg'

    for extension_name in FuGuangFinderSync.appex FuGuangWidgets.appex; do
      extension="$application/Contents/PlugIns/$extension_name"
      test -d "$extension"
      extension_entitlements="$(/usr/bin/codesign -d --entitlements - "$extension" 2>/dev/null)"
      printf '%s\n' "$extension_entitlements" | grep -Fq 'com.apple.security.app-sandbox'
      printf '%s\n' "$extension_entitlements" | grep -Fq 'com.apple.security.application-groups'
      printf '%s\n' "$extension_entitlements" | grep -Fq 'group.me.vkr.fg'
    done

    runHook postInstall
  '';

  # Keep the verified upstream Developer ID signature intact after restoring
  # the two HFS Unicode names.
  dontFixup = true;

  meta = {
    description = "macOS productivity utility for quick actions and content workflows";
    homepage = "https://fg.vkr.me/mac";
    license = lib.licenses.unfree;
    platforms = [ "aarch64-darwin" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
}
