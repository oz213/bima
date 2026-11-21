# BIMA v4.3.6 — Google/urlscan Discovery → Existing SQLite `jobs` (guarded)

## Scope

The actual BIMA production database uses `multi/state/crawler_production.sqlite3` with `jobs` (unique `url`, default `status='pending'`, default `attempts=0`). The live crawler selects pending IDs in ascending order. The existing registry `urls.json` baseline and the shared IndoBERT model are **not** changed.

This release adds one independent importer **only**:

- `multi/discovery_queue.py`: ingest candidates already emitted by `multi/multi_source_discovery.py` v4.3.4/v4.3.5 into the **existing** `jobs` table. Source is the local `multi-source.jsonl` file, **not** the Google API and **not** `urls.json`.
- `multi/tests/test_discovery_queue_v436.py`: isolated SQLite and synthetic JSONL unit tests. No network, notifications, model, or production DB usage.
- Default mode is **preview/read-only** (`mode=ro`, `query_only=ON`). It never requests provider APIs, starts Chrome, sends alerts, writes a database, changes crontab, or restarts services.
- An operator must pass **both** `--apply --acknowledge-queue-write` to insert jobs. This is an intentional production DB write. Once inserted, the existing BIMA crawler may process and ultimately alert on a candidate if its normal classification rules decide so; this importer never sends an alert itself.

**This release does not enable automatic queuing from cron.** Continue the v4.3.5 Google/urlscan daily preview; decide whether and when to activate queue writing after inspecting pending backlog and reviewing the first sample.

## Safety gates

- Accepts only `.id` URLs that match the stated `found_via_scopes` and the current `domains.json`; URLs must pass existing strict `proactive_discovery.normalize_url` validation. Raw querystrings and fragments are rejected.
- Accepts **Google and urlscan** records only, where provenance is verifiable in `preview_candidates`. If *any* source is `brave` (including mixed-source records in old v4.3.4 log entries), the candidate is excluded. There is no Brave ingestion or Brave result storage.
- Dedupes log candidates and existing jobs via `jobs.url` uniqueness. For homepage roots, recognizes both `https://host.id` and `https://host.id/` as the same seed, avoiding duplicate queue entries. Existing job status, evidence, classifier data, attempts, and alerts are never modified.
- No new tables or migrations. Verifies default `status='pending'`, `attempts=0` and the unique URL index before writing. Opens `mode=rw` only when explicitly asked; uses `BEGIN IMMEDIATE` to serialize with the running crawler. SQLite connections use a bounded busy timeout.
- Backpressure defaults `--max-new 20` per invocation and `--max-pending 10000`. Operators can set `--max-pending` to a conscious threshold (at most 3 million) **after checking the real backlog**. With a large Registry-derived queue, the default threshold will intentionally result in `would_insert=0` and **no automatic bypass**.
- Input is a regular JSONL file <=64 MiB, max 100k records, max 2 MiB per line, <=100k unique candidates. Invalid JSON fails before DB changes. `preview_truncated=true` is counted and reported; truncated upstream candidates cannot be recovered from a single JSONL record.
- Reads **all** historical log lines within these bounds to backfill candidates previously blocked by backlog caps. Preserve the JSONL file until they are ingested or explicitly discarded; archiving/rotating it prematurely may remove pending candidates.
- Current crawler uses ascending `jobs.id`, so new candidates **will not automatically receive higher processing priority** over the large existing `urls.json` backlog. Introducing priority dispatch would require a separately tested change to the crawler; this patch intentionally avoids it.

## Mac deployment (after v4.3.5 on `main`)

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

git status --short
git log -1 --oneline

git apply --check ~/Downloads/bima-v4.3.5-to-v4.3.6-discovery-queue-preview.patch && \
git apply ~/Downloads/bima-v4.3.5-to-v4.3.6-discovery-queue-preview.patch

PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -q

git diff --check
git add -- README_DISCOVERY_V436.md multi/discovery_queue.py multi/tests/test_discovery_queue_v436.py
git diff --cached --check
git commit -m "feat: add guarded discovery JSONL to existing jobs importer"
git push origin main
```

## Server deployment and test (no service restart)

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover -s multi/tests -q
```

Preserve the server-local ` M crawler_google.py` and `?? domains.json`; never `git reset --hard`, `git clean`, or `git add .` in production. Do not restart `pandi-crawler-multi.service` / `pandi-telegram-monitor.service`.

### 1. Read-only backlog profile (can run before the next cron execution)

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H ./venv/bin/python3 - <<'PY'
import sqlite3
from pathlib import Path
path = Path('multi/state/crawler_production.sqlite3').resolve()
with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10) as conn:
    conn.execute('PRAGMA query_only=ON')
    for status, n in conn.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status ORDER BY status'):
        print(f'{status}: {n}')
    print('total:', conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
    print('max_id:', conn.execute('SELECT COALESCE(MAX(id),0) FROM jobs').fetchone()[0])
PY
```

### 2. Read-only Discovery-to-queue audit (after first daily v4.3.5 run)

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B multi/discovery_queue.py \
  --input /home/fauzi/bima-logs/multi-source.jsonl \
  --db multi/state/crawler_production.sqlite3 \
  --domains domains.json \
  --max-new 20 --max-pending 10000 \
  | ./venv/bin/python3 -m json.tool
```

If the file does not yet exist, the importer returns an error and **does not create it**. No API request is made. Look at `pending_before`, `already_in_jobs`, `not_in_jobs`, `would_insert`, `blocked_by_pending_limit`, `excluded_brave_entries`, `rejected_entries`, `truncated_preview_runs`. `would_insert=0` with a large `pending_before` is a deliberate safety stop, not a crash.

### 3. Optional bounded DB insertion (explicit operator approval only)

**Do not run this command until pending backlog and alert impact have been reviewed.** It inserts live `pending` jobs that the running crawler may process and alert upon. Example with the conservative default backlog threshold:

```bash
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B multi/discovery_queue.py \
  --input /home/fauzi/bima-logs/multi-source.jsonl \
  --db multi/state/crawler_production.sqlite3 \
  --domains domains.json \
  --max-new 5 --max-pending 10000 \
  --apply --acknowledge-queue-write
```

This **will intentionally insert up to 5 jobs** if capacity remains. Never set `--max-pending` above the measured backlog merely to force an insertion without deciding how new IDs should be prioritized. Existing `jobs` are never changed; rerunning the same input is idempotent.

## Known limits and next phase

- New jobs are queued by ID; without priority scheduling, crawling a large baseline may delay them. Before enabling the importer automatically, decide a **fair high-priority discovery lane** that protects Telegram requests, retry pacing and routine jobs; separately verify its effect on the live process.
- Existing JSONL state and logs are unchanged. This patch does not persist provenance into the `jobs` table (available in original discovery JSONL); the crawler may overwrite `reason` during classification.
- `urlscan` still needs its API key. Brave is excluded from this workflow, even if a future cron enables transient-only Brave metrics.
- Google Custom Search JSON API ends on 2027-01-01, so multi-source strategy must continue to evolve.
