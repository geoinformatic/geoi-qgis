#!/usr/bin/env bash
# Build the installable QGIS plugin zip: dist/geoi.zip (contains the geoi/ folder).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

out="dist"
mkdir -p "$out"
rm -f "$out/geoi.zip"

# Zip the plugin package, excluding caches and compiled files.
zip -r "$out/geoi.zip" geoi \
  -x 'geoi/**/__pycache__/*' \
  -x 'geoi/__pycache__/*' \
  -x '*.pyc'

echo "Built $out/geoi.zip"
