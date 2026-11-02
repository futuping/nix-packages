{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.todesk;
  arguments =
    if cfg.enable then
      "--package ${lib.escapeShellArg (toString cfg.package)}"
      + lib.optionalString cfg.uiAutoStart " --ui-auto-start"
    else
      "--disable";
in
{
  options.programs.todesk = {
    enable = lib.mkEnableOption "the managed ToDesk application, service, and desktop session helper";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.todesk;
      defaultText = lib.literalExpression "pkgs.todesk";
      description = "ToDesk package containing the original Applications and Library payloads.";
    };
    uiAutoStart = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the ToDesk user interface when an Aqua session starts.";
    };
  };

  # Keep the module imported after disabling it so it can remove owned services
  # and binaries. User configuration and runtime data are retained.
  config.system.activationScripts.postActivation.text = lib.mkAfter ''
    ${pkgs.python314}/bin/python3 -I -B ${../scripts/manage_todesk.py} ${arguments}
  '';
}
