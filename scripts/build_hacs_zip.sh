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
