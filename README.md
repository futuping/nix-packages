# nix-packages

Additional Nix packages that are not distributed through nixpkgs or Homebrew.

## Lite XL application

`lite-xl-app` packages the official Apple Silicon DMG and preserves its
upstream application signature. It is intentionally distinct from the
source-built `pkgs.lite-xl` package in nixpkgs.

Add the flake input and follow the consumer's nixpkgs revision:

```nix
inputs.nix-packages = {
  url = "github:futuping/nix-packages";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Import the overlay module once:

```nix
modules = [
  inputs.nix-packages.darwinModules.lite-xl-app
];
```

Then manage the application through the ordinary package list:

```nix
environment.systemPackages = with pkgs; [
  lite-xl-app
];
```

## Automatic updates

The `Update packages` workflow checks the official Lite XL latest release
daily. It requires one exact `macos-arm64.dmg` asset, downloads it from an
allowlisted GitHub host, calculates its SHA-256 hash, and verifies the hash
against GitHub's release asset digest when one is available. A changed asset
under an unchanged version fails for manual review instead of being trusted
automatically.

GitHub disables scheduled workflows in inactive public repositories after 60
days. When no package update has occurred for 30 days, the workflow creates an
empty heartbeat commit so the schedule remains active without changing package
contents.

Run the updater manually with:

```sh
python3 scripts/update_lite_xl.py
```

Consumers receive published updates the next time they update the
`nix-packages` flake input.
