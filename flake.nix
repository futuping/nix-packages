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
      maintainerSystems = [
        "aarch64-darwin"
        "x86_64-linux"
      ];
      forMaintainerSystems = nixpkgs.lib.genAttrs maintainerSystems;

      aarch64DarwinPkgs = import nixpkgs {
        system = "aarch64-darwin";
        config.allowUnfree = true;
      };
      maintainerFor =
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python314;
          pythonEnvironment = ''
            export PYTHONDONTWRITEBYTECODE=1
            export PYTHONNOUSERSITE=1
          '';
          maintainerCheck = pkgs.writeShellScript "nix-packages-maintainer-check" ''
            set -euo pipefail
            ${pythonEnvironment}
            cd ${self}
            ${python}/bin/python3 --version
            exec ${python}/bin/python3 -m unittest discover -s tests
          '';
          makeUpdaterApp =
            name: script: source:
            let
              program = pkgs.writeShellScript "nix-packages-${name}" ''
                set -euo pipefail
                ${pythonEnvironment}

                repository_root="''${NIX_PACKAGES_REPOSITORY:-$PWD}"
                source_path="$repository_root/${source}"
                if [[ ! -f "$repository_root/flake.nix" || ! -f "$source_path" ]]; then
                  echo "error: run this updater from the nix-packages repository root" >&2
                  exit 2
                fi

                exec ${python}/bin/python3 ${script} "$@" --source "$source_path"
              '';
            in
            {
              type = "app";
              program = "${program}";
            };
        in
        {
          devShell = pkgs.mkShellNoCC {
            packages = [ python ];
            shellHook = pythonEnvironment;
          };
          checkApp = {
            type = "app";
            program = "${maintainerCheck}";
          };
          updaterApps = nixpkgs.lib.optionalAttrs pkgs.stdenv.isDarwin {
            update-ego-lite =
              makeUpdaterApp "update-ego-lite" ./scripts/update_ego_lite.py
                "packages/ego-lite-source.json";
            update-flogravity =
              makeUpdaterApp "update-flogravity" ./scripts/update_flogravity.py
                "packages/flogravity-source.json";
            update-shardx-launcher =
              makeUpdaterApp "update-shardx-launcher" ./scripts/update_shardx_launcher.py
                "packages/shardx-launcher-source.json";
          };
        };
      maintainer = forMaintainerSystems maintainerFor;
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

      devShells = forMaintainerSystems (system: {
        default = maintainer.${system}.devShell;
        maintainer = maintainer.${system}.devShell;
      });

      apps = forMaintainerSystems (
        system:
        {
          maintainer-check = maintainer.${system}.checkApp;
        }
        // maintainer.${system}.updaterApps
      );

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
