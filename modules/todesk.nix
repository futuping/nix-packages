{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.todesk;
  selections = builtins.filter (
    package: builtins.isAttrs package && package ? todeskPayload
  ) config.environment.systemPackages;
  selectedPayloads = lib.unique (map (package: toString package.todeskPayload) selections);
  selectedPackage =
    if selections == [ ] then
      pkgs.todesk
    else if builtins.length selectedPayloads == 1 then
      builtins.head selections
    else
      throw "ToDesk: multiple distinct payloads selected in environment.systemPackages; select one ToDesk package.";
  payload =
    if builtins.isAttrs cfg.package && cfg.package ? todeskPayload then
      cfg.package.todeskPayload
    else
      cfg.package;
  arguments =
    if cfg.enable then
      "--package ${lib.escapeShellArg (toString payload)}"
      + lib.optionalString cfg.uiAutoStart " --ui-auto-start"
    else
      "--disable";
in
{
  options.programs.todesk = {
    enable =
      lib.mkEnableOption "the managed ToDesk application, service, and desktop session helper"
      // {
        default = selections != [ ];
        defaultText = lib.literalExpression "a ToDesk package is selected in environment.systemPackages";
      };
    package = lib.mkOption {
      type = lib.types.package;
      default = selectedPackage;
      defaultText = lib.literalExpression "the selected ToDesk package, or pkgs.todesk";
      description = "ToDesk selection package or a raw package containing the original Applications and Library payloads.";
    };
    uiAutoStart = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the ToDesk user interface when an Aqua session starts.";
    };
  };

  # Keep the module imported after removing the package so it can remove owned services
  # and binaries. User configuration and runtime data are retained.
  config.system.activationScripts.postActivation.text = lib.mkAfter ''
    ${pkgs.python314}/bin/python3 -I -B ${../scripts/manage_todesk.py} ${arguments}
  '';
}
