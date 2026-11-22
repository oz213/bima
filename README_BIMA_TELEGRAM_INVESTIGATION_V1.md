# BIMA Telegram Investigation v1 — opt-in, HTTP-only

This patch integrates the existing `multi/investigation_agent_v1.py` into the
existing `pandi-telegram-monitor.service` using a new `/investigasi` command.
It does **not** create an AI tool-calling loop or a general LLM; it displays
verified, bounded HTTP/HTML evidence for a registered reviewer. The old
`/analisis` command remains unchanged.

## What changes

- `multi/telegram_monitor.py`: instantiates and delivers a bounded optional
  investigator when `BIMA_INVESTIGATION_ENABLED=1`.
- `multi/telegram_investigation.py`: one worker with at most one outstanding
  investigation; it runs the existing HTTP-only engine in a subprocess, with
  `nice -n 15` (where available), **30 s hard timeout**, private reviewer
  authorization, no arbitrary shell execution, and no Telegram/API secrets
  passed to the child.
- `multi/tests/test_telegram_investigation_v1.py`: offline security, timeout
  behavior, worker concurrency, read-only job lookup and report tests.

## Commands (only private chats of authorized reviewers)

```
/investigasi sorothariini.id
/investigasi https://jebol.id/
/investigasi #47483
/investigasi #64555
```

Using `#job` correlates the HTTP observation with the saved BIMA job ID, so
model disagreement is shown where present. Domain/URL-only queries do not
silently assume a particular historical job. `/investigasi` does not enqueue
BIMA jobs or re-send existing alerts. Its output is not a definitive abuse
verdict; no auto-takedown, no site-wide compromise assertion.

The HTTP target must be a public `.id` domain. Redirects can leave `.id` only
through the existing engine's SSRF protections (global DNS addresses, pinned
connections, TLS verification, standard ports). No JavaScript, Selenium,
HTML persistence, file downloads, third-party link traversal or model inference.
The engine uses its existing process-wide file lock, so it cannot overlap
another CLI investigation. It remains conservative when a site cannot be read.

## Installation on Mac before pushing to GitHub

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
PATCH="$HOME/Downloads/bima-telegram-investigation-v1.patch"
git apply --check "$PATCH"
git apply "$PATCH"
git diff --check

PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_telegram_investigation_v1.py' -v
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_investigation_agent_v1.py' -v
# Then run the full repository regression suite with your established venv.

git add -- multi/telegram_monitor.py multi/telegram_investigation.py \
  multi/tests/test_telegram_investigation_v1.py \
  README_BIMA_TELEGRAM_INVESTIGATION_V1.md
git diff --cached --check
git commit -m "feat: add opt-in Telegram HTTP investigations"
git push origin main
```

## Server rollout (fauzi account only, preserve all local modifications)

```bash
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_telegram_investigation_v1.py' -v
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_investigation_agent_v1.py' -v
```

If Git reports permission or local-change conflicts, resolve those first;
**do not** run `reset --hard` or `clean`. Preserve production SQLite, WAL,
`urls.json`, tokens and local overrides.

Add to the existing private, already-loaded
`/home/fauzi/.config/bima/analyst.env`:

```ini
BIMA_INVESTIGATION_ENABLED=1
```

Use `sudoedit` and do not change `BIMA_ANALYST_ENABLED`,
`BIMA_ANALYST_PROVIDER`, `TELEGRAM_BOT_TOKEN`, or
`TELEGRAM_ALLOWED_CHAT_IDS`. Existing reviewer rules apply; optionally
restrict reviewers with `TELEGRAM_REVIEW_USER_IDS` as already supported.

```bash
sudo systemctl restart pandi-telegram-monitor.service
sudo systemctl is-active pandi-telegram-monitor.service pandi-crawler-multi.service
sudo systemctl show pandi-telegram-monitor.service -p EnvironmentFiles --no-pager
sudo journalctl -u pandi-telegram-monitor.service -n 50 --no-pager
```

Verify by using `/help` and `/investigasi #64555` in an **authorized private
Telegram chat**, then `/investigasi #47483`. The first should flag hidden link
spam if the site still serves the same HTML; the second should flag possible
visible gambling promotion and historical model disagreement. HTTP evidence
can change and no inference runs during the command.

## Rollback

Set `BIMA_INVESTIGATION_ENABLED=0` in the same private environment file and
restart **only** `pandi-telegram-monitor.service`. No DB migration, schema
change, crawler restart, model changes, or alert changes are necessary.

## Limitations and next phase

This is a light, deterministic HTTP-only investigator, **not** a conversational
LLM. For ChatGPT-like reasoning, add a model to interpret structured evidence
with source-citation validation and limited, explicitly permitted tools.
On the existing ~2 vCPU / 3.8 GiB RAM server, do not load a large local model
alongside IndoBERT + Chrome. Benchmark small options carefully or use an
approved external inference API while keeping the BIMA service on this server.
