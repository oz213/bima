# BIMA v4.3.2 — DNS Confidence & Redirect Evidence Refinement

**Basis GitHub:** `oz213/bima` commit `3770108` (BIMA v4.3.1). **Alur deploy:** Mac (apply patch dan test) → `git push origin main` → Linux `sudo -u fauzi -H git pull --ff-only origin main` → test server → preview terbatas.

## Tujuan

Membedakan tingkat kepastian hasil audit DNS dan cakupan redirect dari satu hostname `.id` ke host `.id` lain versus benar-benar keluar namespace `.id`. Tidak mengubah `crawler_multi.py`, `dns_audit.py`, `urls.json`, model IndoBERT, database produksi, mekanisme retry, Telegram, atau service/systemd.

### DNS: status lama dipertahankan, tingkat keyakinan ditambahkan

- `dns_confidence`: `confirmed`, `partial`, `suspected`, `inconclusive`.
- `dns_uncertainty_reasons`: daftar kode alasan dari vocabulary terbatas, **tanpa** raw DNS error, alamat server NS, token, atau data sensitif.
- `dns_confidence_counts`: total hasil untuk seluruh seed yang gagal DNS, termasuk yang **belum diaudit** sebagai `inconclusive`.
- `dns_status` v4.3.1 tetap dipertahankan agar konsumen lama tidak rusak.
- `dns_lame_delegation`: `confirmed` **hanya** bila ada delegasi anak `.id`, cakupan pemeriksaan authoritative lengkap dan seluruh endpoint merespons `REFUSED`.
- `dns_lame_delegation_suspected`: `suspected` bila penolakan terlihat tetapi cakupan tidak lengkap.
- `dns_nxdomain_authoritative`: `partial` bila observasi dari parent seperti `madaniresidence.co.id` hanya mencakup sebagian server (`NXDOMAIN` disertai `NO_RESPONSE`). Ini **bukan** kesimpulan domain tidak terdaftar.
- `dns_no_direct_address_observed`: `partial`; pengamatan A/AAAA langsung **tidak** mengikuti seluruh kemungkinan CNAME dan bukan vonis website tidak aktif.
- `SERVFAIL` tanpa bukti authoritative tetap `inconclusive`.
- Kode baru tidak mengubah jadwal retry maupun klasifikasi konten.

### Redirect: cakupan lebih akurat

`external` / `external_redirects` **tetap** mempunyai makna historis v4.3: target berada di luar hostname seed (atau subdomainnya). Jangan menganggap field lama ini berarti keluar dari `.id`.

Setiap observasi redirect pada preview kini memiliki:

| Field | Nilai / makna |
|---|---|
| `redirect_scope` | `same_host`, `within_seed_subdomain`, `cross_id_host`, `outside_id` |
| `outside_id` | `true` **hanya** jika tujuan tidak berada dalam namespace `.id` |
| `evidence_level` | `http_observed` untuk HTTP Location; `static_signal` untuk JavaScript statis dan Meta Refresh |
| `outside_id_redirects` | Hitungan observasi tujuan di luar `.id` |
| `redirect_scope_counts` | Hitungan per scope untuk seluruh redirect yang tercatat, termasuk `robots.txt` |
| `redirect_evidence_counts` | Hitungan HTTP teramati vs sinyal statis |

`cross_id_host` artinya pindah hostname tetapi kedua host tetap `.id`; **tidak otomatis membuktikan registrant berbeda** (tidak menggunakan registrable-domain / Public Suffix List). Contoh: `fl3xu5.web.id` → `fl3xu5.id` menghasilkan `redirect_scope=cross_id_host`, `outside_id=false`, `external=true` (legacy) dan `evidence_level=http_observed`.

Penyesuaian prioritas **investigasi teknis**, bukan confidence judi online:

| Kategori | HTTP | Meta Refresh | JavaScript statis |
|---|---:|---:|---:|
| Benar-benar luar `.id` | 95 (sama dengan v4.3) | 85 (sama) | 75 (sama) |
| Host `.id` lain | 55 | 45 | 35 |

Dynamic JavaScript hint tetap prioritas 30; redirect `robots.txt` tidak menjadi kandidat Browser Review. Browser **tidak** otomatis dieksekusi, tidak ada fetch tujuan lintas seed / namespace, dan redirect bukan vonis abuse. Path URL bisa tercantum dalam output meski query, fragmen, dan kredensial disamarkan; jangan sebarkan log mentah secara publik.

## Berkas patch

1. `multi/discovery_dns.py` — menambahkan key tingkat keyakinan dan alasan ketidakpastian.
2. `multi/proactive_discovery.py` — klasifikasi scope, prioritas Browser Review, agregasi statistik dan preview.
3. `multi/redirect_intelligence.py` — metadata evidence pada redirect chains.
4. `multi/tests/test_discovery_v432.py` — tes offline safety, scope, redaksi, DNS dan integrasi.
5. `README_DISCOVERY_V432.md` — panduan dan batasan.

## 1. Mac: pemeriksaan dan apply

Unduh file `bima-v4.3.1-to-v4.3.2-evidence-refinement.patch` ke folder `~/Downloads` pada Mac.

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

git status --short
git log -1 --oneline
# Sebelum apply: HEAD=3770108 dan working tree bersih.

git apply --check ~/Downloads/bima-v4.3.1-to-v4.3.2-evidence-refinement.patch && \
git apply ~/Downloads/bima-v4.3.1-to-v4.3.2-evidence-refinement.patch
```

Jangan gunakan `git reset --hard`, `git clean`, atau patch paksa jika `git apply --check` gagal. Jangan melakukan `git add -A` karena ada konfigurasi/runtime rahasia yang tidak boleh ikut Git.

## 2. Mac: test

Gunakan virtualenv pengujian yang sebelumnya telah dipakai. Pastikan `requests`, `dnspython`, dan Pillow tersedia.

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -p 'test_discovery_v432.py' -v

PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -q

git diff --check
git status --short
```

Target tes khusus v4.3.2: **28 PASS**. Target full suite pada repository Mac dengan jumlah tes terakhir 247: **275 tes**, kecuali ada perubahan lain di repo. Perubahan statistik yang diuji bersifat read-only dan tidak mengirim alert.

## 3. Mac: commit + push

```bash
git add -- \
  README_DISCOVERY_V432.md \
  multi/discovery_dns.py \
  multi/proactive_discovery.py \
  multi/redirect_intelligence.py \
  multi/tests/test_discovery_v432.py

git diff --cached --check
git diff --cached --stat
git commit -m 'feat: refine DNS confidence and .id redirect evidence'
git push origin main
git log -1 --oneline
```

Catat hash commit **yang baru** hasil `git commit`; tidak sama dengan `3770108`.

## 4. Linux: update dari GitHub, tidak mengubah file lokal lain

Tidak ada koneksi SSH langsung dari patch ini ke server. Jalankan **setelah** Mac berhasil commit+push:

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
# File lokal crawler_google.py (modified) dan domains.json (untracked)
# jika masih ada harus dipertahankan, jangan dihapus atau di-stage.

sudo -u fauzi -H git pull --ff-only origin main
sudo -u fauzi -H git log -1 --oneline

sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover \
  -s multi/tests -p 'test_discovery_v432.py' -v

sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover -s multi/tests -q
```

Jika pull tertahan karena local changes, **berhenti** dan periksa, jangan paksa reset. Tidak perlu restart `pandi-crawler-multi.service` atau `pandi-telegram-monitor.service` karena perubahan hanya pada Discovery terpisah.

## 5. Linux: uji preview yang bisa dibandingkan dengan v4.3.1

Stratified seed `21`, 20 seed, 3 audit, tanpa `--apply`:

```bash
cd /home/fauzi/crawler-rebuild
set -o pipefail

sudo -u fauzi -H ./venv/bin/python3 -B \
  multi/proactive_discovery.py \
  --input urls.json \
  --db multi/state/crawler_production.sqlite3 \
  --seed-selection stratified \
  --sampling-seed 21 \
  --seed-limit 20 \
  --scan-web --http-fallback --canonical-www --advanced-js \
  --dns-audit-limit 3 --dns-audit-timeout 1.5 \
  --browser-review-limit 5 --redirect-chain-preview 5 \
  --diagnostic-seeds 10 --max-candidates-per-seed 10 \
  --max-sitemaps 1 --probe-candidates 0 \
  --timeout 6 --request-interval 0.5 \
| ./venv/bin/python3 -c '
import json, sys
x=json.load(sys.stdin)
keys=("ok", "mode", "seeds_this_run", "dns_failed_seeds", "dns_audits_performed",
      "dns_status_counts", "dns_confidence_counts", "preview_dns_audits",
      "external_redirects", "outside_id_redirects", "redirect_scope_counts",
      "redirect_evidence_counts", "preview_browser_review", "preview_redirects",
      "new_jobs", "pending_before", "pending_after")
print(json.dumps({k:x.get(k) for k in keys}, ensure_ascii=False, indent=2))
'
```

Hasil DNS dapat berubah seiring perubahan delegasi/NS. Dari pengujian v4.3.1 terakhir, untuk tiga audit yang sama diharapkan **bila hasil DNS tidak berubah**: 1 `confirmed` (lame delegation), 2 `partial` (NXDOMAIN parent parsial dan no direct address), 5 `inconclusive` (tidak diaudit). Untuk `fl3xu5.web.id → fl3xu5.id`, `cross_id_host`, bukan `outside_id`; jika redirect muncul kembali, ia **tidak** menaikkan `outside_id_redirects`.

**Jangan** aktifkan `--apply` / timer Discovery, jangan menaikkan jumlah worker browser, jangan menonaktifkan IndoBERT, dan jangan mengubah retry produksi berdasarkan hasil preview ini. Statistik dari unit test/preview bukan bukti judi online atau kesiapan rollout otomatis 1,5 juta domain.

## Rollback

Sebelum commit di Mac dan hanya bila perubahan dari patch ini belum diubah lagi:

```bash
git apply --reverse --check ~/Downloads/bima-v4.3.1-to-v4.3.2-evidence-refinement.patch && \
git apply --reverse ~/Downloads/bima-v4.3.1-to-v4.3.2-evidence-refinement.patch
```

Setelah commit dan push, gunakan `git revert <hash-commit-v432>` **di Mac**, test, push, dan pull terkontrol di Linux. Jangan mengedit source server secara manual. Tidak ada migrasi database pada v4.3.2.
