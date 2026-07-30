{
  fetchurl,
  lib,
  stdenvNoCC,
  undmg,
}:

let
  source = builtins.fromJSON (builtins.readFile ./lite-xl-app-source.json);
in
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "lite-xl";
  inherit (source) version;

  src = fetchurl {
    inherit (source) url hash;
  };

  nativeBuildInputs = [ undmg ];
  sourceRoot = ".";

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/Applications" "$out/bin"
    cp -R "Lite XL.app" "$out/Applications/"
    ln -s "$out/Applications/Lite XL.app/Contents/MacOS/lite-xl" "$out/bin/lite-xl"

    runHook postInstall
  '';

  # Preserve the complete ad-hoc signature shipped by upstream.
  dontFixup = true;

  meta = {
    description = "Lightweight text editor written in Lua";
    homepage = "https://lite-xl.com/";
    changelog = "https://github.com/lite-xl/lite-xl/releases/tag/v${finalAttrs.version}";
    license = lib.licenses.mit;
    mainProgram = "lite-xl";
    platforms = [ "aarch64-darwin" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
})
