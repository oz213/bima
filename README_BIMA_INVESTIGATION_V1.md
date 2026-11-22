# BIMA Investigation Engine v1 - HTTP-only on-demand evidence

This is a **non-autonomous foundation** for the future BIMA Investigation Agent.
It is **not** a local LLM, does not have an LLM tool loop, and cannot independently
judge whether a website owner committed abuse. Run it only on demand, separately
from the production crawler. There is no Telegram command added in this patch.

## What it does

- Fetch one `.id` domain/URL (HTTP-first), following at most 4 HTTP redirects.
- Inspect the final HTML (maximum 512 KiB); **no browser, JS, image, scripts,
  outbound anchor navigation, POST, file downloads, or model inference**.
- Inspect hidden external gambling-promotion links with the page title and
  HTML line number. A synthetic Sorot Hari Ini-style fixture exercises the
  `display:none` / `slot gacor` case.
- Identify visible gambling-brand pages with repeated login/registration/bonus
  outbound links. A synthetic JebolTogel-style fixture exercises this case.
- Optionally correlate with one exact ID in the existing `jobs` SQLite table,
  opened with `mode=ro` and `PRAGMA query_only=ON`.
- Mark disagreement when classic ML predicts gambling and IndoBERT predicts
  non-gambling. **IndoBERT is not removed, disabled, or modified**.
- Save a small JSON evidence summary; **no raw HTML**, user forms, or URL query
  strings. The SHA-256 of the fetched HTML supports limited provenance.

## Safety boundaries

- Initial URL must have a `.id` hostname (including subdomains / IDN).
- Only HTTP port 80 and HTTPS port 443. Redirects may leave `.id`, but each
  destination is revalidated. All resolved addresses must be global IPs;
  connections are pinned to a validated address (no DNS rebind between validation
  and TCP connect). TLS verification stays enabled. No proxy environment usage.
- Lock file in `multi/state` permits just one CLI investigation at a time.
- Maximum 8 seconds socket timeout per hop; operator CLI uses a separate
  30-second hard process deadline. No background service, cron or queue.
- Any timeout, blocked address, unsupported compression/content type, or
  no sufficient indicators yields **inconclusive / insufficient evidence**,
  never a "safe" verdict.
- The tool can report evidence of suspected compromised-site SEO spam, but
  **cannot establish compromise or ownership intent**.
- No modifications to existing alert decisions, Telegram bot, crawler workers,
  Selenium, database, IndoBERT, or learning DB; no auto-report/takedown.
- Treat output as sensitive security evidence. The JSON output is created with
  permission `0600` and never overwrites an existing file.

## Local application / test (Mac)

Apply patch from the BIMA Git repository root; patches only add three files:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
PATCH="$HOME/Downloads/bima-investigation-engine-v1.patch"
git apply --check "$PATCH"
git apply "$PATCH"
git diff --check
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_investigation_agent_v1.py' -v
```

Use a suitable Python 3.11+ virtualenv if your Mac's `python3` is older. Run
existing BIMA test suites separately before committing. When satisfied, commit
and push the three added files; pull on Linux as user `fauzi`.

## Server pilot (read-only / no restart)

```bash
APP=/home/fauzi/crawler-rebuild
DB="$APP/multi/state/crawler_production.sqlite3"

sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p test_investigation_agent_v1.py -q

# The external timeout and nice ensure this pilot stays below crawler priority.
OUT="$APP/multi/state/investigation-jebol-$(date -u +%Y%m%dT%H%M%SZ).json"
sudo -u fauzi -H nice -n 15 timeout -k 2s 30s \
  "$APP/venv/bin/python3" -B \
  "$APP/multi/investigation_agent_v1.py" \
  --target https://jebol.id/ --db "$DB" --job-id 47483 --out "$OUT"

OUT="$APP/multi/state/investigation-sorothariini-$(date -u +%Y%m%dT%H%M%SZ).json"
sudo -u fauzi -H nice -n 15 timeout -k 2s 30s \
  "$APP/venv/bin/python3" -B \
  "$APP/multi/investigation_agent_v1.py" \
  --target https://sorothariini.id/ --db "$DB" --job-id 64555 --out "$OUT"
```

Output contains current observations and optional old saved-job metadata,
which **may refer to different points in time**. Do not conflate them. A tool
category such as `hidden_gambling_seo_spam` is an evidence-based review flag,
not a legal or registrar enforcement verdict.

## What comes next

1. Verify output on the two above cases and add representative clean/benign
   news sites to the regression set.
2. Add immutable observation provenance and safe HTML excerpt storage policy.
3. Integrate a deliberately constrained, authorized Telegram `/investigasi`
   command (off by default) only after preproduction review.
4. Add an optional LLM explanation/reasoning layer with citations to observation
   IDs. A small local model must first pass resource/performance testing on
   the existing 2-vCPU, 3.8-GiB BIMA VM; never share crawler/Selenium workers.
