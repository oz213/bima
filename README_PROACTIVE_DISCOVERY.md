# BIMA Proactive .ID Discovery & Redirect-Aware Re-Crawling — v4

**Status:** paket source untuk diterapkan di Mac dan dipush ke Git. **Belum terpasang atau diaktifkan di server.** Pengujian offline tidak menggantikan pengujian di staging dengan egress policy yang benar.

**Paket upgrade kompatibel v2 ke v4 (incremental):** Gunakan `bima-proactive-v2-to-v4-compatible.patch` bila repository sudah memiliki v2 di branch `main`. Jangan terapkan patch kumulatif v4/v3 atau upgrade lama bersamaan. Patch ini tidak melakukan text-hunk replace langsung terhadap `multi/crawler_multi.py`: jalankan `tools/upgrade_bima_queue.py` setelah patch agar perubahan antrean disesuaikan dengan struktur kode lokal dengan pemeriksaan AST dan validasi sintaks.

## 1. Keputusan arsitektur

- `urls.json` **sudah** memuat domain Registry PANDI yang memiliki nameserver (NS), dan **diperbarui manual**. File tersebut tetap menjadi baseline; patch ini **tidak mengubah file, tidak membuat sumber registry kedua**, dan tidak melakukan sinkronisasi Registry otomatis.
- Seluruh penemuan/antrean baru ditujukan ke `.id`, termasuk IDN (punycode) dan subdomain di bawah baseline seed.
- Crawler multi-worker dan model **IndoBERT** tetap melakukan klasifikasi. Discovery dan scheduler **tidak mengklasifikasikan konten sendiri** serta tidak pernah mengirim alert ke BIMA.
- Redirect dari URL `.id` menuju `.com`/TLD lain dicatat sebagai bukti **tanpa menjadikan domain luar `.id` sebagai target baru**. Halaman tujuan eksternal **tidak diakses oleh Discovery Engine**.
- **Penting:** `crawler_multi.py` utama sudah mengikuti beberapa redirect eksternal untuk klasifikasi. Pengamanan browser/egress/SSRF pada crawler utama harus diaudit di staging sebelum auto-enqueue atau recrawl produksi diaktifkan; kebijakan network/sandbox merupakan lapisan kontrol terpisah yang tidak ditambahkan oleh patch ini.
- Informasi redirect **bukan vonis judi online**; JS literal tidak membuktikan browser benar-benar mengeksekusinya.

### Diagram alur

```text
urls.json (Registry delegated .id; manual update)
  ├─> proactive_discovery.py (homepage/robots/sitemap + optional CT)
  │     ├─> optional small probe of discovered same-host pages
  │     ├─> HTTP/Meta/JS redirect observations (destination redacted)
  │     └─> default PREVIEW / optional --apply -> SQLite jobs(pending)
  │
  └─> recrawl_scheduler.py (SQLite metadata only; no network)
        ├─> completed low_risk >7 days old, OR
        ├─> new external-redirect signal after latest clean crawl, >24h old
        └─> default PREVIEW / optional --apply --allow-live-rechecks
             -> guarded status complete -> pending, audit logged
                                       |
                                       v
                            crawler_multi.py
                       HTTP + IndoBERT + browser
                               BIMA / Telegram
```

## 2. Paket v4

```text
README_PROACTIVE_DISCOVERY.md
multi/proactive_discovery.py                     # v3 + bounded URL probe
multi/recrawl_scheduler.py                       # new read-only-by-default recrawl
multi/routine_queue.py                           # handle old pending IDs after cursor
tools/upgrade_bima_queue.py                     # adaptive, explicit queue fix (check/apply)
multi/crawler_multi.py                           # patched separately by tool, NOT by Git patch
multi/tests/test_proactive_discovery.py
multi/tests/test_recrawl_scheduler.py
multi/tests/test_routine_queue.py
deploy/pandi-proactive-discovery.service          # preview (never --apply)
deploy/pandi-proactive-discovery.timer
deploy/90-enable-proactive-enqueue.conf           # optional; DON'T INSTALL

deploy/pandi-bima-recrawl.service                # preview (never --apply)
deploy/pandi-bima-recrawl.timer
deploy/90-enable-bima-recrawl.conf                # optional; DON'T INSTALL
```

## 3. Discovery: what v4 can actually find

- Link URLs exposed in public homepage HTML, sitemap(s), `robots.txt`, and optional passive CT via crt.sh (`--ct` off by default; external provider receives a seed hostname).
- With `--scan-web --probe-candidates 2`, inspect **up to 2 discovered same-host pages per seed** (highest heuristic priority first). This may reveal off-domain 302 redirects in `/promo/` even if the homepage looks benign.
- `robots.txt` requirements are respected for link collection and probes; non-200/non-404 robots response fails closed for web fetch. No brute force or Chrome execution in discovery.
- Capture HTTP 301/302/303/307/308 as observed; capture meta refresh and literal JS location expressions as **static hints** only.
- Pin DNS-validated public IP when fetching `.id` pages. Never follow off-seed redirects from discovery; retain only sanitized destinations without query/fragment.
- SQLite candidate queue uses `jobs(url UNIQUE)` and a separate pending-candidate holding area. Preview does not create/modify tables.

**Not included:** search engine feeds, urlscan integration, passive DNS providers, DNS-change monitoring, JavaScript execution confirmation, automatic Registry sync, or automatic alert adjudication.

## 4. Recrawl behavior

The new `multi/recrawl_scheduler.py` reads `jobs` and only considers URLs:

- That are valid HTTP(S) `.id` and within the host inventory from `urls.json`, with no query/fragment;
- Whose job has `status='complete'` **and** `risk='low_risk'`;
- That were last checked at least **168 hours (7 days)** ago; OR have a **new external redirect observation** since that check and were last checked at least **24 hours** ago.

The scheduler will **not** touch `alerted`, `pending_alert`, `inconclusive`, `browser_queued`, `http_inflight`, `retry_http`, or other statuses. Selection is bounded to 2,000 rows/category per run by default. With explicit live mode, it requeues max 10 old jobs/run (hard CLI maximum 50) and respects an overall `pending` backlog of 100. An audit row stores the previous status, risk, redacted final URL, attempts, timestamp and reason **before** the previous job state is cleared. Updates use `BEGIN IMMEDIATE` plus an exact status/risk/timestamp guard.

**Cursor wrap fix:** in the existing `--telegram-checks --limit 0` mode the routine job cursor had been monotonic. A previously completed job requeued with an older ID would be invisible until restart. The opt-in upgrade tool `tools/upgrade_bima_queue.py --apply` inserts the import/call to `routine_queue.find_next_pending_job()` into `crawler_multi.py` so that, after exhausting higher IDs, the routine loop wraps to lower pending IDs. This change requires a **controlled restart of the crawler service once** after the code is pulled; mere `git pull` cannot reload running Python code.

The scheduler makes no outbound network connections and does not load IndoBERT. Nevertheless, **live recrawl may cause BIMA alerts from the main crawler**, so explicit confirmation is required using *both* `--apply` and `--allow-live-rechecks`.

## 5. Mac: apply compatible patch, adapt crawler, test, commit

Use **only** `bima-proactive-v2-to-v4-compatible.patch` for an existing v2 branch. Git patch intentionally skips the file `multi/crawler_multi.py`, because its layout may differ from a generic v2 snapshot. Instead, the adaptive tool recognizes a known queue assignment in `submit_one()` and safely replaces **only that assignment**. It refuses unknown layouts, never overwrites the whole file, and compiles the prospective Python code before writing.

```bash
cd ~/Projects/bima

git status --short
# Working tree should be clean before proceeding.
git apply --check ~/Downloads/bima-proactive-v2-to-v4-compatible.patch
git apply ~/Downloads/bima-proactive-v2-to-v4-compatible.patch

# Preview the one-file crawler change, then apply explicitly.
python3 -B tools/upgrade_bima_queue.py --check
python3 -B tools/upgrade_bima_queue.py --apply

for suite in test_proactive_discovery.py test_recrawl_scheduler.py test_routine_queue.py test_queue_upgrade_compat.py; do
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
    -s multi/tests -p "$suite" -v || exit 1
done

python3 -B -c "import ast; from pathlib import Path; ast.parse(Path('multi/crawler_multi.py').read_text())"
git diff --check
git status --short

# Stage only source/docs/config templates; NOT urls.json, SQLite or secrets.
git add -- README_PROACTIVE_DISCOVERY.md multi/proactive_discovery.py \
  multi/recrawl_scheduler.py multi/routine_queue.py multi/crawler_multi.py \
  multi/tests/test_proactive_discovery.py multi/tests/test_recrawl_scheduler.py \
  multi/tests/test_routine_queue.py multi/tests/test_queue_upgrade_compat.py \
  tools/upgrade_bima_queue.py deploy/pandi-proactive-discovery.service \
  deploy/pandi-proactive-discovery.timer deploy/90-enable-proactive-enqueue.conf \
  deploy/pandi-bima-recrawl.service deploy/pandi-bima-recrawl.timer \
  deploy/90-enable-bima-recrawl.conf

git diff --cached --check
git diff --cached --stat
git commit -m "feat: upgrade .id discovery and recrawl to v4"
git push origin main
```

**If the upgrade tool reports an unknown queue layout, do not force the change.** Send only the `submit_one()` function and top imports for an exact adapter update. The other files may already have been applied safely, and `git status --short` shows what changed. No production services are started by these steps.

## 6. Server: pull, validate, and preview only

```bash
cd /home/fauzi/crawler-rebuild

git status --short
git branch --show-current
sudo -u fauzi git pull --ff-only origin main

# Verify that the service user can read sources and input
sudo -u fauzi test -r crawler_url.py && echo 'crawler engine readable'
sudo -u fauzi test -r urls.json && echo 'Registry baseline readable'

for suite in test_proactive_discovery.py test_recrawl_scheduler.py test_routine_queue.py; do
  sudo -u fauzi -H ./venv/bin/python3 -B -m unittest discover \
    -s multi/tests -p "$suite" -v || exit 1
done

# Discovery preview (no database write, but performs bounded .id HTTP fetches)
sudo -u fauzi -H ./venv/bin/python3 -B multi/proactive_discovery.py \
  --input urls.json --db multi/state/crawler_production.sqlite3 \
  --scan-web --seed-limit 3 --max-candidates-per-seed 20 \
  --probe-candidates 2 --max-sitemaps 2

# Recrawl preview (metadata-only, NO network and NO database write)
sudo -u fauzi -H ./venv/bin/python3 -B multi/recrawl_scheduler.py \
  --input urls.json --db multi/state/crawler_production.sqlite3 \
  --max-rechecks 10 --max-pending 100
```

Pulling code does not automatically install systemd units or launch new services. **Do not activate optional live overrides until the network/SSRF/alert policy is approved.** A one-time controlled restart of `pandi-crawler-multi.service` is necessary if old-ID cursor wrap behavior is needed with a running crawler; first resolve the earlier `Permission denied` for `crawler_url.py` as user `fauzi`.

To install preview-only timers *after a successful staging review*:

```bash
sudo install -m 0644 deploy/pandi-proactive-discovery.service /etc/systemd/system/
sudo install -m 0644 deploy/pandi-proactive-discovery.timer /etc/systemd/system/
sudo install -m 0644 deploy/pandi-bima-recrawl.service /etc/systemd/system/
sudo install -m 0644 deploy/pandi-bima-recrawl.timer /etc/systemd/system/
sudo systemctl daemon-reload
# Optional, explicitly enabled by the operator (preview-only):
# sudo systemctl enable --now pandi-proactive-discovery.timer
# sudo systemctl enable --now pandi-bima-recrawl.timer
```

**Do not install** `deploy/90-enable-proactive-enqueue.conf` or `deploy/90-enable-bima-recrawl.conf` in production now; these are examples with `--apply`. The main crawler's existing API might generate **real BIMA alerts** after new/requeued jobs are processed. Backup SQLite and consider `--dry-run` on the crawler if testing live recrawl in staging; do not run two crawler instances against the same DB.

## 7. Test results and known limitations

Offline verification: **57/57 unit tests passed** (discovery/redirect, bounded probes, scope/IDN, queue/backpressure, safe recrawl, alert-state exclusions, cursor wrap). Tests do not verify TLS, real sites, Selenium behavior, actual model detection, or BIMA API delivery. Most importantly, the main crawler's browser DNS/egress/redirect safety is NOT solved by this patch.

Search-engine, passive-DNS and threat-intelligence providers can be integrated in later phases. Registry sync remains a separate read-only integration; while `urls.json` is manually updated the baseline inventory may miss newly delegated domains.
