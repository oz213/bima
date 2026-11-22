# BIMA Continuous Learning v2 + Registry Snapshot Lifecycle

Patch basis: BIMA `7f240b5` (compatible with an additional DNS Target Safety patch if
all original context hunks remain unchanged). This release is a **candidate/offline
learning** pipeline and **explicit Registry snapshot cutover**, not automatic
IndoBERT fine-tuning.

## Why not `rm crawler_production.sqlite3`?

Production SQLite stores more than crawling status: Telegram requests,
Pre-Alert decisions, ambiguous BIMA sends, source job IDs, discovery metadata,
and live incident review state. Clearing or replacing the whole database would
lose important evidence and can accidentally duplicate abuse alerts.

The new design **never deletes jobs**. A Registry cycle has an independent
active queue and ordered URL membership, so the next cycle does **not** wait
for the preceding ~1.5 million URLs. The previous cycle's unfinished Registry
queue and its retired network retries are excluded from routine dispatch
without deleting the old rows. Discovery and Telegram retries stay eligible. Existing
Terminal/Alert/Telegram/Pre-Alert jobs remain protected.

## Components

- `multi/network_retry_queue.py`: restrict bounded automatic retries to the
  active snapshot plus independent Discovery and Telegram cases; old Registry
  retry backlog cannot consume the new snapshot's retry quota.
- `multi/registry_snapshot_v2.py`: cycle metadata, ordered active queue,
  transactional status transitions and an immutable copy of prior ordinary
  verdicts before safe recrawls. The pending queue uses the **partial index**
  `idx_registry_snapshot_pending_v2`; the original pending-cursor fallback
  stays active until the first v2 cycle is explicitly applied.
- `tools/bima_registry_snapshot_v2.py`: preview, explicit cutover, status,
  staged atomic `urls.json` replacement. `apply` **refuses** if either service
  is active or if free disk space near the DB is below 1 GiB.
- `multi/learning/continuous_v2.py`: stable observation UID = source identity,
  event/cycle and evidence hash. Explicit site labels and **independent text
  labels**. Uses the existing *separate* `bima_learning_v450.sqlite3`.
- `tools/bima_continuous_learning_v2.py`: sync site-only Pre-Alert feedback,
  add verified evidence, independently label the text, review a new cycle and
  train/evaluate an offline hashed-word Naive Bayes candidate.
- No changes to production IndoBERT, `gambling_classifier.pkl`, BIMA API,
  ambiguous-alert replay safeguards, `bima.json` or Telegram commands.
  Network-retry timing and pacing limits stay unchanged; only eligible scope
  is filtered to avoid dispatching retired Registry cycles.

## One-time install: Mac -> GitHub -> Linux

From the **real BIMA checkout**, run:

```bash
PATCH="$HOME/Downloads/bima-continuous-learning-v2.patch"
git status --short
git pull --ff-only origin main
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover -s multi/tests -p 'test_registry_snapshot_v2.py' -v
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover -s multi/tests -p 'test_continuous_learning_v2.py' -v
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover -s multi/tests -p 'test_network_retry_queue.py' -v
```

Commit **only** the following ten paths:

```bash
git add -- multi/registry_snapshot_v2.py multi/discovery_priority.py multi/network_retry_queue.py multi/crawler_multi.py multi/learning/continuous_v2.py tools/bima_registry_snapshot_v2.py tools/bima_continuous_learning_v2.py multi/tests/test_registry_snapshot_v2.py multi/tests/test_continuous_learning_v2.py README_BIMA_CONTINUOUS_LEARNING_V2.md
git diff --cached --check
git diff --cached --name-status
git commit -m "feat: registry snapshot cycles and durable continuous learning v2"
git push origin main
```


Then on Linux:

```bash
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover -s "$APP/multi/tests" -p 'test_registry_snapshot_v2.py' -v
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover -s "$APP/multi/tests" -p 'test_continuous_learning_v2.py' -v
```

Do not restart just to import the patch. Existing crawler continues its existing
queue until the next **explicit** snapshot cutover.

## On each `urls.json` replacement: no full crawl required

1. Ensure the `ExecStart` in `systemctl cat pandi-crawler-multi.service` uses
   the real `--input`/`--db` paths. If they differ, replace the example paths.
2. Generate the fresh PDNS JSON using your existing MySQL pipeline, but redirect
   it to **`$APP/urls.json.next`**, *never* overwrite the active `urls.json`.
   Keep database credentials private, and let `mysql -p` prompt interactively.
3. Preview and verify a plausible count/hash (default minimum 100,000 URLs):

```bash
APP=/home/fauzi/crawler-rebuild
DB="$APP/multi/state/crawler_production.sqlite3"
CYCLE="registry-$(date -u +%Y%m%dT%H%M%SZ)"
TOOL="$APP/tools/bima_registry_snapshot_v2.py"

sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" preview \
  --db "$DB" --input "$APP/urls.json" \
  --source "$APP/urls.json.next" --cycle "$CYCLE"
```

4. **Only after preview succeeds**, stop BOTH live services; do not delete any
   SQLite files or use `rm`, `sqlite3 .recover`, or `git reset`:

```bash
sudo systemctl stop pandi-crawler-multi.service pandi-telegram-monitor.service
systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
```

Expected: `inactive` and `inactive`. Ensure no separate discovery/import job is
writing production SQLite at the same time.

5. Apply while both services are **stopped**:

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" apply \
  --db "$DB" --input "$APP/urls.json" \
  --source "$APP/urls.json.next" --cycle "$CYCLE"
```

The tool first commits the new Registry queue in an **input_ready=false**
state, then atomically publishes `urls.json` and enables the queue. If a file
swap fails, Registry routine dispatch stays disabled (fail closed). If publishing fails, **do not restart**; rerun the identical
`apply` command with the same `CYCLE` and staged file to finalize safely.
A `CYCLE` ID cannot be reused with different content. A shrink of >50% against
the previous snapshot is rejected by default, with an explicit override
`--allow-major-shrink` only after reviewing PDNS extraction.

6. Inspect the active cycle and restart services:

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" status --db "$DB"
sudo systemctl start pandi-crawler-multi.service pandi-telegram-monitor.service
systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
PID=$(systemctl show -p MainPID --value pandi-crawler-multi.service)
journalctl --utc "_PID=$PID" --no-pager -n 80 | grep -E 'IndoBERT loaded ONCE|HTTP attempted=|ERROR|Traceback'
```

Verify `input_ready: true` in the `status` output **before restarting**.
The crawler will import the new file on startup. `new=0` may be **expected**,
because the snapshot tool already inserted its URLs. The active Registry queue
contains all eligible URLs of the latest cycle, ordered by the snapshot file.
The old unfinished Registry queue is **not** an eligibility requirement.

## What gets recrawled or preserved?

**Eligible for a fresh Registry cycle:** `complete`, `inconclusive`,
`retry_http`, `failed`, interrupted HTTP/browser jobs, and already pending
Registry jobs, excluding any protected domain. Historical verdicts for reset
jobs are copied into `registry_snapshot_job_history_v2`.

**Never automatically reset or resend:** existing `alerted`, `pending_alert`,
Pre-Alert, Telegram-reviewed jobs, `send_unknown`, and **any hostname already
associated with an alert or Telegram/Pre-Alert**. This hostname-level protection
also prevents cross-scheme duplicates (`http` versus `https`). Such sites can
still be investigated explicitly by an authorized analyst; automatic Registry
replay is deliberately suppressed. This is a conservative safety tradeoff.

**Discovery** and interactive **Telegram `/cek`** retain their priority paths.
Bounded `retry_http` dispatch excludes retired Registry jobs that are not in
the active snapshot; Discovery and Telegram retries remain eligible.
No pre-existing job ID is recycled. Old Registry snapshots remain only as
summary metadata and historical verdict rows, not 1.5 million copies of all
crawl jobs for every cycle.

## Continuous Learning v2

The learning DB remains separate from crawler production SQLite. Sync human
**site** decisions from Selective Pre-Alert at any point, even with services
running; this is read-only to production DB:

```bash
APP=/home/fauzi/crawler-rebuild
DB="$APP/multi/state/crawler_production.sqlite3"
LEARN="$APP/multi/state/bima_learning_v450.sqlite3"
LEARN_TOOL="$APP/tools/bima_continuous_learning_v2.py"
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" sync-prealerts --production-db "$DB"
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" stats --limit 20
```

Sync does **not** guess a training text label from an alert. Review each
site-evidence pair, verify the actual page text (40..120000 chars), save the
approved excerpt to a private UTF-8 file, then explicitly label the text:

```bash
# Replace UID and the text filename with an actual case from `stats`.
UID='<source_uid_from_stats>'
EVIDENCE='/home/fauzi/private-evidence-approved.txt'
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" attach-text --uid "$UID" --file "$EVIDENCE" --reviewer 'analyst'
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" label-text --uid "$UID" --label gambling \
  --reviewer 'analyst' --note 'Verified text evidence promotes gambling'
```

To add a **new review of a previously seen domain** in a later Registry cycle,
use `add-reviewed` with the current cycle ID, existing job ID, and approved text.
The unique observation ID contains the cycle and evidence digest. This avoids
the old `review_cases.source_job_id` uniqueness limitation when collecting
multiple fresh observations across cycles.

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" add-reviewed \
  --production-db "$DB" --cycle "$CYCLE" --job 12345 \
  --site true_positive --text-label gambling \
  --file "$EVIDENCE" --reviewer 'analyst' --note 'Verified new snapshot content'
```

Only run offline model learning after there are enough **verified unique
texts**: at least 100 total, 25 of each class, 20 domain groups, with both
classes in train/validation/test. The candidate is **hashed-word Naive Bayes**,
not a fine-tuned IndoBERT. Dataset includes v1 verified evidence and v2
explicit human reviews, with duplicate text removed and grouped splits.

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$LEARN_TOOL" \
  --learning-db "$LEARN" learn --runs "$APP/multi/state/learning-runs-v2"
```

Possible results: `waiting_for_labels`, `candidate_ready`, or `up_to_date`.
**Nothing is automatically loaded into production**. IndoBERT remains mandatory
with its current tuned model. Validate candidate precision/recall and selection
bias with independently reviewed domain samples before any future integration
or IndoBERT fine-tuning; never promote solely based on 100 examples.

## Scope, limitations and operational safety

- This **does not rotate or empty** `crawler_production.sqlite3`; preserving it
  is essential for unresolved Pre-Alert and Telegram delivery state.
- Registry snapshot application deliberately **writes** new cycle metadata,
  active queue rows, and safe status resets; it is not read-only and requires
  both services stopped. Preview and model candidate learning are non-disruptive
  to the crawler, except expected disk/CPU cost.
- A new snapshot reuses stable `jobs.id` values. V1 reviews remain immutable;
  v2 observations have separate stable unique keys for newer cycles.
- Manual text-labeling is not performed automatically from Telegram screenshots
  or website predictions. An analyst must verify the text, ideally from the
  same time as the original decision.
- Dataset grouping defaults to hostname. Multiple subdomains of one registered
  site might need one shared domain-group in a future corpus audit to avoid
  train/test leakage.
- The full code is provided as an incremental patch. The offline test suite
  uses synthetic DB fixtures; **run the tests on the actual Mac and Linux repo
  before first cutover**. No live PDNS connection or production model activation
  was used in testing.
