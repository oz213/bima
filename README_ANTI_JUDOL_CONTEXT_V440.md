# BIMA v4.4.0 — Anti-judol rejection context (false-positive regression)

## Proven incident, not a domain allowlist

On 21 November 2026, the operator inspected BIMA production read-only:

| URL hostname | jobs.id | status | classifier.reason | ML prediction | Gambling probability (`ml_conf`) |
|---|---:|---|---|---|---:|
| `lapakbacklink.co.id` | 33386 | `alerted` | `contextual_gambling_phrase_detected` | `non-gambling` | 0.23135074939286437 |
| `zyvora.id` | 25992 | `alerted` | `contextual_gambling_phrase_detected` | `non-gambling` | 0.18255034760508648 |

Both were sent to BIMA with `alert_confidence=0.6`. Their current public homepages discuss rejecting gambling promotions, but the exact historical HTML may differ. Review both cases in BIMA/IDADX; do not delete history or assume this code change retracts reports.

## Scope of patch

- Add a narrow helper in the **existing authoritative classifier `crawler_url.py`**, independent of hostname. Only the `contextual_gambling_phrase_detected` medium-risk branch can be demoted to `low_risk` with reason `anti_gambling_rejection_context`.
- Requires **ML prediction exactly `non-gambling`** plus a local explicit negation/refusal referring to gambling. Existing IndoBERT/Classic ML inference and confidence thresholds remain unchanged.
- Veto the exemption on specific gambling marketing language, action-oriented gambling offers, or detected hidden gambling links. If hidden-link detection fails, exemption fails closed. Because pages can change or cloak, this cannot guarantee every future case is safe.
- Does **not** reclassify previously alerted jobs, change `jobs`, resend or retract alerts, update BIMA/IDADX, touch API keys, reset pending alerts, or edit Discovery/Registry priority.
- No changes to Google/Brave discovery, IndoBERT, daemon or cron. Brave remains disabled pending API authorization.

## Apply only on Mac → GitHub → production

1. On Mac, check repository status/commit. Download patch `bima-v4.3.9-to-v4.4.0-anti-judol-context.patch` to `~/Downloads/`.
2. Verify and apply:

```sh
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git apply --check "$HOME/Downloads/bima-v4.3.9-to-v4.4.0-anti-judol-context.patch"
git apply "$HOME/Downloads/bima-v4.3.9-to-v4.4.0-anti-judol-context.patch"
python3 -m py_compile crawler_url.py
PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -q
git diff --check
git add -- crawler_url.py multi/tests/test_anti_gambling_rejection_v440.py README_ANTI_JUDOL_CONTEXT_V440.md
git diff --cached --check
git commit -m 'fix: distinguish anti-gambling rejection from gambling promotion'
git push origin main
```

3. On production server, retain ` M crawler_google.py` and `?? domains.json`. `git pull --ff-only` and test, but **do not automatically restart**. Evaluate the rollback plan and window before restarting the live crawler, which can send real alerts.

```sh
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover -s multi/tests -q
```

For production verification, read historical job records with SQLite `mode=ro`; do NOT requeue `alerted` or `pending_alert` just to test this change. Use synthetic unit tests; if live scanning is needed, use an isolated **test DB** and `--dry-run`, never duplicate BIMA submissions.

## Operational incident response

1. Record jobs `33386` and `25992`, classification reasons, confidence, timestamp and available screenshots/alerts.
2. Have the IDADX/BIMA reviewer mark the cases as false positives **if confirmed after examining the evidence**; the script does not edit external records.
3. Monitor for genuine gambling promo on compromised pages, including hidden links, redirects, and cloaked pages.
4. Evaluate a broader false-positive corpus (safe policies versus true-positive operator sites) before enabling new thresholds globally.

## Validation limits

The patch uses the exact source anchors of `crawler_url.py` from the public GitHub commit `9e3ee83`, inspected remotely. Local container did not have a full copy of `crawler_url.py`; a synthetic source fixture was used to verify patch application, AST isolation and regression tests. **The Mac `git apply --check`, `py_compile`, full tests, and server full tests are authoritative** before any production restart. No network requests are made by these regression tests.
