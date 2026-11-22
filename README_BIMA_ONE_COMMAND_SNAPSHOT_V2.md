# BIMA Registry Snapshot v2: One-Command Cutover

This operator tool wraps the **existing** `tools/bima_registry_snapshot_v2.py` and
only needs an already-generated `urls.json.next`. It does not perform PDNS queries,
modify IndoBERT, replay alerts, delete jobs, or clear production SQLite.

## Install once (Mac > GitHub > server)

Apply the incremental patch to the repository at `a5d3a81` or its successor:

```sh
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
PATCH="$HOME/Downloads/bima-registry-one-command-v2.patch"
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_registry_rollover_v2.py' -v
git add -- tools/bima_registry_rollover_v2.py \
  multi/tests/test_registry_rollover_v2.py README_BIMA_ONE_COMMAND_SNAPSHOT_V2.md
git diff --cached --check
git commit -m "ops: add one-command safeguarded registry snapshot rollover"
git push origin main
```

Then on Linux (does not require stopping crawler to pull a **new script**):

```sh
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_registry_rollover_v2.py' -v
```

Do not discard local `crawler_google.py` or `domains.json` changes.

## Normal operation — single command

Prerequisite: `/home/fauzi/crawler-rebuild/urls.json.next` is a **fully written,
closed**, readable PDNS export (use a temporary filename and rename it to
`urls.json.next` only after your MySQL command succeeds). The active service
must use the default `--input` and `--db` paths (the tool verifies ExecStart).

```sh
sudo python3 /home/fauzi/crawler-rebuild/tools/bima_registry_rollover_v2.py
```

The operator tool will:

1. Obtain a system-wide lock to prevent two concurrent cutovers.
2. Check that the source file is valid and stable; call the official `preview`
   using BIMA's existing URL normalizer; ensure >=100,000 valid distinct URLs.
3. Validate the current active cycle and decline a >50% shrink without an
   explicit override. On the first cycle, compare with the existing active
   `urls.json` as the baseline. Verify the production service's configured input/DB paths
   and available disk before stopping anything.
4. Stop `pandi-crawler-multi.service` and `pandi-telegram-monitor.service`.
5. Create SQLite-native, `quick_check`-verified backups of production and
   learning DBs, and a copy of the previous active `urls.json`. Backups are
   private (0700 directory, 0600 files) under `/home/fauzi/bima-backups/<cycle>`.
6. Run the existing snapshot `apply`, including its atomic file publish; check
   `active_cycle`, `ready`, and input SHA-256 before starting anything.
7. Start both services, verify both are active and that the new crawler PID logs
   `IndoBERT loaded ONCE` within 180 seconds.
8. Move the consumed `urls.json.next` to the backup directory so an accidental
   repeated command cannot immediately requeue the same snapshot.

No new DB deletion, no reset of human decisions or unknown-delivery alerts,
and no automatic model training.

## Optional flags

```sh
# Intentionally re-run even if PDNS generated identical content:
sudo python3 /home/fauzi/crawler-rebuild/tools/bima_registry_rollover_v2.py --repeat-source

# Explicitly accept a drop of >50% relative to the prior snapshot (investigate first):
sudo python3 /home/fauzi/crawler-rebuild/tools/bima_registry_rollover_v2.py --allow-major-shrink

# Non-standard crawler systemd unit. Verify --input/--db yourself FIRST:
sudo python3 /home/fauzi/crawler-rebuild/tools/bima_registry_rollover_v2.py --skip-unit-check

# More time for a slow IndoBERT cold start:
sudo python3 /home/fauzi/crawler-rebuild/tools/bima_registry_rollover_v2.py --health-timeout 300
```

The `--skip-unit-check` option never disables schema/JSON validation, service
stop checks, SQLite backup, atomic `apply`, ready/hash verification or post-start
health checks. It ONLY bypasses the verification of `ExecStart` flags.

## Failure and recovery

- Bad JSON, unexpected shrink, service configuration mismatch, insufficient disk,
  or source hash changes during preflight: **services are not stopped**.
- Backup failure or an `apply` that rolled back without modifying the active
  cycle: restart previously active services automatically.
- Failure **after the new cycle has been committed** but **before ready=true**:
  leave services stopped. Keep **the same** `urls.json.next` and rerun the same
  one-command tool; it reuses the incomplete cycle ID and finishes publication.
- Post-start health check failure: never roll back or delete the already-applied
  snapshot. Inspect `systemctl status` / `journalctl`; both services may already
  be running, but the tool returns an error to avoid a false success report.
- If an interrupted backup directory exists but is incomplete, the tool refuses
  to overwrite it. Verify/repair the backup manually; do not blindly delete it.
- To intentionally repeat a snapshot with identical bytes use `--repeat-source`.

**Important:** this wrapper was tested with synthetic/mock systemd and SQLite
fixtures, not on the live production 1.5-million-URL database. First cutover
should run during an operationally suitable window, with enough disk capacity
and no concurrent Discovery/other SQLite writers.
