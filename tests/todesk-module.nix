{
  lib,
  pkgs,
  module,
  expectedPackage,
}:

let
  evaluate =
    configuration:
    (lib.evalModules {
      modules = [
        {
          config._module.args.pkgs = pkgs;
          options.nixpkgs.overlays = lib.mkOption {
            type = with lib.types; listOf raw;
            default = [ ];
          };
          options.environment.systemPackages = lib.mkOption {
            type = with lib.types; listOf package;
            default = [ ];
          };
          options.system.activationScripts = lib.mkOption {
            type =
              with lib.types;
              attrsOf (submodule {
                options.text = lib.mkOption {
                  type = lines;
                  default = "";
                };
              });
            default = { };
          };
        }
        module
        configuration
      ];
    }).config;
  activation = config: config.system.activationScripts.postActivation.text;
  enabledWith =
    config: payload:
    config.programs.todesk.enable
    && lib.hasInfix (builtins.unsafeDiscardStringContext ("--package ${lib.escapeShellArg (toString payload)}")) (
      activation config
    )
    && !(lib.hasInfix "--disable" (activation config));
  disabled =
    config:
    !config.programs.todesk.enable
    && lib.hasInfix "--disable" (activation config)
    && !(lib.hasInfix "--package" (activation config));

  # These fixtures are evaluated only; no alternate app payload is built.
  replacementPayload = pkgs.runCommand "todesk-test-payload" { } ''mkdir -p "$out"'';
  selectionFor =
    name: payload: pkgs.runCommand name { passthru.todeskPayload = payload; } ''mkdir -p "$out"'';
  duplicateSelection = selectionFor "todesk-test-duplicate" pkgs.todesk.todeskPayload;
  replacementSelection = selectionFor "todesk-test-replacement" replacementPayload;
  conflictingSelections = [
    pkgs.todesk
    replacementSelection
  ];

  defaults = evaluate { };
  bare = evaluate { environment.systemPackages = [ pkgs.todesk ]; };
  explicitEnable = evaluate { programs.todesk.enable = true; };
  explicitDisable = evaluate {
    environment.systemPackages = [ pkgs.todesk ];
    programs.todesk.enable = false;
  };
  rawOverride = evaluate {
    environment.systemPackages = [ pkgs.todesk ];
    programs.todesk.package = replacementPayload;
  };
  wrapperOverride = evaluate {
    environment.systemPackages = [ pkgs.todesk ];
    programs.todesk.package = replacementSelection;
  };
  selectedReplacement = evaluate { environment.systemPackages = [ replacementSelection ]; };
  duplicate = evaluate {
    environment.systemPackages = [
      pkgs.todesk
      pkgs.todesk
      duplicateSelection
    ];
  };
  conflict = evaluate { environment.systemPackages = conflictingSelections; };
  conflictWithOverride = evaluate {
    environment.systemPackages = conflictingSelections;
    programs.todesk.package = replacementPayload;
  };
  conflictDisabled = evaluate {
    environment.systemPackages = conflictingSelections;
    programs.todesk.enable = false;
  };
  uiAutoStart = evaluate {
    environment.systemPackages = [ pkgs.todesk ];
    programs.todesk.uiAutoStart = true;
  };
  overlayPackage = (pkgs.appendOverlays defaults.nixpkgs.overlays).todesk;
  checks = {
    overlayMatchesPackage = overlayPackage.drvPath == expectedPackage.drvPath;
    absentDisables = disabled defaults;
    bareEnables = enabledWith bare pkgs.todesk.todeskPayload;
    bareSelectsWrapper = bare.programs.todesk.package.drvPath == pkgs.todesk.drvPath;
    explicitEnableWithoutSelection = enabledWith explicitEnable pkgs.todesk.todeskPayload;
    explicitDisableOverridesSelection = disabled explicitDisable;
    rawPackageOverride = enabledWith rawOverride replacementPayload;
    wrapperPackageOverride = enabledWith wrapperOverride replacementPayload;
    selectedPayloadIsUsed = enabledWith selectedReplacement replacementPayload;
    samePayloadSelectionsAreAllowed = enabledWith duplicate pkgs.todesk.todeskPayload;
    distinctPayloadsFail = !(builtins.tryEval (toString conflict.programs.todesk.package)).success;
    explicitPackageResolvesConflict = enabledWith conflictWithOverride replacementPayload;
    explicitDisableBypassesConflict = disabled conflictDisabled;
    uiDoesNotAutoStart = !(lib.hasInfix "--ui-auto-start" (activation bare));
    uiCanAutoStart =
      enabledWith uiAutoStart pkgs.todesk.todeskPayload
      && lib.hasInfix "--ui-auto-start" (activation uiAutoStart);
    moduleDoesNotSelectPackages = explicitEnable.environment.systemPackages == [ ];
  };
in
assert lib.all (check: check) (
  lib.mapAttrsToList (name: passed: lib.assertMsg passed "ToDesk module check failed: ${name}") checks
);
expectedPackage
