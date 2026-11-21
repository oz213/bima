# BIMA v4.3.3 — SLD .id Google Discovery (Preview-Only)

## Purpose

Use the 14 suffix scopes in `domains.json` as *search scopes*, **not hosts to visit**. Add `multi/google_discovery.py` as a bounded, provider-specific Google Custom Search JSON API source. It reuses the existing `proactive_discovery.normalize_url()` validator and keeps query strings, credentials and off-scope URLs out of candidate output. The original `crawler_google.py`, `crawler_multi.py`, `crawler_url.py`, IndoBERT, registry `urls.json`, and the SQLite production DB remain unchanged.

**Google deprecation:** Google says the Custom Search JSON API will be discontinued for existing customers **January 1, 2027**. The API is already closed to new customers. Keep this source replaceable. See <https://developers.google.com/custom-search/v1/overview>. Do not build a long-term dependency on Google CSE.

## Input

`domains.json` is a local untracked runtime file. Expected:

```json
{"domains": [".ac.id", ".ai.id", ".biz.id", ".co.id", ".desa.id", ".go.id", ".id", ".mil.id", ".my.id", ".net.id", ".or.id", ".ponpes.id", ".sch.id", ".web.id"]}
```

A leading dot indicates a DNS suffix for matching; `.id` is a broad fallback overlapping the 13 narrower scopes. Queries are normalized to `site:co.id (...)` for `.co.id`, and `site:.id (...)` for `.id`. **Google search operators are not security boundaries**: returned URLs are independently checked against the selected suffix, `.id` TLD, hostname, path, scheme, and other existing BIMA candidate restrictions.

Queries are derived from at most six sanitized `QUERY_KEYWORDS` entries in `config.json`. Keys `GOOGLE_API_KEY` and `GOOGLE_CX` may be literal strings or `${ENV_VARIABLE}`. Neither value is printed.

## Run locally on Mac or Linux (no internet calls)

From the repo root:

```bash
PYTHONPATH="$PWD/multi:$PWD" python3 -B multi/google_discovery.py \
  --domains domains.json --config config.json
```

If `domains.json` does not exist on Mac, create a local **untracked** file with the above list. Never commit `config.json` or secret data. This command reads files and prints the query plan. It does **not** request Google, start a browser, access the crawler DB, enqueue jobs, or send alerts.

## Optional, explicit Google API preview (will consume quota)

```bash
PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B multi/google_discovery.py \
  --domains domains.json --config config.json \
  --execute-google-api --acknowledge-quota \
  --max-api-requests 2 --results-per-request 10
```

- At most **2 billable/free-quota requests** by default, independent of the number of scopes; 14 maximum in a single invocation. One page (`start=1`) per chosen scope; no retry, no pagination, no sleeping in plan mode.
- Results only include `.id` URLs which match the *queried* suffix; deduplication is by the existing BIMA normalized URL (queries and fragments dropped).
- `source=google_cse_index` and `evidence_level=search_index_signal` mean a **search-index finding**, **not** proof of live content, gambling, or abuse.
- Google CSE may return incomplete or stale results, and a `site:` query need not enumerate all hosts below a suffix.
- HTTP errors such as `403`, `429`, or invalid JSON stop further API requests. API key and raw error URL are not emitted.
- **No `--apply` option**, no SQLite write, no BIMA alert, no Chrome or Selenium; this is not the old `crawler_google.py` runtime.

## Limitations / next steps

1. `crawler_google.py` remains untouched because the server has a one-line local `BASE_DIR` change and is owned by root; do not overwrite or commit the file during this patch.
2. The untracked `domains.json` and production `config.json` are not part of this patch. Existing ACLs granting `fauzi` read access are sufficient; do not globally open permissions.
3. This phase intentionally does **not** enqueue or compare against the production SQLite queue; it returns candidates for manual review. Do not label it as fully integrated into the crawler.
4. Before background scheduling or enqueue integration, add a persistent quota ledger, checkpoint/cache, review gates, and provider-neutral replacement for Google CSE before 2027-01-01.
5. Safe browser isolation belongs to v4.4. Preserve the shared IndoBERT inference worker and HTTP-first crawler architecture.

## Tests

```bash
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover -s multi/tests -p 'test_google_discovery_v433.py' -v
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover -s multi/tests -q
```
