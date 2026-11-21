#!/bin/sh
# BIMA v4.3.5 daily preview-only scheduler. Brave is disabled by default.
set -eu
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$ROOT"
LOG_DIR=${BIMA_DISCOVERY_LOG_DIR:-"$HOME/bima-logs"}
mkdir -p -- "$LOG_DIR"
LOCK="$LOG_DIR/multi-source.lock"
exec 9>"$LOCK"
/usr/bin/flock -n 9 || exit 0

# Brave must be explicitly approved/opted into daily execution. The API
# terms may restrict BIMA's intended use; do not enable without review.
PROVIDERS=google,urlscan
BRAVE_DAILY_REQUESTS=1
if [ "${BIMA_ENABLE_BRAVE_DAILY:-0}" = 1 ]; then
  SECRET_FILE="$HOME/.config/bima/brave-api-key"
  if [ ! -f "$SECRET_FILE" ] || [ -L "$SECRET_FILE" ] || [ ! -r "$SECRET_FILE" ]; then
    printf '%s\n' 'BIMA: Brave enabled but private API key file is missing/unreadable' >&2
    exit 1
  fi
  FILE_MODE=$(stat -c '%a' "$SECRET_FILE" 2>/dev/null || stat -f '%Lp' "$SECRET_FILE" 2>/dev/null || true)
  if [ "$FILE_MODE" != 600 ]; then
    printf '%s\n' 'BIMA: Brave API key file must have mode 0600' >&2
    exit 1
  fi
  BRAVE_SEARCH_API_KEY=
  IFS= read -r BRAVE_SEARCH_API_KEY < "$SECRET_FILE" || true
  if [ -z "$BRAVE_SEARCH_API_KEY" ]; then
    printf '%s\n' 'BIMA: Brave API key file is empty' >&2
    exit 1
  fi
  export BRAVE_SEARCH_API_KEY
  PROVIDERS=google,brave,urlscan
  BRAVE_DAILY_REQUESTS=14
fi

PYTHONPATH="$ROOT/multi:$ROOT" \
  "$ROOT/venv/bin/python3" -B "$ROOT/multi/multi_source_discovery.py" \
  --domains "$ROOT/domains.json" \
  --config "$ROOT/config.json" \
  --providers "$PROVIDERS" \
  --execute --acknowledge-quota \
  --max-google-requests 14 \
  --max-brave-requests "$BRAVE_DAILY_REQUESTS" \
  --brave-monthly-ledger "$LOG_DIR/brave-monthly-quota.json" \
  --brave-monthly-request-cap 900 \
  --max-urlscan-requests 2 \
  --results-per-request 5 \
  --preview-limit 500 \
  --request-interval 1 \
  --timeout 8 \
  --state-file "$LOG_DIR/multi-source-state.json" \
  >> "$LOG_DIR/multi-source.jsonl" \
  2>> "$LOG_DIR/multi-source-errors.log"
