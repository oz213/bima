# BIMA v4.3.5 — Brave Search: quota guard & transient-only results

**Scope:** harden the already-integrated Brave API adapter in v4.3.4. This is **not** an approval to use Brave in a domain-abuse surveillance workflow, nor is it an automatic enqueue integration. Preserve Google + urlscan and the BIMA crawler/IndoBERT unchanged.

## Policy gate (read this first)

Brave Search API Terms of Use, last updated **1 September 2026**:
<https://api-dashboard.search.brave.com/terms-of-service>

- Terms restrict storing/caching Search Results beyond transient operational storage. v4.3.5 **does not output, persist, hash into state, or enqueue any URL originating from Brave results**. Only aggregate counts are returned. No titles, snippets, raw responses, search result links, or per-URL Brave provenance are stored.
- Terms also restrict API use in connection with activities described as **surveillance**, among others. Whether PANDI's legitimate domain-abuse monitoring is within an authorized use needs clarification from Brave/appropriate legal review. **Do not enable daily Brave calls until authorization is confirmed.** Transient-only handling does not by itself resolve this question.
- Terms prohibit using Search Results to train, evaluate or improve AI models. BIMA does not pass Brave Search Results to IndoBERT in this patch; no model evaluation/training is added.
- If PANDI requires retaining Brave-derived URLs for crawler queues or evidence, obtain explicit rights/authorization from Brave first. This patch deliberately offers no bypass for Brave URL retention.

## Changes

1. `multi/brave_quota.py`: process-safe, private Brave-only monthly API **attempt** reservation ledger (UTC calendar month; default **900**, maximum **1,000**). Uses file lock and atomic replace. Stores `{version, month_utc, attempts}` only — **no search results or API key**.
2. `multi/multi_source_discovery.py`: Google/urlscan behavior unchanged. Brave discovered URLs remain temporary in memory for scope validation and aggregate counters; never added to JSONL `preview_candidates`, stored URL hashes, shared source lists or SQLite. A missing `--brave-monthly-ledger` prevents Brave calls. Supports `BRAVE_SEARCH_API_KEY` via environment without modifying `config.json`.
3. `tools/run_multisource_daily.sh`: existing cron still works, but **Brave is explicitly disabled by default**, even if an API key exists in `config.json`. Google stays 14 requests/day; urlscan remains 2/day when configured. An authorized opt-in `BIMA_ENABLE_BRAVE_DAILY=1` requires a private `~/.config/bima/brave-api-key` file and enables a maximum 14 Brave calls/day. The ledger caps this process at 900 attempted requests/month.
4. New unit tests cover concurrent quota reservations, UTC month rollover, symlink/permission failures, no Brave URL persistence, provider error redaction, and backwards-compatible Google/urlscan state.

**Important:** local usage ledger does not know earlier API tests from another machine, other API keys, other clients or other deployments. It is **not** an account-wide spending guarantee. The dashboard's **1,000 monthly request limit and Free spend limit** are the authoritative safeguards; make sure both are saved. A conservative 900 local cap does not override those controls.

## Read-only checks / test plan (no network calls)

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover -s multi/tests -q
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B multi/multi_source_discovery.py \
  --domains domains.json --config config.json \
  --providers google,brave,urlscan \
  --brave-monthly-ledger /home/fauzi/bima-logs/brave-monthly-quota.json
```

Plan mode creates no ledger, even when the path is supplied.

### Safe setup for a Brave credential (only *after* policy clearance)

The `fauzi` user can store the key in its own private key file. **Never paste the value into chat, put it in Git, or pass it as a shell command argument.** The following prompt does not echo the key:

```bash
sudo -u fauzi -H install -d -m 0700 /home/fauzi/.config/bima
sudo -u fauzi -H /home/fauzi/crawler-rebuild/venv/bin/python3 -c '
import getpass, os
path="/home/fauzi/.config/bima/brave-api-key"
key=getpass.getpass("Brave API key: ").strip()
if not key: raise SystemExit("Empty API key")
fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
try:
    os.fchmod(fd, 0o600)
    os.write(fd, (key+"\n").encode())
finally: os.close(fd)
'
```

### One-request, transient-only confirmation (optional; consumes quota)

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H bash -s <<'BASH'
set -euo pipefail
cd /home/fauzi/crawler-rebuild
IFS= read -r BRAVE_SEARCH_API_KEY < "$HOME/.config/bima/brave-api-key"
export BRAVE_SEARCH_API_KEY
PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B \
  multi/multi_source_discovery.py --domains domains.json --config config.json \
  --providers brave --execute --acknowledge-quota \
  --max-brave-requests 1 --results-per-request 5 --preview-limit 0 \
  --brave-monthly-ledger "$HOME/bima-logs/brave-monthly-quota.json" \
  --brave-monthly-request-cap 900
BASH
```

The output may show `brave_transient_valid_results > 0` yet `new_candidates=0` and `preview_candidates=[]` because Brave results are deliberately not retained. No production database, browser, Telegram or BIMA alert is touched.

### Daily execution (only after policy clearance)

The existing cron line is still the right one:

```cron
30 20 * * * /home/fauzi/crawler-rebuild/tools/run_multisource_daily.sh
```

To **opt in** after permission is confirmed and the key file is protected, replace that *same* line (do **not** add another) with:

```cron
30 20 * * * BIMA_ENABLE_BRAVE_DAILY=1 /home/fauzi/crawler-rebuild/tools/run_multisource_daily.sh
```

The scheduler uses UTC; 20:30 UTC = 03:30 WIB the following calendar day. If unsure of license permission, leave `BIMA_ENABLE_BRAVE_DAILY` unset and Google continues running.

Logs continue under `/home/fauzi/bima-logs/multi-source.jsonl`. Brave monthly reservations are in `brave-monthly-quota.json` (0600) plus a `.lock` file. Those files are **not** to be tracked in Git. Do not delete the ledger mid-month or its local cap would reset.

## Compatibility and limitations

- No changes to `crawler_google.py`, `crawler_multi.py`, `crawler_url.py`, IndoBERT, `urls.json`, `domains.json`, SQLite production DB, or either BIMA systemd service.
- This patch **cannot deliver Brave-discovered URLs to the BIMA queue**; only count-level analytics are available. A future independently reviewed, permissioned transient processing path is needed for actionable Brave findings.
- This patch does not add Common Crawl, CT, or automatically paid providers.
- A hard Google cutoff remains January 1, 2027; v4.3.5 does not change it.
