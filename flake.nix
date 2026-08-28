{
  description = "Additional Nix packages and overlays";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    neomacs = {
      url = "github:eval-exec/neomacs/6def94af1c407027274a61c04356212b87a4c7ff";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      neomacs,
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
      shardxLauncherOverlay = final: _prev: {
        shardx-launcher = final.callPackage ./packages/shardx-launcher.nix { };
      };
      neomacsPackage = neomacs.packages.aarch64-darwin.neomacs;
      neomacsOverlay = final: _prev: {
        neomacs = neomacs.packages.${final.stdenv.hostPlatform.system}.neomacs;
      };
    in
    {
      packages.aarch64-darwin = rec {
        ego-lite = darwinPkgs.callPackage ./packages/ego-lite.nix { };
        lite-xl-app = darwinPkgs.callPackage ./packages/lite-xl-app.nix { };
        shardx-launcher = darwinPkgs.callPackage ./packages/shardx-launcher.nix { };
        neomacs = neomacsPackage;
        default = lite-xl-app;
      };

      overlays = {
        ego-lite = egoLiteOverlay;
        lite-xl-app = liteXlAppOverlay;
        shardx-launcher = shardxLauncherOverlay;
        neomacs = neomacsOverlay;
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

      darwinModules.shardx-launcher =
        { lib, ... }:
        {
          nixpkgs.overlays = lib.mkAfter [ shardxLauncherOverlay ];
        };

      darwinModules.neomacs =
        { lib, ... }:
        {
          nixpkgs.overlays = lib.mkAfter [ neomacsOverlay ];
        };
    };
}
