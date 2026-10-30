{
  fetchurl,
  lib,
  stdenvNoCC,
  unzip,
}:

let
  source = builtins.fromJSON (builtins.readFile ./wordcraft-cli-source.json);
in
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "wordcraft-cli";
  inherit (source) version;

  src = fetchurl {
    inherit (source) url hash;
  };

  nativeBuildInputs = [ unzip ];
  sourceRoot = "wordcraft-cli-${finalAttrs.version}-macos-universal";

  installPhase = ''
    runHook preInstall

    install -Dm755 wordcraft-cli "$out/bin/wordcraft-cli"
    mkdir -p "$out/share/doc/wordcraft-cli"
    for document in README.md ATTRIBUTION.md NOTICE LICENSE LICENSE-MIT LICENSE-APACHE COPYRIGHT OFL-*.txt; do
      if [ -f "$document" ]; then
        cp "$document" "$out/share/doc/wordcraft-cli/"
      fi
    done

    runHook postInstall
  '';

  # Preserve the signed upstream universal Mach-O, including both slices.
  dontFixup = true;

  meta = {
    description = "Headless WordCraft document editor, converter, and MCP server";
    homepage = "https://github.com/storytold/wordcraft";
    changelog = "https://github.com/storytold/wordcraft/releases/tag/v${finalAttrs.version}";
    license = with lib.licenses; [
      mit
      asl20
    ];
    mainProgram = "wordcraft-cli";
    platforms = [ "aarch64-darwin" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
})
