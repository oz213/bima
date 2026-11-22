# BIMA Continuous Learning v2.1 - Review Workbench and Dataset Readiness

## Scope

Operator-only **read-only** reporting and human-review prioritization. It extends
Continuous Learning v2 without changing the crawler, `urls.json`, snapshot
rollover, SQLite schemas, Pre-Alert delivery, Telegram commands, IndoBERT,
classic ML model, or backup/retention policy.

The learning database remains separate from `crawler_production.sqlite3`.
**No automatic label generation, model training, alert send, data deletion, or
background work.** Even `complete` and `inconclusive` are NOT true negative
labels. Both site and text labels require analyst confirmation.

## New components

- `multi/learning/review_workbench_v21.py` - read-only learning snapshot,
  class deficit analysis matching v2 `learn()` gates, prioritized metadata-only
  review queue, and efficient deterministic candidate sampling from production.
- `tools/bima_learning_workbench_v21.py` - CLI with `status`, `queue`,
  `sample-production` and optional private metadata-only JSON output (0600,
  new file only, no overwrite).
- `multi/tests/test_learning_workbench_v21.py` - synthetic database safety,
  queue order, missing evidence, data integrity, duplicates, group split gates,
  file permissions, and read-only tests.

## Deploy (after Continuous Learning v2 is installed)

Apply on Mac from the actual BIMA repository:

```sh
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git pull --ff-only origin main
PATCH="$HOME/Downloads/bima-learning-review-workbench-v21.patch"
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_learning_workbench_v21.py' -v
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_continuous_learning_v2.py' -v
```

Commit only these four files:

```sh
git add -- multi/learning/review_workbench_v21.py \
  tools/bima_learning_workbench_v21.py \
  multi/tests/test_learning_workbench_v21.py \
  README_BIMA_CONTINUOUS_LEARNING_V21.md
git diff --cached --check
git commit -m "feat: read-only human review workbench for continuous learning"
git push origin main
```

On Ubuntu:

```sh
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_learning_workbench_v21.py' -v
```

**No service restart or snapshot cutover needed** for this read-only tool.
Keep server-local `crawler_google.py`, `domains.json`, private configuration
and existing snapshots untouched.

## Daily operational usage

```sh
APP=/home/fauzi/crawler-rebuild
LEARN="$APP/multi/state/bima_learning_v450.sqlite3"
PROD="$APP/multi/state/crawler_production.sqlite3"
TOOL="$APP/tools/bima_learning_workbench_v21.py"

# All operations below are read-only, no evidence text printed.
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" --learning-db "$LEARN" status
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" --learning-db "$LEARN" \
  queue --focus needs-text-label --limit 25
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" --learning-db "$LEARN" \
  queue --focus needs-evidence --limit 25

# Expand the evaluation pool with human-verified candidate negatives/uncertain.
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" \
  sample-production --production-db "$PROD" --per-status 10
```

The sampler uses indexed deterministic ID-spread candidates from `complete`
and `inconclusive` jobs; it is **not statistically representative sampling**,
not a measure of false-negative rate, and never labels them as negative.
Reviewers must inspect approved evidence before entering site/text decisions.

For a private metadata-only report:

```sh
OUT="$APP/multi/state/learning-review-$(date -u +%Y%m%dT%H%M%SZ).json"
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" \
  --learning-db "$LEARN" --output "$OUT" queue --limit 50
```

The destination must not exist. Output file has permission `0600`. The report
contains case IDs, hostname, label status and evidence character counts, never
text excerpts, URL paths, screenshots or configuration secrets. Keep it private.

## Completing human labels

For v1 records, reviewers continue to use Telegram `/review` and `/bukti`
(or `python3 -m learning.cli` for approved local evidence). For a v2 Pre-Alert
site decision which has no approved text, capture the actual page excerpt to a
private text file **and verify it**. Then use the existing v2 commands:

```sh
LEARN_TOOL="$APP/tools/bima_continuous_learning_v2.py"
UID='<source_uid from queue>'
EVIDENCE='/home/fauzi/private-evidence-approved.txt'

sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" attach-text --uid "$UID" --file "$EVIDENCE" \
  --reviewer 'analyst'
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" label-text --uid "$UID" --label gambling \
  --reviewer 'analyst' --note 'Verified this excerpt promotes gambling'
```

`gambling` above is only an **example**; do not reuse it without checking the
specific evidence. Choose `non_gambling` or `not_for_training` when warranted.
In particular a gambling-related news article or report is not necessarily
promoting gambling.

## When training becomes possible

`status` counts unique **verified** texts (v1+v2) after SHA-256 validation and
deduplication, with the same gates as the existing v2 `learn()` function:
100 texts, 25 per class, 20 domain groups, and both classes present in each
train/validation/test split. Splitting uses the same deterministic domain-group
hash as `continuous_v2.learn`. A split-imbalance/integrity issue blocks
eligibility even when numeric minimums are met.

Once `status.readiness.status` is `candidate_eligible`, the existing *offline*
training command is available:

```sh
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" learn \
  --runs "$APP/multi/state/learning-runs-v2"
```

This trains **hashed-word Naive Bayes candidate**, NOT IndoBERT. It **does not
install** any model or change classification thresholds. External hold-out and
false-negative analysis are still required before production use or fine-tuning.

## Compatibility and known limitations

- Only new files: the prior one-command snapshot rollover and **full backup
  behavior are unchanged**, regardless of whether that optional patch is deployed.
- Learning DB must already exist and have been initialized by Continuous
  Learning v2; status/queue will not create or migrate it.
- Work queue scans a bounded 5,000 oldest/unreviewed v2 and 5,000 newest v1
  records at a time; `total_matching_loaded` reflects that window, not an exact
  count if corpus is larger. Larger deployments should add paginated queries.
- No raw evidence or full URL is shown in generated worklists. Do not send
  private metadata reports to a public chat or repository.
- The systematic candidate sampler distributes around job IDs and does not
  establish the false-positive or false-negative rate of the entire .id zone.
- Tests use temporary/synthetic databases only. Run tests against the *real*
  Mac checkout and server Python environment before relying on the workbench.
