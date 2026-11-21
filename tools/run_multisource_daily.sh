#!/bin/sh
# BIMA v4.3.4 daily multi-source discovery. Does not enqueue or alert.
set -eu
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$ROOT"
LOG_DIR=${BIMA_DISCOVERY_LOG_DIR:-"$HOME/bima-logs"}
mkdir -p -- "$LOG_DIR"
LOCK="$LOG_DIR/multi-source.lock"
exec 9>"$LOCK"
/usr/bin/flock -n 9 || exit 0
PYTHONPATH="$ROOT/multi:$ROOT" \
  "$ROOT/venv/bin/python3" -B "$ROOT/multi/multi_source_discovery.py" \
  --domains "$ROOT/domains.json" \
  --config "$ROOT/config.json" \
  --providers google,brave,urlscan \
  --execute --acknowledge-quota \
  --max-google-requests 14 \
  --max-brave-requests 2 \
  --max-urlscan-requests 2 \
  --results-per-request 5 \
  --preview-limit 500 \
  --request-interval 1 \
  --timeout 8 \
  --state-file "$LOG_DIR/multi-source-state.json" \
  >> "$LOG_DIR/multi-source.jsonl" \
  2>> "$LOG_DIR/multi-source-errors.log"
