#!/usr/bin/env bash
# Build the HACS release asset (eon_next.zip).
#
# hacs.json sets zip_release + filename: eon_next.zip, so HACS installs from
# this asset on a GitHub release rather than from the repository tree.  The
# zip holds the *contents* of custom_components/eon_next/ at its root (HACS
# extracts it straight into custom_components/eon_next/), without bytecode.
#
# Usage: scripts/build_hacs_zip.sh [output-path]   (default: ./eon_next.zip)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-$root/eon_next.zip}"
case "$out" in
  /*) ;;
  *) out="$PWD/$out" ;;
esac

# A hand build zips the working tree, so warn about local changes that a
# release build (clean checkout) would not contain.
if git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
  if [ -n "$(git -C "$root" status --porcelain -- custom_components/eon_next)" ]; then
    echo "WARNING: custom_components/eon_next has uncommitted or untracked changes; they will be included." >&2
  fi
fi

rm -f "$out"
cd "$root/custom_components/eon_next"
zip -r -q "$out" . -x '*__pycache__*' -x '*.pyc'

# Sanity checks: manifest at the zip root, no bytecode.
listing="$(unzip -Z1 "$out")"
grep -qx 'manifest.json' <<<"$listing" || { echo "manifest.json missing from zip root" >&2; exit 1; }
if grep -qE '__pycache__|\.pyc$' <<<"$listing"; then
  echo "bytecode found in zip" >&2
  exit 1
fi
echo "Built $out ($(wc -l <<<"$listing") files)"
