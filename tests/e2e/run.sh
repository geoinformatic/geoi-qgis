#!/usr/bin/env bash
# Seed a public, editable feature service, serve the platform over real HTTP,
# and run the headless-QGIS editing round-trip against it.
#
#   bash tests/e2e/run.sh
#
# Requires: php (cli + sqlite3), curl, and a QGIS python (qgis.core).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin_root="$(cd "$here/../.." && pwd)"
port="${GEOI_E2E_PORT:-8631}"

export GEOI_DATA_DIR="$(mktemp -d)"
export GEOI_GOOGLE_CLIENT_ID="geoi-test-client.apps.googleusercontent.com"
export GEOI_E2E_BASE="http://127.0.0.1:${port}/platform"

SRV=""
cleanup() {
  [ -n "$SRV" ] && kill "$SRV" 2>/dev/null || true
  rm -rf "$GEOI_DATA_DIR"
}
trap cleanup EXIT

SLUG="$(php "$here/seed.php" "$here/point.geojson" edittest)"
export GEOI_E2E_SLUG="$SLUG"
echo "seeded editable service '$SLUG'"

php -S "127.0.0.1:${port}" "$here/router.php" >/tmp/geoi-e2e-php.log 2>&1 &
SRV=$!

ready=""
for _ in $(seq 1 40); do
  if curl -fs "${GEOI_E2E_BASE}/rest/info" >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
if [ -z "$ready" ]; then
  echo "platform did not come up; php log:"; cat /tmp/geoi-e2e-php.log || true
  exit 1
fi

cd "$plugin_root"
python3 "$here/roundtrip.py"
