{
  description = "Additional Nix packages and overlays";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    neomacs = {
      url = "github:eval-exec/neomacs/main";
      # Reuse upstream's locked build dependencies as well as its package.
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
      flogravityPackageCheck = aarch64DarwinPkgs.writeShellScript "flogravity-package-check" ''
        set -euo pipefail
        export LC_ALL=C

        application="${self.packages.aarch64-darwin.flogravity}/Applications/浮引.app"
        signature_details="$(/usr/bin/codesign -d --verbose=4 "$application" 2>&1)"

        printf '%s\n' "$signature_details" | ${aarch64DarwinPkgs.gnugrep}/bin/grep -Fqx \
          'Authority=Developer ID Application: JUN LIU (3MFNWTLLFG)'
        printf '%s\n' "$signature_details" | ${aarch64DarwinPkgs.gnugrep}/bin/grep -Fqx \
          'TeamIdentifier=3MFNWTLLFG'
        printf '%s\n' "$signature_details" | ${aarch64DarwinPkgs.gnugrep}/bin/grep -Eq \
          '^CodeDirectory .*flags=.*runtime'
        if printf '%s\n' "$signature_details" | ${aarch64DarwinPkgs.gnugrep}/bin/grep -Fqx \
          'Signature=adhoc'; then
          echo 'FloGravity unexpectedly has an ad-hoc signature' >&2
          exit 1
        fi

        /usr/bin/codesign --verify --deep --strict --verbose=4 "$application"
        gatekeeper="$(
          /usr/sbin/spctl --assess --type execute --verbose=4 "$application" 2>&1
        )"
        printf '%s\n' "$gatekeeper" | ${aarch64DarwinPkgs.gnugrep}/bin/grep -Fqx \
          'source=Notarized Developer ID'
      '';
      neomacsPackageCheck = aarch64DarwinPkgs.writeShellScript "neomacs-package-check" ''
        set -euo pipefail
        export LC_ALL=C

        package="${self.packages.aarch64-darwin.neomacs}"
        test "$package" = "${self.checks.aarch64-darwin.neomacs-overlay}"
        # Both options return before Lisp, logging, or GUI initialization.
        version="$("$package/bin/neomacs" --version)"
        [[ "$version" == Neomacs\ * ]]
        fingerprint="$("$package/bin/neomacs" --fingerprint)"
        [[ "$fingerprint" =~ ^[[:xdigit:]]{64}$ ]]
        test -s "$package/bin/neomacs-$fingerprint.pdump"
        application="$package/Applications/Neomacs.app"
        launcher="$application/Contents/MacOS/neomacs-launcher"
        test -x "$launcher"
        test -s "$application/Contents/Resources/neomacs.icns"
        test -s "$application/Contents/Resources/icon-NOTICE.txt"
        ${neomacsPkgs.libicns}/bin/icns2png --list \
          "$application/Contents/Resources/neomacs.icns" \
          | ${aarch64DarwinPkgs.gnugrep}/bin/grep -q '512x512'
        /usr/bin/plutil -lint "$application/Contents/Info.plist"
        /usr/bin/codesign --verify --deep --strict "$application"
        test "$("$launcher" --version)" = "$version"
        test "$("$launcher" --fingerprint)" = "$fingerprint"
        printf '%s\n' "$version"
      '';
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
            export NIX_PACKAGES_BASH=${pkgs.bash}/bin/bash
            export PATH=${pkgs.lib.makeBinPath [ pkgs.coreutils ]}:"$PATH"
            cd ${self}
            ${pkgs.yq-go}/bin/yq eval '.' .github/workflows/*.yml >/dev/null
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
            packages = [
              python
              pkgs.nix
              pkgs.yq-go
            ];
            shellHook = pythonEnvironment + ''
              export NIX_PACKAGES_BASH=${pkgs.bash}/bin/bash
            '';
          };
          checkApp = {
            type = "app";
            program = "${maintainerCheck}";
          };
          updaterApps = nixpkgs.lib.optionalAttrs pkgs.stdenv.isDarwin {
            push-neomacs-cache = {
              type = "app";
              program = "${pkgs.writeShellScript "nix-packages-push-neomacs-cache" ''
                set -euo pipefail
                export NIX_PACKAGES_NIX=${pkgs.nix}/bin/nix
                export NIX_PACKAGES_CACHIX=${pkgs.cachix}/bin/cachix
                export PATH=${pkgs.lib.makeBinPath [ pkgs.coreutils ]}:"$PATH"
                exec ${pkgs.bash}/bin/bash ${./scripts/push_neomacs_cache.sh} "$@"
              ''}";
            };
            update-neomacs = {
              type = "app";
              program = "${pkgs.writeShellScript "nix-packages-update-neomacs" ''
                set -euo pipefail
                export NIX_PACKAGES_NIX=${pkgs.nix}/bin/nix
                export PATH=${pkgs.lib.makeBinPath [ pkgs.coreutils ]}:"$PATH"
                exec ${pkgs.bash}/bin/bash ${./scripts/update_neomacs.sh} "$@"
              ''}";
            };
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
      # Keep the launcher and core on upstream's lock. A consumer's nixpkgs
      # follows edge or overlays must not change the output published by CI.
      neomacsPkgs = import neomacs.inputs.nixpkgs { system = "aarch64-darwin"; };
      neomacsPackage = neomacsPkgs.callPackage ./packages/neomacs.nix {
        upstream = neomacs.packages.aarch64-darwin.neomacs;
        upstreamSource = neomacs;
      };
      neomacsOverlay = final: _prev: {
        neomacs =
          assert final.stdenv.hostPlatform.system == "aarch64-darwin";
          neomacsPackage;
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
        neomacs-package = self.packages.aarch64-darwin.neomacs;
        neomacs-overlay = (aarch64DarwinPkgs.extend neomacsOverlay).neomacs;
        neomacs-launcher = aarch64DarwinPkgs.callPackage ./tests/neomacs-launcher.nix {
          upstreamSource = neomacs;
        };
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
        // nixpkgs.lib.optionalAttrs (system == "aarch64-darwin") {
          flogravity-package-check = {
            type = "app";
            program = "${flogravityPackageCheck}";
          };
          neomacs-package-check = {
            type = "app";
            program = "${neomacsPackageCheck}";
          };
          neomacs-finder-check = {
            type = "app";
            program = "${aarch64DarwinPkgs.callPackage ./tests/neomacs-finder.nix {
              upstreamSource = neomacs;
            }}";
          };
        }
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
