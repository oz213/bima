#!/bin/sh
# BIMA v4.3.8 daily discovery; bounded queue import is opt-in and fail-closed.
set -eu
umask 077
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$ROOT"
LOG_DIR=${BIMA_DISCOVERY_LOG_DIR:-"$HOME/bima-logs"}
mkdir -p -- "$LOG_DIR"
LOCK="$LOG_DIR/multi-source.lock"
exec 9>"$LOCK"
/usr/bin/flock -n 9 || exit 0

# Brave API terms require separate authorization for abuse monitoring.
# It remains disabled unless explicitly opted in after policy clearance.
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

# Enabled only by the explicit operator-controlled cron environment variable.
AUTO_IMPORT=${BIMA_DISCOVERY_AUTO_IMPORT:-0}
if [ "$AUTO_IMPORT" != 0 ] && [ "$AUTO_IMPORT" != 1 ]; then
  printf '%s\n' 'BIMA: BIMA_DISCOVERY_AUTO_IMPORT must be 0 or 1' >&2
  exit 1
fi

# Preserve the v4.3.5 behavior unless automatic queue imports are authorized.
# The execute output contains at most one JSON object per scheduled run.
run_discovery() {
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
    --state-file "$LOG_DIR/multi-source-state.json"
}

if [ "$AUTO_IMPORT" = 0 ]; then
  run_discovery >> "$LOG_DIR/multi-source.jsonl" \
    2>> "$LOG_DIR/multi-source-errors.log"
  exit $?
fi

SPOOL_DIR="$LOG_DIR/multi-source-pending"
if [ -L "$SPOOL_DIR" ]; then
  printf '%s\n' 'BIMA: Discovery spool must not be a symlink' >&2
  exit 1
fi
mkdir -p -m 700 -- "$SPOOL_DIR"
chmod 700 -- "$SPOOL_DIR"
RUN_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS=$(mktemp "$SPOOL_DIR/pending-$RUN_STAMP-XXXXXXXX.jsonl")
# Keep a successful run until the guarded FIFO importer has accounted for
# every candidate. No Brave URLs are written by multi_source_discovery.py.
if run_discovery > "$RESULTS" 2>> "$LOG_DIR/multi-source-errors.log"; then
  cat -- "$RESULTS" >> "$LOG_DIR/multi-source.jsonl"
else
  cat -- "$RESULTS" >> "$LOG_DIR/multi-source.jsonl"
  mv -- "$RESULTS" "$RESULTS.failed"
  printf '%s\n' 'BIMA: provider discovery failed; no automatic queue write' \
    >> "$LOG_DIR/multi-source-errors.log"
  exit 1
fi

# Exactly one automatic importer attempt per UTC calendar day. This is made
# BEFORE any DB write, so an interrupted run fails closed for the rest of that
# day; the private spool is replayed on the next scheduled day.
DAY_UTC=$(date -u +%Y%m%d)
MARKER="$LOG_DIR/multi-source-import-$DAY_UTC.started"
if (set -C; : > "$MARKER") 2>/dev/null; then
  PYTHONPATH="$ROOT/multi:$ROOT" \
    "$ROOT/venv/bin/python3" -B "$ROOT/multi/daily_discovery_import.py" \
    --spool-dir "$SPOOL_DIR" \
    --db "$ROOT/multi/state/crawler_production.sqlite3" \
    --domains "$ROOT/domains.json" \
    --max-new 5 \
    --max-priority 20 \
    --max-pending 10000 \
    --allow-priority-over-pending-ceiling \
    --apply --acknowledge-queue-write \
    >> "$LOG_DIR/multi-source-import.jsonl" \
    2>> "$LOG_DIR/multi-source-errors.log"
else
  printf '%s\n' 'BIMA: automatic Discovery import already attempted this UTC day; spool retained' \
    >> "$LOG_DIR/multi-source-errors.log"
fi
