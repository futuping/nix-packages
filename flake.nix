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
      aarch64DarwinPkgs = import nixpkgs {
        system = "aarch64-darwin";
        config.allowUnfree = true;
      };
      egoLiteOverlay = final: _prev: {
        ego-lite = final.callPackage ./packages/ego-lite.nix { };
      };
      flogravityOverlay = final: _prev: {
        flogravity = final.callPackage ./packages/flogravity.nix { };
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
      packages.aarch64-darwin = {
        ego-lite = aarch64DarwinPkgs.callPackage ./packages/ego-lite.nix { };
        flogravity = aarch64DarwinPkgs.callPackage ./packages/flogravity.nix { };
        shardx-launcher = aarch64DarwinPkgs.callPackage ./packages/shardx-launcher.nix { };
        neomacs = neomacsPackage;
      };

      checks.aarch64-darwin = {
        flogravity-package = self.packages.aarch64-darwin.flogravity;
        flogravity-overlay = (aarch64DarwinPkgs.extend flogravityOverlay).flogravity;
      };

      overlays = {
        ego-lite = egoLiteOverlay;
        flogravity = flogravityOverlay;
        shardx-launcher = shardxLauncherOverlay;
        neomacs = neomacsOverlay;
      };

      darwinModules.ego-lite =
        { lib, ... }:
        {
          nixpkgs.overlays = lib.mkAfter [ egoLiteOverlay ];
        };

      darwinModules.flogravity =
        { lib, ... }:
        {
          nixpkgs.overlays = lib.mkAfter [ flogravityOverlay ];
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
