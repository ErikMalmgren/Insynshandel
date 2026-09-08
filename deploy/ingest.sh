#!/usr/bin/env bash
# §10.3 cadence in one place. Called by insyn-ingest.service on every timer
# tick; picks the depth from the wall clock so the timer stays trivial.
#
#   - 03:xx local  → nightly: heal gaps, then a 90-day re-scan
#   - any other    → hourly:  a cheap 7-day incremental
#
# Every `insyn` call writes nothing when nothing changed (§4.5), so a quiet
# hour costs one HTTP request.
set -euo pipefail
cd "$(dirname "$0")/.."

dc() { docker compose run --rm cli "$@"; }

hour=$(date +%H)
if [ "$hour" = "03" ]; then
	dc ingest gaps
	dc ingest recent --days 90
else
	dc ingest recent --days 7
fi

dc build

# Static fallback under the volume — handy if the API is down, or to diff the
# JSON in git. Nothing on the API path consumes it; drop this line if you only
# serve live.
dc export-static --out /data/dist

# Advisory only: doctor exits non-zero on a soft issue (e.g. a degraded
# market-cap provider), which must NOT fail the timer and page you at 03:00.
dc doctor || echo "doctor reported failures — check 'journalctl -u insyn-ingest'"
