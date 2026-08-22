{
  fetchurl,
  lib,
  stdenvNoCC,
  undmg,
}:

let
  source = builtins.fromJSON (builtins.readFile ./shardx-launcher-source.json);
in
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "shardx-launcher";
  inherit (source) version;

  src = fetchurl {
    inherit (source) url hash;
  };

  nativeBuildInputs = [ undmg ];
  sourceRoot = ".";

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/Applications"
    cp -R "ShardX Launcher.app" "$out/Applications/"

    runHook postInstall
  '';

  # Upstream distributes this application without a Developer ID signature.
  # Avoid Nix fixups changing the unmodified upstream bundle.
  dontFixup = true;

  meta = {
    description = "Open-source anti-detect browser launcher";
    homepage = "https://github.com/ProxyShard/ShardBrowser";
    changelog = "https://github.com/ProxyShard/ShardBrowser/releases/tag/v${finalAttrs.version}";
    license = lib.licenses.mit;
    platforms = [ "aarch64-darwin" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
})
