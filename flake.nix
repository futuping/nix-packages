{
  description = "Additional Nix packages and overlays";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    {
      self,
      nixpkgs,
    }:
    let
      liteXlAppOverlay = final: _prev: {
        lite-xl-app = final.callPackage ./packages/lite-xl-app.nix { };
      };
    in
    {
      packages.aarch64-darwin = rec {
        lite-xl-app = nixpkgs.legacyPackages.aarch64-darwin.callPackage ./packages/lite-xl-app.nix { };
        default = lite-xl-app;
      };

      overlays = {
        lite-xl-app = liteXlAppOverlay;
        default = self.overlays.lite-xl-app;
      };

      darwinModules.lite-xl-app =
        { lib, ... }:
        {
          nixpkgs.overlays = lib.mkAfter [ liteXlAppOverlay ];
        };
    };
}
