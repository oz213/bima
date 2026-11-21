# BIMA v4.3.8 — Automatic bounded Google/urlscan priority import

**Rollout state:** source patch only; **auto-import is OFF by default**. Deploy via **Mac → GitHub → server `git pull`**. No Python/source editing on the live server. No restart of the crawler or Telegram monitor is necessary for this cron-only change.

## Confirmed starting point

- BIMA v4.3.7 unit tests on server: **399/399 PASS**.
- Live crawler restarted with v4.3.7, PID `2814040` at verification, IndoBERT loaded once, and `/cek` Telegram enabled.
- Canary Google jobs `7737184` and `7737185` were each **complete** after **one** attempt on 21 November 2026. This confirms processing, **not** a particular abuse verdict.
- Registry backlog was approximately 1.515 million `pending`; BIMA uses the same SQLite `jobs` table. `discovery_job_priority` exists since the v4.3.7 canary.
- Brave Search API key is privately stored, but **Brave remains excluded from automatic ingestion** until authorized usage and result handling are confirmed. Do **not** enable Brave to make this feature work.

## What changes

1. `tools/run_multisource_daily.sh` has an optional `BIMA_DISCOVERY_AUTO_IMPORT=1` switch. With it **unset** (default), the existing Google/urlscan daily discovery logs behave as before: no queue write. With it set, the scheduler saves the result from **that execution** in a private spool, appends it to the normal JSONL log, then runs the bounded importer. It never re-reads the full historical log for automatic import.
2. `multi/daily_discovery_import.py` reads **private per-run files oldest first**. It uses the existing `multi/discovery_queue.py` for `.id` validation, SQL `INSERT OR IGNORE`, existing-job promotion and priority metadata. It allows at most **5 new jobs per scheduled invocation** with a maximum **20 active pending Discovery priority jobs**. The cron script also creates an **atomic UTC-day `.started` marker before any import**, preventing more than one *automatic* import attempt in that UTC day, including accidental manual reruns of the same cron script. Separate manual importer calls do not share this quota gate.
3. With a 1.5-million-job Registry backlog, the auto-import command **explicitly** passes `--allow-priority-over-pending-ceiling`; this permits at most five extra new jobs despite exceeding the default global pending ceiling of 10,000. It does not increase the global backlog cap or change regular crawler behavior. Old terminal verdicts, attempts, Telegram requests, alert states, and Registry jobs are never reset by this feature.
4. Unprocessed candidates remain in `~/bima-logs/multi-source-pending/` (directory mode `0700`, files `0600`) for bounded FIFO replay on the **next** authorized daily run. Once every candidate in a file is already in `jobs`, or no longer eligible for promotion, that private spool file is deleted. Search output remains in the existing private JSONL audit log.
5. If a provider run fails, there is **no automatic DB import**. Its output is retained with a `.failed` extension for manual review, and the error is logged without credentials. If the import fails mid-batch or after some files were imported, earlier SQLite commits may exist; the spool remains for idempotent retry on a later day. The daily marker intentionally prevents same-day automatic retries.
6. No changes to live crawler code, shared IndoBERT, BIMA API, `/cek`, browser/Selenium, `urls.json`, or systemd services.

**Important operational constraint:** 14 Google queries/day × up to 5 results/query may yield more than 5 candidates per day. The private spool retains excess candidates so they do not disappear when the daily importer reaches its limit. If arrival exceeds processing capacity over many days, the spool can grow; review pending-file count and adjust the policy only after measuring outcomes. The importer fails closed if there are more than 365 pending spool files.

## Apply source patch on Mac

Verify Mac is at v4.3.7 before applying the incremental patch. Do not mix with older v4.3.6 patches:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git log -1 --oneline

git apply --check ~/Downloads/bima-v4.3.7-to-v4.3.8-auto-discovery.patch && \
  git apply ~/Downloads/bima-v4.3.7-to-v4.3.8-auto-discovery.patch

sh -n tools/run_multisource_daily.sh
PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -q
git diff --check

git add -- README_DISCOVERY_V438.md \
  multi/daily_discovery_import.py \
  multi/tests/test_daily_discovery_v438.py \
  tools/run_multisource_daily.sh
git diff --cached --check
git commit -m 'feat: guarded daily discovery-to-priority queue import'
git push origin main
```

## Update server and verify without API calls

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main
sh -n tools/run_multisource_daily.sh
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover -s multi/tests -q
sudo -u fauzi -H crontab -l | grep run_multisource_daily
```

Previously reported ` M crawler_google.py` and `?? domains.json` should remain untouched. No `git reset --hard`, `git clean`, or indiscriminate staging. **Pulling this patch does not automatically enable queue writes or require a service restart.**

## Explicit cron opt-in only after verification

The existing user `fauzi` cron entry runs on the host's UTC clock:

```cron
30 20 * * * /home/fauzi/crawler-rebuild/tools/run_multisource_daily.sh
```

When the full tests and service checks pass, use `sudo -u fauzi -H crontab -e` to **replace** that exact existing line (do not add a second task) with:

```cron
30 20 * * * BIMA_DISCOVERY_AUTO_IMPORT=1 /home/fauzi/crawler-rebuild/tools/run_multisource_daily.sh
```

This runs daily at **20:30 UTC = 03:30 WIB next day**. No Brave key setup is required for Google. Keep `BIMA_ENABLE_BRAVE_DAILY` unset. Existing `multi-source.jsonl` remains the audit log, not the import source. **Do not manually execute the cron script just to test auto-import**, since it performs actual provider calls and may add live jobs eligible for real BIMA alerts.

### Verify after the first scheduled run (read-only)

```bash
sudo -u fauzi -H crontab -l | grep run_multisource_daily
sudo -u fauzi -H sh -c 'tail -n 1 "$HOME/bima-logs/multi-source-import.jsonl"'
sudo -u fauzi -H sh -c 'find "$HOME/bima-logs/multi-source-pending" -maxdepth 1 -name "pending-*.jsonl" -type f | wc -l'
systemctl is-active pandi-crawler-multi.service
systemctl is-active pandi-telegram-monitor.service
```

Look for `ok:true`, `new_jobs` no greater than `5`, and `brave_ingestion_enabled:false`. A nonzero pending-spool count is expected if provider candidates exceed five/day. Check `multi-source-errors.log` using `tail` without revealing API keys if a run failed.

## Rollback

To stop automatic DB additions, **restore the original cron line**, leaving source and private spool untouched. The next ordinary scheduled run then continues Google discovery in preview-only mode. Do not delete `multi-source-state.json`, `brave-monthly-quota.json`, `multi-source-pending`, `domains.json`, production SQLite, or any existing `pending_alert`; deleting those can destroy deduplication or audit state. No service restart is needed to disable this specific cron import path.

## Validation limits

This patch is tested offline with synthetic SQLite and a simulated cron interpreter, not on PANDI's actual production database. The shell cron smoke tests are skipped on macOS because `/usr/bin/flock` is Linux-only; the production server runs them. The bundled fixture on the development runner reports **364 tests run, 363 passed, 1 skipped**; actual server test totals may differ with its larger repository. The user must independently confirm cron, private spool permissions, outcome counts, and absence of unwanted alerts after activation. No Brave API, Google API, or BIMA notification requests are made during unit tests.
