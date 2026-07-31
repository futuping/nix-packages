{
  fetchurl,
  lib,
  stdenvNoCC,
  undmg,
}:

let
  source = builtins.fromJSON (builtins.readFile ./ego-lite-source.json);
in
stdenvNoCC.mkDerivation {
  pname = "ego-lite";
  inherit (source) version;

  src = fetchurl {
    inherit (source) url hash;
  };

  nativeBuildInputs = [ undmg ];
  sourceRoot = ".";

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/Applications" "$out/bin"
    cp -R "ego lite.app" "$out/Applications/"

    ln -s \
      "$out/Applications/ego lite.app/Contents/Frameworks/ego Framework.framework/Versions/Current/Helpers/ego-browser" \
      "$out/bin/ego-browser"

    runHook postInstall
  '';

  # Preserve upstream's notarized Developer ID signature.
  dontFixup = true;

  meta = {
    description = "Browser designed for people and AI agents to work in parallel";
    homepage = "https://lite.ego.app/";
    license = lib.licenses.unfree;
    mainProgram = "ego-browser";
    platforms = [ "aarch64-darwin" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
}
