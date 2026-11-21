# BIMA v4.5.0 — Self-Learning Foundation (pilot, review-first)

**Status:** additive, offline, opt-in pilot. No changes to the v4.4.1 classifier,
IndoBERT checkpoint, `multi/crawler_multi.py`, Selenium, Telegram, or BIMA alert sender.
No AI model is automatically retrained or promoted by this release.

## Purpose

- Read recent **alerted** jobs from the existing production SQLite in **read-only mode**.
- Select human review candidates from two *specific* patterns:
  1. `model_rule_disagreement`: final `medium_risk` / `high_risk`,
     final `contextual_gambling_phrase_detected`, and both Classic ML and
     IndoBERT predict `non-gambling` (BERT must actually have been used).
  2. `http_browser_escalation`: HTTP result was `low_risk` / `inconclusive`,
     but Selenium classified as `medium_risk` / `high_risk`.
- Store review cases in an **independent** SQLite DB, never in the crawler DB.
- Require analyst-supplied evidence, an explicit **incident verdict**, and a
  separate **text-specific label** before exporting a training/evaluation sample.
  A real gambling redirect with normal-looking page text can be a website-level
  true positive but a text-model non-gambling sample; never conflate them.
- Export labeled UTF-8 text as reproducible JSONL splits, grouped by
  `domain_group` (hostname by default; analyst should explicitly supply the
  registered domain for subdomains).

**Important:** A review *candidate* is not a confirmed false positive. An IDADX
status or a `BIMA alert sent` status is not accepted as ground truth.
False-positive correction is supervised, not self-confirming.

## Operating constraints

- Python 3.9+; Python standard library only.
- Run from the BIMA repository root with `PYTHONPATH="$PWD/multi:$PWD"`.
- Source database is opened using `file:...?mode=ro` plus
  `PRAGMA query_only=ON`. An indexed `status='alerted'` query is limited to
  5,000 recent alerts by default; maximum 50,000.
- Learning SQLite defaults to `multi/state/bima_learning_v450.sqlite3`
  (under the existing Git-ignored `multi/state/`). Its WAL/SHM files are
  separate from `crawler_production.sqlite3`.
- **No cron, systemd services, or API endpoints** are installed. No automatic
  scanning or alert transmission. The CLI makes no HTTP requests.
- Neither model weights nor BIMA tokens / configuration / screenshots are read
  or written by this module. Do not commit learning DB, evidence text, datasets,
  or reviewer notes to Git.
- `attach-text` requires an explicitly supplied, authorized, locally stored
  UTF-8 text sample. It never scrapes a URL or fetches an IDADX record.
- No personal data or credentials should be included in evidence files.

## Check/install on Mac (before production)

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -p 'test_learning_v450.py' -v
PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -q
```

## Opt-in read-only pilot on server

Do not run the commands below during the first deploy until full tests pass
and a separate review of the new code has completed. `seed` creates/writes ONLY
its separate learning SQLite database; the production DB remains read-only.

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m learning.cli seed \
  --production-db multi/state/crawler_production.sqlite3 --limit 5000
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m learning.cli stats
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m learning.cli list --limit 20
```

Repeated `seed` is idempotent. A new snapshot of the same source job does **not**
overwrite an existing review, evidence, or label.

## Human review workflow

First verify the case using authorized BIMA/IDADX evidence, browser screenshot,
redirect chain, and other independent evidence. Do not treat model disagreements
as proof that BIMA was wrong. **Always inspect `list` first and select the exact
case ID and domain you have actually verified; never blindly label case 1.**
Then supply authorized text evidence manually:

```bash
# EXAMPLE ONLY: replace with an actual case ID from `list`, not a source job ID.
CASE_ID=123
# Create a UTF-8 file (outside Git) from independently VERIFIED evidence.
# For illustration only, use its real path, NOT a token-bearing URL.
PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B \
  -m learning.cli attach-text --case "$CASE_ID" \
  --file /secure/location/approved-case-text.txt --origin manual_capture

PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B \
  -m learning.cli review --case "$CASE_ID" --label false_positive \
  --text-label non_gambling --reviewer 'analyst1' \
  --note 'Verified business anti-gambling policy; no active gambling evidence'
# If multiple related subdomains exist, explicitly use their common .id
# registered domain as --domain-group in the review command above.
```

Use `--label needs_review` when evidence is ambiguous. Final `true_positive` and
`false_positive` incident verdicts **require attached evidence** and an explicit
`--text-label gambling|non_gambling|not_for_training`, and are immutable in this
pilot, and are written to an append-only review events table for audit. The CLI
reviewer string is attribution, **not** an authenticated identity mechanism:
restrict learning DB access at the filesystem level.

A domain can change between observations. Evidence must correspond to the
reviewed snapshot. Do not attach today's unrelated page as proof of an old alert.
In a full system, provenance should include capture timestamps, immutable
references, and reviewer identity controls.

## Export a candidate evaluation/training dataset

Only execute once there are enough independently verified text examples:

```bash
PYTHONPATH="$PWD/multi:$PWD" ./venv/bin/python3 -B \
  -m learning.cli export --output multi/state/exports/pilot-20261121 \
  --min-total 100 --min-per-class 25 --min-groups 20
```

Outputs `train.jsonl`, `validation.jsonl`, `test.jsonl`, and `manifest.json`.
Text labels are `1=confirmed gambling text` and `0=confirmed non-gambling text`.
Website-level incident verdicts do **not** automatically determine text labels:
`--text-label not_for_training` excludes cases where evidence is only a redirect,
hidden URL, or other non-text signal. This export **does not train, change or
 deploy IndoBERT**. It does not export unlabeled cases.
A cryptographic seed deterministically assigns complete `domain_group` groups to
80%/10%/10% train/validation/test. It fails rather than producing an empty split.

**Leakage caution:** default grouping is by hostname, not by the Public Suffix
List registrable domain. If several subdomains belong to the same delegated .id
name, **reviewers must set the same `--domain-group` on every related case**.
Repeated identical texts in different groups must be reconciled prior to
training. This initial exporter is designed for a pilot, not an unrestricted
self-training system. Split percentages are targets, not guaranteed proportions
for small samples.

## Next phase (NOT implemented here)

- Capture immutable evidence references with original observation time.
- Separate identity/authentication and two-person adjudication for sensitive cases.
- Evaluate model against a held-out, domain-separated gold set; track precision,
  recall, F1, false-positive rate, and changes across crawl cohorts.
- Train candidate IndoBERT on a **separate** resource, use shadow evaluation,
  canary deployment, approval gates, and rollback.
- Never alter thresholds, blacklist, whitelist, or alert actions by autonomous
  learning from BIMA's own unverified alerts.

## Siklus belajar otomatis v4.5.1

Dari root repo, jalankan dengan akun service:

```sh
PYTHONPATH=multi ./venv/bin/python3 -B -m learning.cli learn --production-db multi/state/crawler_production.sqlite3
```

Gunakan path database produksi yang dipakai crawler saat ini. Perintah mengambil kandidat
review terbaru (maksimum 5.000 alert), kemudian membaca label teks final dari database
learning. Analis memakai alur attach-text dan review di atas. Gunakan domain terdaftar
yang sama sebagai domain-group untuk subdomain terkait.

Status waiting_for_labels berarti jumlah label atau cakupan kelas belum cukup.
Syarat awal mengikuti ekspor v4.5.0: 100 kasus, 25 per kelas, 20 kelompok domain,
serta kedua kelas terwakili di setiap split. Kode melatih kandidat multinomial
Naive Bayes dengan token kata yang di-hash pada bagian train. Hasil validation
dan test memuat confusion matrix, precision, recall dan F1 untuk kelas gambling.
Skor ini mengukur sampel review yang terpilih; evaluasi pada domain representatif
diperlukan untuk mengukur performa operasional.

candidate_ready memuat lokasi model.json dan report.json di multi/state/learning-runs/.
Dataset identik menghasilkan up_to_date. Dataset yang berubah menghasilkan direktori
kandidat baru. File model dapat dibaca sebagai JSON dan dipakai secara offline dengan
learning.candidate.predict(model, text). Crawler produksi tetap menjalankan IndoBERT.

Untuk menjalankan siklus harian pada server setelah tes sukses:

```sh
sudo install -m 0644 deploy/pandi-learning.service deploy/pandi-learning.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Jika database crawler berada di NFS atau path nondefault, jalankan
`sudo systemctl edit pandi-learning.service` dan isi bagian [Service] dengan
`Environment=BIMA_LEARNING_SOURCE=/path/aktual/crawler_production.sqlite3`.
Database learning dan model kandidat menggunakan direktori state lokal secara default.
Pastikan direktori state untuk learning berada pada filesystem lokal bila state crawler
merupakan symlink ke NFS. Opsi CLI --learning-db (sebelum learn) dan --runs (setelah learn)
dapat diarahkan ke penyimpanan lokal dengan override ExecStart.

```sh
sudo systemctl start pandi-learning.service &&
sudo systemctl enable --now pandi-learning.timer
sudo journalctl -u pandi-learning.service -n 30 --no-pager
```
