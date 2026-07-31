# nix-packages

Additional Nix packages that are not distributed through nixpkgs or Homebrew.

## ego lite

`ego-lite` packages the official Apple Silicon DMG and exposes the bundled
`ego-browser` executable on `PATH`. The derivation disables fixups so the
notarized Developer ID signature from CITRO LABS PTE. LIMITED remains intact.
The browser binary is closed source and is therefore marked unfree; the upstream
repository's MIT license covers the open-source automation harness rather than
the browser itself.

Import its focused overlay module:

```nix
modules = [
  inputs.nix-packages.darwinModules.ego-lite
];
```

Then select the package normally:

```nix
environment.systemPackages = with pkgs; [
  ego-lite
];
```

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

The `Update packages` workflow checks both packages daily. For Lite XL, it
requires one exact `macos-arm64.dmg` release asset, downloads it from an
allowlisted GitHub host, and verifies its SHA-256 against GitHub's asset digest
when available.

ego lite uses a mutable official CDN URL instead of versioned release assets.
Its updater cross-checks the downloaded SHA-256 against the CDN's S3 metadata,
records the S3 version ID, mounts the DMG on macOS, and validates the bundle
version, bundle ID, Developer ID authority, Team ID, executable architecture,
strict code signature, and bundled CLI. Any changed artifact under an unchanged
application version also fails for manual review.

GitHub disables scheduled workflows in inactive public repositories after 60
days. When no package update has occurred for 30 days, the workflow creates an
empty heartbeat commit so the schedule remains active without changing package
contents.

Run the updater manually with:

```sh
python3 scripts/update_lite_xl.py
python3 scripts/update_ego_lite.py
```

Consumers receive published updates the next time they update the
`nix-packages` flake input.
