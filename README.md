# nix-packages

Additional Nix packages that are not distributed through nixpkgs or Homebrew.

Add the flake input and follow the consumer's nixpkgs revision:

```nix
inputs.nix-packages = {
  url = "github:futuping/nix-packages";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

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

## FloGravity

`flogravity` packages the official universal macOS DMG for FloGravity (浮引).
The HFS image's Unicode bundle and executable names are truncated by `undmg`,
so the derivation identifies the sole application structurally and restores the
names declared by its `Info.plist`. It checks the pinned bundle identity,
architectures, Developer ID authority, Team ID, and hardened-runtime flag, then
strictly verifies the normalized bundle without re-signing it. Restoring the
two names also restores the validity of the original notarized Developer ID
seal, so the installed application retains its upstream trust and restricted
entitlements. The application is closed source and offers a separately
licensed Pro edition, so the package is marked unfree.

Import its focused overlay module:

```nix
modules = [
  inputs.nix-packages.darwinModules.flogravity
];
```

Then select the package by its bare name:

```nix
environment.systemPackages = with pkgs; [
  flogravity
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

`neomacs` forwards the upstream Apple Silicon Nix package from `main`, retaining
the upstream flake's own locked build dependencies. `flake.lock` records the
exact adopted source revision; the input URL no longer freezes one commit.
This intentionally follows development revisions rather than stable releases.
The package keeps the upstream `neomacs` command and adds
`Applications/Neomacs.app` for Finder and Dock launching. The app contains the
upstream icon and a small native launcher that executes the dependency-tracked
upstream CLI wrapper. It is a Nix application entry point, not a self-contained
redistributable DMG. Nix-darwin exposes it through `/Applications/Nix Apps`.

The launcher is built separately from Neomacs: adding or changing the entry
point reuses an unchanged upstream build. A new upstream Store path generates
a matching launcher automatically, so no host paths or PATH lookups are baked
into the source. Only the new launcher bundle is ad-hoc signed; the upstream
program and its runtime resources are not modified or re-signed. Nix reuses
matching outputs or binary caches when available; otherwise it compiles the
upstream source and its missing dependencies.

From a writable checkout, update only Neomacs and retain the candidate lock only
after a native build and headless package check succeed:

```sh
nix run --no-update-lock-file .#update-neomacs
```

The updater restores the original lock on failure and skips the build if the
lock is unchanged. To check the currently locked package without updating it:

```sh
nix run --no-update-lock-file .#neomacs-package-check
```

The check exercises `--version`, the embedded fingerprint, and its matching
runtime image through both the CLI and app launcher, and verifies the app's
metadata, icon and signature. It does not start the GUI or load user
configuration, and is not a full interactive application test. The
`checks.aarch64-darwin.neomacs-launcher` fixture additionally verifies argument
forwarding and automatic retargeting when the upstream package path changes,
without compiling Neomacs.

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
daily. FloGravity is discovered from its official stable Sparkle appcast. For a
new candidate release, the updater accepts only immutable versioned assets from
the reviewed download host, verifies its Sparkle Ed25519 signature, computes
the complete SHA-256, and checks the bundle identity, universal architectures,
and embedded signing identity. Every accepted version must have a valid,
notarized Developer ID signature; there are no version-specific exceptions.
Before publishing a changed FloGravity source, the native updater workflow also
builds the normalized Nix output and reassesses its preserved signature with
Gatekeeper before committing the source metadata.
ShardX Launcher requires one exact Apple Silicon release asset, downloads it
from an allowlisted GitHub host, and verifies its SHA-256 against GitHub's asset
digest when available.

ego lite uses a mutable official CDN URL instead of versioned release assets.
Its updater cross-checks the downloaded SHA-256 against the CDN's S3 metadata,
records the S3 version ID, mounts the DMG on macOS, and validates the bundle
version, bundle ID, Developer ID authority, Team ID, executable architecture,
strict code signature, and bundled CLI. Any changed artifact under an unchanged
application version also fails for manual review.

The same daily workflow updates Neomacs in a separate, sequential Apple Silicon
job after the binary updaters. It updates only the Neomacs input and its upstream
dependency lock graph, builds and smoke-tests the candidate, and checks the
standalone flake before committing `flake.lock`. A failed candidate or timeout
does not publish a Neomacs update or block the preceding DMG updates. The
120-minute budget accommodates source builds without promising cache hits.
Consumers still need to refresh their own lock; system activation is manual.

GitHub disables scheduled workflows in inactive public repositories after 60
days. When no package update has occurred for 30 days, the workflow creates an
empty heartbeat commit so the schedule remains active without changing package
contents.

## Maintainer environment

The flake pins the maintainer toolchain, including Python 3.14 and the Nix CLI
used for Neomacs lock updates and standalone evaluation.
Enter it from the repository root with:

```sh
nix develop --no-update-lock-file .#maintainer
```

Run every offline updater test through the same locked Python, without using a
host `python3` or user-installed Python packages:

```sh
nix run --no-update-lock-file .#maintainer-check
```

CI also runs the complete offline suite on Python 3.9, the declared minimum
compatible version, and Python 3.14, the current maintainer version. The
scheduled workflow uses only the locked Nix entry points. Pull requests and
pushes also build FloGravity and Neomacs on Apple silicon macOS runners, covering
the final DMG bundle and the upstream source package rather than evaluation
alone.

On Apple silicon macOS, run an updater manually from the repository root with:

```sh
nix run --no-update-lock-file .#update-ego-lite
nix run --no-update-lock-file .#update-flogravity
nix run --no-update-lock-file .#update-shardx-launcher
```

Consumers receive published updates the next time they update the
`nix-packages` flake input.
