# BIMA Multi-Abuse Foundation v1 — Passive Phishing & Evaluation

**Target base commit:** `002d97d0fd74a403c94081b72afe0e371864d540` (`main`, 21 Nov 2026).

This stage is intentionally **risk-neutral**: BIMA's gambling classifier, shared IndoBERT, browser routing, screenshot RAM handling, BIMA alert payload, and `urls.json` inventory are unchanged. The new phishing logic **does not issue alerts or claim a phishing verdict**. It adds bounded metadata to the existing `classifier_json` only after explicit opt-in (`BIMA_PHISHING_SHADOW=1`) and deterministic sampling (default 5%). No HTTP requests, JavaScript execution, or downloads are performed by the new detector.

## Features

- `multi/phishing_signals.py` parses a bounded HTML prefix (256,000 characters) to recognize **password forms** with cross-origin, unencrypted, or non-HTTP form actions. It considers `<base href>` and `action` resolution; ordinary same-origin login forms are not called phishing. Results are non-sensitive structured codes (`review_recommended`, `login_form_only`, `insufficient_signal`, etc.), form counts, truncation flag, and a SHA-256 prefix hash. URLs, field values, input HTML, and tokens are not saved in the metadata. **Cross-origin login can be legitimate SSO**, so human verification is required.
- `multi/crawler_multi.py` attaches `phishing_triage` as nested optional metadata without changing the original `status`, `reason`, `ml_conf`, any IndoBERT call, or BIMA reporting. Shadow disabled by default, opt-in via environment variable. The sampling key is based on the initial URL, so repeated samples are stable.
- `multi/abuse_eval_pack.py` provides a separate read-only CLI to sample `jobs` from `alert`, `complete`, and `uncertain` states using deterministic reservoir sampling. Manifest includes job IDs and metadata but no raw URLs or HTML. It can calculate limited metrics from **human-reviewed** CSV labels for the *existing gambling detector*. It does not claim to evaluate phishing; that requires phishing-specific ground truth.
- Two new test files exercise HTML signal detection, accidental leakage, legacy classification preservation, deterministic sampling, read-only operation, and review metrics.

## Mac development workflow

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git branch --show-current
git log -1 --oneline
git status --short

PATCH="$HOME/Downloads/bima-multi-abuse-foundation-v1-002d97d.patch"
git apply --check "$PATCH"
git apply "$PATCH"
git diff --check

TEST_PY="$HOME/.venvs/bima-test/bin/python3"  # or existing venv 3.11/3.12
PYTHONPATH="$PWD/multi:$PWD" "$TEST_PY" -B -m unittest discover \
  -s multi/tests -p 'test*.py' -q
PYTHONPATH="$PWD/multi:$PWD" "$TEST_PY" -B \
  multi/tests/regression_classifier_rule.py crawler_url.py

# After checking all results:
git add -- README_BIMA_MULTI_ABUSE_V1.md multi/crawler_multi.py \
  multi/phishing_signals.py multi/abuse_eval_pack.py \
  multi/tests/test_phishing_signals_v1.py multi/tests/test_abuse_eval_pack_v1.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat: add passive phishing signals and abuse evaluation pack"
git push origin main
```

This project does not require XAMPP as a web server; the path above is only the local checkout location.

## Offline phishing smoke test on Mac

No live domain must be fetched to test the detector. Create a synthetic example:

```bash
printf '%s\n' '<form action="https://example.net/submit"><input type="email" name="email"><input type="password" name="password"></form>' > /tmp/bima-phishing-fixture.html
"$TEST_PY" -B multi/phishing_signals.py \
  --html-file /tmp/bima-phishing-fixture.html --url https://test.id/login
```

Expected `status`: `review_recommended`; expected signal `cross_origin_credential_action`. This is **not a phishing conviction**.

## Server rollout (after successful Mac tests + push)

Do not reset SQLite, delete WAL, re-import `urls.json`, change secrets, or enable auto-reporting.

```bash
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test*.py' -q
```

**Default rollout does not activate phishing shadow.** The existing crawler only picks up the code at its next controlled restart; with the flag unset, it behaves as before. If a small shadow canary is approved after reviewing RAM/CPU:

```ini
# systemctl edit pandi-crawler-multi.service
[Service]
Environment=BIMA_PHISHING_SHADOW=1
Environment=BIMA_PHISHING_SHADOW_RATE=0.01
```

Then do `sudo systemctl daemon-reload`, schedule a controlled crawler restart, and inspect `classifier_json` records for a `phishing_triage` field. This adds observations only; **no new BIMA phishing alert will be generated**. To disable, remove/reset both `Environment=` overrides and restart the crawler under change control. Because this is a new metadata field, verify downstream consumers can ignore it.

## Read-only evaluation sample from server

```bash
APP=/home/fauzi/crawler-rebuild
DB="$APP/multi/state/crawler_production.sqlite3"
OUT="$APP/multi/state/bima-abuse-review-$(date -u +%Y%m%d).json"
sudo -u fauzi -H "$APP/venv/bin/python3" -B \
  "$APP/multi/abuse_eval_pack.py" sample \
  --db "$DB" --out "$OUT" --seed 20261121 \
  --alert 50 --complete 100 --uncertain 50
```

- The `jobs` database is opened with SQLite `mode=ro`, `query_only=ON`, and a read transaction. Existing files are not overwritten; output is created with mode `0600`.
- The manifest contains **job IDs**, so keep it in `multi/state/` (ignored from Git) and distribute only to authorized analysts. It cannot by itself prove that any domain is abusive.
- `status_updated_at` is a status timestamp, **not** proof of when the HTML was captured. An observation history is a subsequent project phase.
- Results with retry/DNS failure are not automatically labeled safe.

After reviewers label a representative subset in a separate local CSV:

```csv
job_id,label
123,gambling
124,non_gambling
125,uncertain
```

Evaluate the existing gambling detector only:

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" -B \
  "$APP/multi/abuse_eval_pack.py" evaluate \
  --manifest "$OUT" --labels /home/fauzi/private/bima-review-labels.csv
```

Precision/recall values are **only for reviewed, scorable cases**; selective review alone cannot estimate full-population quality. Keep unreviewed and uncertain counts visible. Human-reviewed phishing labels, evidence archives, and representative sampling are necessary before training or automatically reporting phishing.

## Boundaries & next phase

1. Add immutable observation records and evidence provenance for each scan.
2. Improve .id abuse sample review coverage and label taxonomy (site abuse vs text content).
3. Add phishing-specific verified cases and evaluate signal precision before introducing a trained phishing model.
4. Add domain/redirect/infrastructure correlation and model shadow-mode evaluation.
5. Keep DNS Abuse categories (phishing, malware, botnet, pharming, spam used for delivery) distinct from policy/content abuses such as illegal gambling.

**Not included:** production phishing verdict, automated takedown, page fetching from the new modules, TLS/DNS graph, malware execution, or IndoBERT fine-tuning.
