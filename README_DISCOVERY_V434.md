# BIMA v4.3.4 — Multi-Source Domain Discovery (Preview Only)

## Goal and safety

Use **14 suffix scopes** from the existing untracked `domains.json` to find candidate `.id` URLs through separate search intelligence providers, preserving the existing keywords **`slot OR gacor`** from `config.json`. **Do not** run the old `crawler_google.py` as part of this workflow: it launches legacy Selenium (`--no-sandbox`) and can send BIMA alerts.

New module: `multi/multi_source_discovery.py`. Read-only providers: **Google Custom Search JSON API**, **Brave Search API**, **urlscan.io Search API** (historical scans; **not** live scanning or scan submission). Provider adapters are separate; absent API credentials skip that provider rather than blocking the configured Google source.

**There is no automatic enqueue or BIMA abuse verdict**. The module never imports the crawler engine, starts Chrome, sends Telegram/BIMA alerts, or opens/writes the crawler SQLite database. Shared IndoBERT, `crawler_multi.py`, `crawler_url.py`, `crawler_google.py`, `urls.json`, and `domains.json` are not modified. Opt-in state writes persist **SHA-256 URL hashes only**, outside the production DB.

| Provider | Discovery type | Query pattern / source | Secret key | Bounded daily runner |
| --- | --- | --- | --- | --- |
| Google | Search index signal | `site:web.id ("slot" OR "gacor")` | Existing `GOOGLE_API_KEY`, `GOOGLE_CX` | 14 requests |
| Brave | Search index signal | `site:web.id ("slot" OR "gacor")` | `BRAVE_SEARCH_API_KEY` | 2 requests |
| urlscan | Historical scan metadata | `page.domain:web.id AND (page.title:slot OR task.url:slot OR ...) AND date:>now-7d` | `URLSCAN_API_KEY` | 2 requests |

The code normalizes each candidate through the existing `google_discovery.normalize_candidate()` / `proactive_discovery.normalize_url()`, requiring a hostname in the queried suffix and a public `.id` HTTP(S) URL. Query strings/fragments are stripped; logout-type paths, unsupported extensions, userinfo and non-default ports are rejected. Search-result evidence is **never proof of gambling**, abuse, or current live content. The broad `.id` query overlaps the other scopes and is deduplicated.

Google CSE has a published **2027-01-01** end date for existing customers, and is automatically skipped on/after that day. Official docs: https://developers.google.com/custom-search/v1/overview . Brave API docs: https://api-dashboard.search.brave.com/app/documentation/web-search . urlscan Search API: https://docs.urlscan.io/pages/search-api-reference . Provider API quotas and billing depend on account settings; a per-run cap is **not** a permanent account-level quota guarantee.

## Plan mode (safe, no external API calls)

Run from repository root, after applying patch:

```bash
PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B \
  multi/multi_source_discovery.py --domains domains.json --config config.json \
  | ./venv/bin/python3 -m json.tool
```

Plan mode reads configuration **without printing API tokens**, prints each provider's planned requests, uses UTC-day deterministic rotation for smaller budgets (2 requests/day visits all 14 suffixes over 7 days), does **not** call any provider or modify state/DB.

## One-request live smoke test (opt-in; quota may be charged)

```bash
PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B \
  multi/multi_source_discovery.py --domains domains.json --config config.json \
  --providers google,brave,urlscan \
  --execute --acknowledge-quota \
  --max-google-requests 1 --max-brave-requests 1 --max-urlscan-requests 1 \
  --results-per-request 5 --request-interval 1 \
  | ./venv/bin/python3 -m json.tool
```

A provider with missing credentials is marked `skipped_missing_credentials`, without an API call. A provider returning 403/429 stops **only that provider**, reports a sanitized error, and overall `ok` becomes false. No retries/redirect-following to API endpoints. Responses from Brave/urlscan are capped at 1 MiB; search results are never fetched at their own URLs.

To configure Brave/urlscan, use `BRAVE_SEARCH_API_KEY` and `URLSCAN_API_KEY` in the **local protected** `config.json` or use `${ENV_VARIABLE}` references resolved from the process environment. Do not commit keys and do not put raw tokens into cron lines or shell history. urlscan search access may require a registered account/key and is subject to quotas. Empty URL list means the provider found no matching indexed results; not that namespace is clean.

## One daily scheduler (replaces legacy Google-only cron)

`tools/run_multisource_daily.sh` launches one daily execution as user `fauzi`, using `/usr/bin/flock`, a restricted log directory (`umask 077`), separate JSONL logs, and `--state-file "$HOME/bima-logs/multi-source-state.json"`. The state file is an opt-in **hash-only** dedup cache (100,000 entries cap, mode 0600), not a crawler queue, not a query cache. Only emitted preview candidates are recorded as seen; truncated candidates are *not* silently lost. The script runs the following maximum per day: **14 Google + 2 Brave + 2 urlscan API calls**, which may consume billable quota; missing Brave/urlscan keys are skipped. No scheduled scans happen until the user's crontab is explicitly edited.

**IMPORTANT**: user `fauzi` currently has a legacy cron line for `multi/google_discovery.py` at `30 20 * * *`. **Replace that line**, do not add a second job or Google will be charged twice. First validate the new script in plan mode, and optionally one small live test. When ready, open `sudo -u fauzi -H crontab -e`, remove the old Google-only line and add exactly:

```cron
30 20 * * * /home/fauzi/crawler-rebuild/tools/run_multisource_daily.sh
```

On server timezone `Etc/UTC` this triggers at **03:30 WIB** the following calendar day. No service restart is required. Do not edit root crontab by mistake. Verify `sudo -u fauzi -H crontab -l` afterwards; the crawler systemd service remains independent.

If a different log location is required, set `BIMA_DISCOVERY_LOG_DIR` in a user-owned wrapper script or service environment; do not place API credentials into a crontab command line.

### Verify one run

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H crontab -l
journalctl -u cron --since '24 hours ago' --no-pager | grep run_multisource_daily || true
LOG=/home/fauzi/bima-logs/multi-source.jsonl
if [ -s "$LOG" ]; then tail -n 1 "$LOG" | ./venv/bin/python3 -m json.tool; fi
```

Observe `providers.*.status`, `api_requests`, `new_candidates`, `already_seen_candidates`, `preview_candidates`, `state_file_updated`, `db_writes=0`, `alerts_sent=0`, `new_jobs=0`.

## Design limits and future source adapters

- **Certificate Transparency (crt.sh):** broad `%.id` / `%.co.id` requests risk huge responses, so CT should use bounded **per-registered-domain seeds** rather than 14 whole SLDs. Existing proactive discovery already offers an opt-in CT mechanism. Not integrated into this v4.3.4 daily runner.
- **Common Crawl:** broad SLD/TLD CDX wildcard queries are expensive and heavily rate-limited; add a weekly seeded-domain batch in a future safe adapter, not an unbounded daily scan.
- **URLhaus:** malware-download IOC feed, **not** a gambling search index; downloads require an Auth-Key, and its results should have an independent threat label. Not included by default.
- **Registry `urls.json` and DNS/sitemap:** already handled by BIMA baseline and proactive discovery. Do not duplicate the Registry domain baseline through provider queries.
- Per-provider request caps + checkpoint of seen URLs **do not** implement a 24-hour quota ledger or API call caching. Add these before broader deployment or paid accounts.
- Running without isolated Chrome does not weaken browser security because no browser is launched; Chrome isolation remains a separate v4.4 hardening project.

## Tests

```bash
PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B \
  -m unittest discover -s multi/tests -p 'test_multisource_v434.py' -v
PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B \
  -m unittest discover -s multi/tests -q
```

All newly added tests use synthetic API responses and never access production credentials, network, SQLite, browsers or Telegram.
