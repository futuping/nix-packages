{
  lib,
  pkgs,
  module,
  expectedPackages,
  checkNeomacs ? false,
}:

let
  evaluate =
    selection:
    lib.evalModules {
      modules = [
        (
          { config, ... }:
          {
            # Use only overlays provided by the module under test, as the
            # nix-darwin package-set module does in the real consumer.
            config._module.args.pkgs = pkgs.appendOverlays config.nixpkgs.overlays;
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
        )
        module
        selection
      ];
    };
  absent = evaluate { };
  selected = evaluate (
    { pkgs, ... }:
    {
      environment.systemPackages = with pkgs; [ todesk ];
    }
  );
  catalog = absent._module.args.pkgs;
  activation = evaluated: evaluated.config.system.activationScripts.postActivation.text;
  payloadArgument = builtins.unsafeDiscardStringContext (
    "--package ${lib.escapeShellArg (toString expectedPackages.todesk.todeskPayload)}"
  );
  # Neomacs forces Darwin import-from-derivation on a cold Store. Its complete
  # equality probe runs after the native CI job realizes that dependency.
  binaryNames = builtins.filter (name: name != "neomacs") (builtins.attrNames expectedPackages);
  checks = {
    exportsWholeCatalog = lib.all (name: builtins.hasAttr name catalog) (
      builtins.attrNames expectedPackages
    );
    binaryDerivationsAreUnchanged = lib.all (
      name: catalog.${name}.drvPath == expectedPackages.${name}.drvPath
    ) binaryNames;
    noApplicationsSelectedByDefault = absent.config.environment.systemPackages == [ ];
    absentToDeskIsDisabled = !absent.config.programs.todesk.enable;
    absentToDeskRetainsCleanup = lib.hasInfix "--disable" (activation absent);
    bareToDeskEnablesManagement = selected.config.programs.todesk.enable;
    selectedToDeskUsesExpectedPayload = lib.hasInfix payloadArgument (activation selected);
    onlyOneApplicationSelected = builtins.length selected.config.environment.systemPackages == 1;
    onlySelectedApplicationIsToDesk =
      (builtins.head selected.config.environment.systemPackages).drvPath
      == expectedPackages.todesk.drvPath;
    activationRunsOnce =
      builtins.length (lib.splitString "manage_todesk.py" (activation selected)) == 2;
    neomacsDerivationIsUnchanged =
      !checkNeomacs || catalog.neomacs.drvPath == expectedPackages.neomacs.drvPath;
  };
in
assert lib.all (check: check) (
  lib.mapAttrsToList (
    name: passed: lib.assertMsg passed "Default Darwin module check failed: ${name}"
  ) checks
);
expectedPackages.todesk
