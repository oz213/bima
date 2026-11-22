# BIMA Tahap 3A — DNS retry deadline index & read-only diagnostics

**Base reviewed:** `5c355cc` (22 November 2026). This patch changes only retry scheduling and adds an offline diagnostic tool. It does **not** modify IndoBERT, browser behavior, credential files, source discovery, Telegram review, BIMA alert delivery, or any existing job status/retry deadline.

## Why

In the observed production snapshot, `retry_http` contains 38,118 URLs, mostly first-attempt failures: 19,244 `temporary_failure`, 17,544 `no_address`, 1,227 `none`, and 103 `not_found`. The latter `none` may represent HTTP timeout; group by `error_category` before treating it as DNS.

The existing BIMA crawler already has a 10-retry/minute cap, 4:1 normal/retry pacing, and differentiated cooldowns (30 minutes for temporary DNS/timeout, 6 hours for `no_address`, 24 hours for `not_found`). **This patch deliberately does not change any of those values.**

## Behavior

1. `find_due_network_retry` chooses the **earliest eligible UTC retry deadline** (tie: lowest ID) instead of the lowest ID irrespective of deadline. It still excludes NULL/future deadlines, categories outside `dns_error`/`timeout`, zero attempts, and exhausted attempts.
2. A **small partial SQLite index** on `(next_retry_at,id)` covers eligible `retry_http` DNS/timeout jobs. The index is created only after old `jobs` columns have been migrated by `connect_db`.
3. A stable query hint uses the partial index when it exists. The old unhinted query remains available for isolated tests or pre-index DBs, while selection rules stay identical.
4. `tools/bima_dns_retry_triage.py` opens SQLite with URI `mode=ro` and `PRAGMA query_only=ON`, then returns one consistent snapshot of category, due/undue counts, and attempts. It does not perform DNS lookups or write any file.
5. **No automatic retry of `pending_alert`, `prealert_send_unknown`, or any other alert state.**

## Validation on Mac (from the BIMA repo root)

```bash
PATCH="$HOME/Downloads/bima-dns-retry-scheduler-v1.patch"
git status --short
git pull --ff-only origin main
git apply --check "$PATCH"
git apply "$PATCH"
git diff --check

PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_dns_retry_scheduler_v1.py' -v
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_network_retry_queue.py' -v

git add -- multi/network_retry_queue.py multi/crawler_multi.py \
  multi/tests/test_dns_retry_scheduler_v1.py \
  tools/bima_dns_retry_triage.py README_BIMA_DNS_RETRY_SCHEDULER_V1.md
git diff --cached --check
git diff --cached --name-status
git commit -m "perf: prioritize due DNS retries with indexed queue diagnostics"
git push origin main
```

## Rollout on Linux

```bash
APP=/home/fauzi/crawler-rebuild
DB="$APP/multi/state/crawler_production.sqlite3"
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_dns_retry_scheduler_v1.py' -v
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_network_retry_queue.py' -v
sudo -u fauzi -H "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_dns_retry_triage.py" --db "$DB"
```

Restart `pandi-crawler-multi.service` only after successful tests, during a quiet period. The first crawler start installs the partial index in-place (SQLite schema DDL, not destructive). Expect startup to take a little longer for index creation. **No database backup, migration reset, or manual requeue is part of this procedure.**

```bash
sudo systemctl restart pandi-crawler-multi.service
systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
journalctl --utc -u pandi-crawler-multi.service --since '5 minutes ago' \
  --no-pager | grep -E 'IndoBERT loaded ONCE|HTTP attempted=|Traceback|ERROR' | tail -n 30
```

## Verification of SQLite index (read-only)

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" - "$DB" <<'PY'
import sqlite3, sys
from pathlib import Path
uri = Path(sys.argv[1]).resolve().as_uri() + '?mode=ro'
with sqlite3.connect(uri, uri=True) as db:
    print(db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_jobs_due_network_retry_v1'").fetchone())
PY
```

## Next steps (not in this patch)

- Run the read-only diagnostics twice at least 30 minutes apart; establish if `temporary_failure` is localized or widespread.
- Check recursive resolver health and query latency independently. Do not use cache-bypassing repeated DNS probes or disable SSRF validation.
- Only after the resolver issue is understood, consider exponential backoff or changing retry rates; current data shows 99.1% first-attempt failures, so backoff alone would not fix the initial failure load.
- Compare sustained *completed* jobs/hour (not just HTTP attempts/hour) and browser queue pressure before raising HTTP concurrency. Keep the IndoBERT model enabled and loaded once.
