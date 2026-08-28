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

## ShardX Launcher

`shardx-launcher` packages the official Apple Silicon ShardX Launcher DMG.
The launcher is MIT licensed, but its first run downloads a separate,
closed-source Chromium engine and fingerprint library from ProxyShard's CDN.
The upstream launcher is not notarized or signed with an Apple Developer ID;
the derivation leaves its bundle unchanged.

Import its focused overlay module:

```nix
modules = [
  inputs.nix-packages.darwinModules.shardx-launcher
];
```

Then select the package normally:

```nix
environment.systemPackages = with pkgs; [
  shardx-launcher
];
```

## Neomacs

`neomacs` forwards the upstream Apple Silicon Nix package at commit
`6def94af1c407027274a61c04356212b87a4c7ff`. This revision includes the
post-v0.0.15 Darwin build fixes; the v0.0.15 macOS release artifacts are not
self-contained and the tagged flake predates those fixes. The package installs
the `neomacs` command rather than a macOS application bundle.

Import its focused overlay module:

```nix
modules = [
  inputs.nix-packages.darwinModules.neomacs
];
```

Then select the package by its bare name:

```nix
environment.systemPackages = with pkgs; [
  neomacs
];
```

## Automatic updates

The `Update packages` workflow checks the updater-managed binary packages
daily. Lite XL and ShardX Launcher each require one exact Apple Silicon release
asset, download it from an allowlisted GitHub host, and verify its SHA-256
against GitHub's asset digest when available.

ego lite uses a mutable official CDN URL instead of versioned release assets.
Its updater cross-checks the downloaded SHA-256 against the CDN's S3 metadata,
records the S3 version ID, mounts the DMG on macOS, and validates the bundle
version, bundle ID, Developer ID authority, Team ID, executable architecture,
strict code signature, and bundled CLI. Any changed artifact under an unchanged
application version also fails for manual review.

Neomacs remains pinned to a reviewed upstream source revision until a stable
release contains both the Darwin build fixes and self-contained macOS artifacts.

GitHub disables scheduled workflows in inactive public repositories after 60
days. When no package update has occurred for 30 days, the workflow creates an
empty heartbeat commit so the schedule remains active without changing package
contents.

Run the updater manually with:

```sh
python3 scripts/update_lite_xl.py
python3 scripts/update_ego_lite.py
python3 scripts/update_shardx_launcher.py
```

Consumers receive published updates the next time they update the
`nix-packages` flake input.
