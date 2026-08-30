#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 0 ]]; then
  if [[ $# -eq 1 && "$1" == --help ]]; then
    echo 'Usage: nix run .#update-neomacs'
    echo 'Update the Neomacs input, build and check it, then retain the new lock.'
    echo 'Run from a writable nix-packages checkout; failures restore its original lock.'
    exit 0
  fi
  echo 'error: only --help is supported' >&2
  exit 2
fi

: "${NIX_PACKAGES_NIX:?run this updater through nix run .#update-neomacs}"
repository_root="${NIX_PACKAGES_REPOSITORY:-$PWD}"
cd -- "$repository_root"
if [[ ! -f flake.nix || ! -f flake.lock || -L flake.lock ||
      ! -f scripts/update_neomacs.sh || ! -w flake.lock || ! -w . ]]; then
  echo 'error: run this updater from a writable nix-packages checkout' >&2
  exit 2
fi

original_lock="$(mktemp "${TMPDIR:-/tmp}/nix-packages-neomacs-lock.XXXXXX")"
cp -p flake.lock "$original_lock"
keep_lock=false
cleanup() {
  status=$?
  trap - EXIT
  if [[ "$keep_lock" != true ]]; then
    cp -p "$original_lock" flake.lock || status=1
  fi
  rm -f "$original_lock" || status=1
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$NIX_PACKAGES_NIX" flake update neomacs
if cmp -s "$original_lock" flake.lock; then
  echo 'Neomacs lock is already current; skipping the package build.'
else
  "$NIX_PACKAGES_NIX" run --no-update-lock-file .#neomacs-package-check
  echo 'Neomacs update passed its native package check.'
fi
keep_lock=true
