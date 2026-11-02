{
  cpio,
  gzip,
  lib,
  requireFile,
  runCommand,
  stdenvNoCC,
  xar,
}:

let
  source = builtins.fromJSON (builtins.readFile ./todesk-source.json);
  payload = stdenvNoCC.mkDerivation {
    pname = "todesk";
    inherit (source) version;

    # The official CDN requires interactive verification. Keep a reviewed hash,
    # without persisting browser credentials or redistributing the proprietary PKG.
    src = requireFile {
      name = "ToDesk_${source.version}.pkg";
      inherit (source) hash;
      message = ''
        Download ${source.url} in a browser and complete the official verification.
        Import the downloaded file with:
          nix-store --add-fixed sha256 /path/to/ToDesk_${source.version}.pkg
        Expected SHA-256: ${source.hash}
      '';
    };

    nativeBuildInputs = [
      xar
      gzip
      cpio
    ];

    unpackPhase = ''
      runHook preUnpack
      xar -xf "$src"
      mkdir payload
      cd payload
      gzip -dc ../ToDesk.pkg/Payload | cpio -idm --no-absolute-filenames
      runHook postUnpack
    '';

    installPhase = ''
      runHook preInstall
      test -x Applications/ToDesk.app/Contents/MacOS/ToDesk
      test -x Applications/ToDesk.app/Contents/MacOS/ToDesk_Service
      test -f Library/LaunchDaemons/com.youqu.todesk.service.plist
      test -f Library/LaunchAgents/com.youqu.todesk.session.plist
      mkdir -p "$out"
      cp -R Applications Library "$out/"
      runHook postInstall
    '';

    # Preserve the Developer ID signatures. Do not run the vendor's installer,
    # telemetry, uninstaller/watchers, or services inside the build sandbox.
    dontFixup = true;

    meta = {
      description = "ToDesk remote desktop payload for the managed Darwin service";
      homepage = "https://www.todesk.com/";
      license = lib.licenses.unfree;
      platforms = [ "aarch64-darwin" ];
      sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
    };
  };
in
# Selection keeps the signed payload in the closure without exposing a second,
# immutable app through nix-darwin's Applications environment. The lifecycle
# module uses the marker to deploy the writable application and its services.
runCommand "todesk-selection-${source.version}"
  {
    pname = "todesk";
    inherit (source) version;
    inherit (payload) meta;
    passthru.todeskPayload = payload;
  }
  ''
    mkdir -p "$out/share/todesk"
    ln -s ${payload} "$out/share/todesk/payload"
  ''
