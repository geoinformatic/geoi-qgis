#!/usr/bin/env bash
#
# verify-release-urls.sh — MANUAL pre-upload sanity check (NOT wired into CI).
#
# plugins.qgis.org rejects an upload whose homepage / tracker / repository URLs
# don't resolve, and a wrong-URL upload has already blocked a release. This
# script `curl -sI`s each of those three URLs from the PUBLIC metadata and
# confirms a 2xx (following redirects). It is deliberately NOT in any workflow:
# network reachability is flaky and must never gate CI — run it by hand right
# before you publish.
#
# By default it checks the PUBLIC repo values (what actually gets uploaded);
# pass --metadata <file> to check a specific metadata.txt instead.
#
# Usage:
#   qgis-plugin/scripts/verify-release-urls.sh                 # public URLs
#   qgis-plugin/scripts/verify-release-urls.sh --metadata path/to/metadata.txt
#
set -euo pipefail

# The public-repo URLs (kept in lock-step with tools/rewrite_public_metadata.py).
default_urls=(
  "https://github.com/geoinformatic/geoi-qgis#usage"
  "https://github.com/geoinformatic/geoi-qgis/issues"
  "https://github.com/geoinformatic/geoi-qgis"
)

meta=""
while [ $# -gt 0 ]; do
  case "$1" in
    --metadata) meta="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; exit 2 ;;
  esac
done

urls=()
if [ -n "$meta" ]; then
  [ -f "$meta" ] || { echo "error: metadata not found: $meta" >&2; exit 1; }
  for key in homepage tracker repository; do
    val="$(awk -F= -v k="$key" \
      '$0 ~ "^[[:space:]]*" k "[[:space:]]*=" { sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); print; exit }' \
      "$meta")"
    [ -n "$val" ] && urls+=("$val")
  done
else
  urls=("${default_urls[@]}")
fi

fail=0
for u in "${urls[@]}"; do
  code="$(curl -sI -o /dev/null -w '%{http_code}' -L --max-time 20 "$u" || echo '000')"
  if [ "$code" -ge 200 ] && [ "$code" -lt 300 ]; then
    printf '  ok   %-3s %s\n' "$code" "$u"
  else
    printf '  FAIL %-3s %s\n' "$code" "$u"
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "One or more release URLs did not return 2xx." >&2
  exit 1
fi
echo "All release URLs return 2xx."
