{
  fetchurl,
  lib,
  stdenvNoCC,
  undmg,
}:

let
  source = builtins.fromJSON (builtins.readFile ./shardx-launcher-source.json);
  appName =
    if lib.versionAtLeast source.version "2.1.0" then
      "ShardBrowser Launcher.app"
    else
      "ShardX Launcher.app";
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
    cp -R "${appName}" "$out/Applications/"

    runHook postInstall
  '';

  # Preserve the original upstream bundle and its signature without re-signing.
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
