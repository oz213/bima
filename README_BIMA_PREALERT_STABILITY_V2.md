# BIMA Stage 1 — Pre-Alert stability (incremental patch after `724883c`)

This patch is additive to the live Selective Telegram Pre-Alert v1 and unified
private config; it does **not** change the original legacy direct-alert path,
IndoBERT, Google/Brave/urlscan cadence, model parameters or production DB path.

## Delivery diagnostics without duplicate POSTs

- The existing BIMA API wire contract is unchanged: **one** HTTP POST, no redirects,
  successful only on **HTTP 200**, no automatic POST retry. HTTP 201, 202, 409,
  timeouts and connection problems remain `send_unknown` until the destination
  system is checked.
- New table `bima_prealert_dispatch_audit` records one attempt/case: start/end
  UTC, HTTP status (if available), elapsed milliseconds, confirmed/unknown
  result and exception **class name only**. It does **not** store response
  headers/body, URL query, token, screenshot, or raw exception message.
- A unique row per case prevents a duplicate POST even if someone accidentally
  resets an already-attempted case to `approved`.
- The table is initialized automatically and additively when the Telegram
  service opens the existing crawler SQLite. **Older cases** (e.g. `#3`) will
  have `audit=not_recorded`; a past HTTP status cannot be recovered from this patch.
- `dispatch_one()` returns `None` for a preflight failure/race, instead of `False`
  that could cause the Telegram loop to attempt `outcome['ok']`.

Read-only investigation (case ID, not job ID):

```bash
APP=/home/fauzi/crawler-rebuild
DB="$APP/multi/state/crawler_production.sqlite3"
sudo -u fauzi -H "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_prealert_triage.py" --db "$DB" --case 3
sudo -u fauzi -H "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_prealert_triage.py" --db "$DB"
```

For any `send_unknown` or `sending` case, check IDADX/BIMA receipt and its
API access log **before** changing status or considering another POST. This
patch deliberately provides **no force-retry command**.

## Re-open a manually investigated `needs_review` case

1. From the authorized private Telegram account, run `/investigasi #73186`
   (JOB ID), assess HTTP evidence independently. The investigation itself
   cannot approve the alert.
2. If appropriate, run `/tinjauulang #5` (PRE-ALERT CASE ID). Only
   `needs_review` can be re-opened; `sent`, `approved`, `send_unknown`, or
   `rejected` are refused. Only the configured private reviewer is authorized.
3. The bot re-sends the **original verified screenshot**, labelled as the old
   capture (not a fresh screenshot), with new buttons. Older buttons cannot
   be used after reopening. Reopening alone **never** posts to BIMA.
4. Click the new 'Judol - kirim BIMA' only when sufficient corroborating
   evidence exists. Other options remain 'Bukan Judol' and 'Bukti kurang'.

The original `human_inconclusive` event is preserved in the audit log.
Screenshot evidence must pass SHA-256 verification or re-opening is refused.

## Rollout in two changes (Mac > GitHub > Linux)

On Mac, from `/Applications/XAMPP/xamppfiles/htdocs/BIMA/bima`:

```bash
PATCH="$HOME/Downloads/bima-prealert-stability-v2.patch"
git status --short
git log -1 --oneline
git apply --check "$PATCH" && git apply "$PATCH"
PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-tests/bin/python" -B \
  -m unittest discover -s multi/tests -p 'test_prealert_stability_v2.py' -v
PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-tests/bin/python" -B \
  -m unittest discover -s multi/tests -p 'test_selective_prealert_v1.py' -v
git diff --check
# Stage only files from this patch; never add private configuration or keys.
git add -- multi/selective_prealert.py multi/telegram_monitor.py \
  multi/tests/test_prealert_stability_v2.py \
  tools/bima_prealert_triage.py README_BIMA_PREALERT_STABILITY_V2.md
git diff --cached --check
git commit -m "feat: audit prealert delivery and safely reopen deferred reviews"
git push origin main
```

On server, check local state before pulling (do not reset or clean):

```bash
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_prealert_stability_v2.py' -v
```

The Telegram bot must be restarted *when operationally safe* to activate the
new functions; do not restart the crawler merely for Telegram auditing.

```bash
sudo systemctl restart pandi-telegram-monitor.service
systemctl is-active pandi-telegram-monitor.service pandi-crawler-multi.service
journalctl -u pandi-telegram-monitor.service --since '2 minutes ago' --no-pager -n 40
```

The first new bot DB connection creates one audit table and adds one nullable
`review_nonce` column, leaving existing records intact. No DB backup step is
required. Never print `/home/fauzi/.config/bima/bima.json` or credentials.

## Scope exclusions

- No changes to the actual IDADX API, which must be checked separately.
- No API receipt ID/idempotency header because the receiver's protocol does
  not yet establish either. Do not invent or send headers without agreement.
- No automatically promoted `needs_review` or `send_unknown` cases.
- No full testing of the actual production Telegram API/IDADX backend, only
  synthetic SQLite and mocked POSTs; deploy canary carefully.
