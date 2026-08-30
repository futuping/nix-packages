#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 0 ]]; then
  if [[ $# -eq 1 && "$1" == --help ]]; then
    echo 'Usage: nix run .#push-neomacs-cache'
    echo 'Publish the locked Neomacs runtime and its small IFD source to utitsoga.'
    echo 'Run after package validation, from the checkout to publish.'
    echo 'Provide the cache write token only through CACHIX_AUTH_TOKEN.'
    exit 0
  fi
  echo 'error: only --help is supported' >&2
  exit 2
fi

: "${NIX_PACKAGES_NIX:?run through nix run .#push-neomacs-cache}"
: "${NIX_PACKAGES_CACHIX:?run through nix run .#push-neomacs-cache}"
if [[ -z "${CACHIX_AUTH_TOKEN:-}" ]]; then
  echo 'error: CACHIX_AUTH_TOKEN is required to publish the cache' >&2
  exit 2
fi

repository_root="${NIX_PACKAGES_REPOSITORY:-$PWD}"
cd -- "$repository_root"
if [[ ! -f flake.nix || ! -f flake.lock ||
      ! -f scripts/push_neomacs_cache.sh || ! -f packages/neomacs.nix ]]; then
  echo 'error: run from the nix-packages checkout to publish' >&2
  exit 2
fi

# Do not scan the Store or upload cargoArtifacts/toolchains. The dummy source
# is needed by Crane's import-from-derivation even for a cache-only consumer.
# Complete both realizations before allowing Cachix to upload anything.
outputs="$("$NIX_PACKAGES_NIX" build --no-link --no-update-lock-file --print-out-paths \
  .#packages.aarch64-darwin.neomacs \
  .#packages.aarch64-darwin.neomacs.upstream.cargoArtifacts.src)"
printf '%s\n' "$outputs" | "$NIX_PACKAGES_CACHIX" push utitsoga
