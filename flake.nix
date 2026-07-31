{
  description = "Additional Nix packages and overlays";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    {
      self,
      nixpkgs,
    }:
    let
      darwinPkgs = import nixpkgs {
        system = "aarch64-darwin";
        config.allowUnfree = true;
      };
      egoLiteOverlay = final: _prev: {
        ego-lite = final.callPackage ./packages/ego-lite.nix { };
      };
      liteXlAppOverlay = final: _prev: {
        lite-xl-app = final.callPackage ./packages/lite-xl-app.nix { };
      };
    in
    {
      packages.aarch64-darwin = rec {
        ego-lite = darwinPkgs.callPackage ./packages/ego-lite.nix { };
        lite-xl-app = darwinPkgs.callPackage ./packages/lite-xl-app.nix { };
        default = lite-xl-app;
      };

      overlays = {
        ego-lite = egoLiteOverlay;
        lite-xl-app = liteXlAppOverlay;
        default = self.overlays.lite-xl-app;
      };

      darwinModules.ego-lite =
        { lib, ... }:
        {
          nixpkgs.overlays = lib.mkAfter [ egoLiteOverlay ];
        };

      darwinModules.lite-xl-app =
        { lib, ... }:
        {
          nixpkgs.overlays = lib.mkAfter [ liteXlAppOverlay ];
        };
    };
}
