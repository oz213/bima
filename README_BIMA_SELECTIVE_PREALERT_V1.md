# BIMA Smart Learning & Selective Pre-Alert v1

Target commit upstream: `d1c8956` (`feat: add Telegram investigation command`).

**OFF BY DEFAULT.** This patch retains every original legacy direct BIMA alert for candidates **not selected**. It changes routing only for **up to 10 selected already-positive candidate alerts per UTC day**:

- Up to **6** candidates with strong indicators (hidden-link evidence or high-risk confidence >= 0.85).
- Up to **3** cases where the IndoBERT and Classic ML predictions disagree.
- Up to **1** deterministic hash-sampled medium-risk alert (approximately 1-in-8 eligibility, not a statistically uniform random sample).
- Unused slots **do not automatically transfer** between categories in v1. Therefore the Telegram total can be fewer than 10/day.
- **All other positive alert candidates** use the **unchanged** crawler `engine.send_alert()` call, without a Telegram approval requirement.
- Domains classified as safe, unreachable or inconclusive are **not auto-alerted** merely to fill a sample quota.

## Workflow

```
positive crawler candidate + screenshot PNG
  |-- selection OFF / not selected --> original direct BIMA API alert
  `-- selected --> durable pending_telegram + local restricted screenshot
           |-- Telegram sendDocument original PNG -> private reviewer
           |-- Judol -> approved -> one BIMA POST with same payload -> alerted
           |-- Bukan Judol -> rejected; no BIMA alert, saved learning label
           `-- Bukti kurang -> needs_review; no BIMA alert, manual investigation
```

The new Telegram `/antrian` command shows pending counts. The existing `/review`, `/investigasi`, `/cek`, IndoBERT, Classic ML and Selenium functions stay intact. The previous periodic summary sender is **not** disabled by Python code; stop its systemd timer as shown below.

## Critical security prerequisite — rotate exposed Telegram token

A bot token is present in the tracked `multi/telegram_monitor.env` on the public repository. Assume compromise, **revoke/rotate through BotFather**, place the new token only in a private server EnvironmentFile, and rewrite/remove the Git history that published the old token if possible. Never commit a new token or API key. A simple `git rm` does not erase it from prior commits. Do not execute a production deployment until this is remediated.

## What changes, and what does not

- One additive table `bima_prealerts`, one append-only event table `bima_prealert_events` in the existing production crawler SQLite database.
- Selected screenshots are saved temporarily under `<crawler DB parent>/prealert_evidence/`, directory `0700`, files `0600`, max 4 MiB per screenshot, 512 MiB aggregate for outstanding cases. They are deleted when BIMA confirms sending or the reviewer rejects. They remain for needs-review/send-unknown until manual resolution. This is a **documented exception** to the old no-local-screenshot policy, needed so the exact approved image reaches the BIMA API.
- Original PNG is delivered using Telegram `sendDocument` (POST multipart, **not** `sendPhoto`, which might recompress it). Metadata records the Telegram `file_id`, message ID and PNG SHA-256.
- The approved delivery uses the same HTTP JSON payload, headers, `confidence_level` calculation, status 200 contract, connect/read timeouts and no automatic POST retry as `crawler_url.send_alert()`. Only selected cases use this lightweight dispatcher, avoiding a second IndoBERT model initialization in the Telegram service.
- Each case holds the crawler job ID, capture SHA-256, selected bucket, frozen classifier metadata, human label, reviewer ID and lifecycle event log. Learning labels can be exported offline; **no model is retrained automatically**.
- If a Telegram screenshot upload fails, the selected case remains queued, with a minimum 5-minute retry interval. If a BIMA POST returns a non-200 response, times out, or crashes mid-flight, it stays `send_unknown`/`sending`; **never automatically replay it** before checking the destination BIMA system for an existing alert.
- On exception during the selection transaction, the crawler follows a **fail-closed prealert_error** path (no BIMA API call). Candidate selection that returns `False` is the normal **legacy direct alert** path.
- If quota or screenshot storage cap is reached *before selection*, the candidate is not selected, so it follows the original direct BIMA alert path.

## Local deployment (Mac)

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
PATCH="$HOME/Downloads/bima-smart-learning-selective-prealert-v1.patch"
git branch --show-current
git status --short
git log -1 --oneline
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover -s multi/tests -p 'test_selective_prealert_v1.py' -v
# Also run existing crawler, review, alert transport and Telegram tests:
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover -s multi/tests -v
# REVIEW results before committing. Do not stage public credentials!
git add -- multi/crawler_multi.py multi/telegram_monitor.py \
  multi/selective_prealert.py multi/prealert_feedback.py multi/prealert_probe.py \
  multi/tests/test_selective_prealert_v1.py README_BIMA_SELECTIVE_PREALERT_V1.md
git diff --cached --check
git commit -m "feat: selective Telegram screenshot approval before BIMA alerts"
git push origin main
```

Do not use `git add -A` until old credential files have been removed from tracking.

## Server preflight BEFORE enabling feature

```bash
APP=/home/fauzi/crawler-rebuild
DB="$APP/multi/state/crawler_production.sqlite3"
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" "$APP/venv/bin/python3" -B \
  -m unittest discover -s "$APP/multi/tests" -p 'test_selective_prealert_v1.py' -v
# Backup SQLite through the online-backup API rather than copying only the WAL file.
sudo -u fauzi -H "$APP/venv/bin/python3" - "$DB" "$DB.backup-prealert-$(date -u +%Y%m%dT%H%M%SZ)" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as src, sqlite3.connect(sys.argv[2]) as dst:
    src.backup(dst)
print('SQLite backup completed')
PY
```

### Validate Telegram multimedia connectivity

Set `TELEGRAM_BOT_TOKEN` (new rotated token) and `BIMA_PREALERT_REVIEWER_CHAT_ID` in a **private shell environment** when running `multi/prealert_probe.py`:

```bash
# Run with secure environment variables already loaded, without echoing tokens:
PYTHONPATH="$APP/multi:$APP" "$APP/venv/bin/python3" -B "$APP/multi/prealert_probe.py"
```

A one-pixel PNG **TEST** should arrive as a Telegram document. This is a real HTTP POST upload: if it fails, **do not enable pre-alert selection**. Fix the server's HTTPS POST/proxy configuration first. The probe does not send anything to BIMA.

### Enable both services (only after preflight succeeds)

For `pandi-crawler-multi.service`, create an override using `sudo systemctl edit pandi-crawler-multi.service`:

```ini
[Service]
Environment=BIMA_PREALERT_ENABLED=1
```

For `pandi-telegram-monitor.service`, use the existing **private** environment file `/home/fauzi/.config/bima/analyst.env` (provided its systemd drop-in already reads that file):

```ini
BIMA_PREALERT_ENABLED=1
BIMA_PREALERT_REVIEWER_CHAT_ID=<PRIVATE_TELEGRAM_USER_ID>
BIMA_PREALERT_CONFIG_PATH=/home/fauzi/crawler-rebuild/config.json
```

Set `BIMA_PREALERT_CONFIG_PATH` to the *actual* private config used by the legacy crawler (`PANDI_CRAWLER_BASE_DIR/config.json`); it must contain the existing `ENDPOINT_URL`, `X_API_KEY`, and optional timeout keys. The bot service needs read access to that file and write access to the crawler DB / `prealert_evidence` directory. Never publish its contents.

After verifying the overrides:

```bash
sudo systemctl daemon-reload
# Restart just the bot first; restart crawler deliberately at an appropriate point.
sudo systemctl restart pandi-telegram-monitor.service
sudo systemctl restart pandi-crawler-multi.service
sudo systemctl is-active pandi-telegram-monitor.service pandi-crawler-multi.service
```

Do not restart the crawler during an important live run solely to enable the feature. Review `systemctl cat ...` and use your ordinary change window.

### Disable `PANDI CRAWLER | MONITORING BERKALA`

```bash
sudo systemctl disable --now pandi-telegram-periodic.timer
sudo systemctl stop pandi-telegram-periodic.service
systemctl list-timers --all | grep -Ei 'telegram|periodic' || true
sudo systemctl is-active pandi-telegram-monitor.service pandi-crawler-multi.service
```

This stops the periodic sender only; keep the Telegram monitor and crawler running. If the timer unit name differs, inspect `systemctl list-timers --all` and disable the relevant unit. The Python function `multi/telegram_periodic.py` stays intact for manual use.

## Telegram test

1. Send `/antrian` from the private authorized reviewer account.
2. When a candidate is selected, receive `BIMA | VERIFIKASI PRE-ALERT` **with a PNG screenshot document**.
3. Tap **Bukan Judol** on a safe synthetic test candidate: the alert must **not** go to BIMA. The feedback remains stored.
4. Tap **Judol - kirim BIMA** only for a confirmed real positive. The dispatcher sends **exactly one** API POST and reports either success or unknown delivery to Telegram.
5. For ambiguous results tap **Bukti kurang**; inspect with `/investigasi #JOB` before reprocessing via your operational review procedure. v1 does not automatically recrawl or reopen such cases.
6. Independently verify non-selected positive candidates continue through the original immediate BIMA path.

**Caution:** the built-in unit tests use mocked sender responses; they do not validate the live network, the real Telegram upload proxy, nor the destination's deduplication behavior. Make a small canary run and inspect production logs/DB before enabling globally.

## Export verified labels (offline learning)

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" \
  "$APP/multi/prealert_feedback.py" --db "$DB" \
  --out "$APP/multi/state/bima-prealert-feedback.jsonl"
```

Do not train directly from `inconclusive`. Review content drift and maintain a separate validation set before model updates.

## Inspect queue without disclosing screenshot bytes

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" - "$DB" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as conn:
    for row in conn.execute('SELECT status,count(*) FROM bima_prealerts GROUP BY status'):
        print(*row)
PY
```

## Rollback

Disable `BIMA_PREALERT_ENABLED` in the **crawler** override to restore the original direct path for **new** alert candidates; do **not** automatically release already selected pending cases without review. Keep the Telegram service running until existing reviews are resolved. If you disable Telegram selection, also disable its dispatcher after recording and reconciling outstanding cases. No destructive database migration is required.
