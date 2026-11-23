# BIMA v12 — Phishing Shadow Triage (pasif, tanpa alert otomatis)

**Basis**: GitHub `oz213/bima`, `main` commit `1abd46a` (23 Nov 2026), setelah Discovery Unlimited v10 dan Discovery History v11. **Tujuan**: meningkatkan pendeteksian indikator phishing pada HTML yang *sudah* dikumpulkan oleh crawler HTTP-first, tanpa mengganti keputusan judi online dan tanpa mengubah IndoBERT.

## 1. Yang berubah

- `multi/phishing_signals.py` masih melakukan pemeriksaan v1 secara default. Hanya jika **dua flag** di bawah diaktifkan, pemeriksaan v12 juga menambahkan metadata ke `phishing_triage` dalam `classifier_json`:
  - `BIMA_PHISHING_SHADOW=1`
  - `BIMA_PHISHING_V12=1`
- Sampling deterministik mengikuti `BIMA_PHISHING_SHADOW_RATE`, default v1 `0.05` (5%). Pilihan canary: `0.01` (1%). Pengaturan ini **tidak** mengubah jumlah domain Discovery yang diimpor; `0` di Discovery tetap berarti *unlimited*.
- Indikator v12: form OTP saja dengan target lintas origin, tujuan yang bukan HTTP(S), form password dengan metode **GET eksplisit**, halaman credential melalui HTTP, tujuan dengan userinfo, literal IP, port nonstandar, dan action malformed.
- `multi/phishing_shadow_report_v12.py`: **read-only** pada SQLite jobs atau history v11. Ringkasan berisi jumlah telemetri, status, indikator, dan ID job untuk review. Tidak mencetak URL, konten HTML, username, data form, atau `classifier_json` lengkap.
- Penambahan `--v12` ke CLI offline `multi/phishing_signals.py` untuk menguji HTML sintetis tanpa perlu menjalankan crawler.
- **Tidak ada** DB migration, trigger baru, cron, worker tambahan, koneksi API/reputasi, perubahan Git/IndoBERT model, browser atau payload alert.

**Batas:** `review_recommended` hanya alasan **review oleh analis**, bukan verdict phishing. Cross-origin bisa sah (SSO). JS-rendered forms atau iframe login yang tidak tampak pada HTML HTTP mungkin tidak terdeteksi. Static HTML parser tidak membuktikan pencurian kredensial. Sampling terbatas dan selection bias tidak boleh dibaca sebagai prevalence/akurasi penuh. Indikator malware belum ada dalam v12.

## 2. Mac — periksa, terapkan, dan uji

Simpan patch `bima-phishing-shadow-v12.patch` ke `~/Downloads`, lalu:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

git status -sb
git log -1 --oneline

git apply --check "$HOME/Downloads/bima-phishing-shadow-v12.patch"
git apply "$HOME/Downloads/bima-phishing-shadow-v12.patch"

PYTHONPATH=multi:multi/tests python3 -B -m unittest discover \
  -s multi/tests -p 'test_phishing_signals_v1.py' -v
PYTHONPATH=multi:multi/tests python3 -B -m unittest discover \
  -s multi/tests -p 'test_phishing_shadow_v12.py' -v

git diff --check
```

Keempat suite Discovery v10/v11 juga bisa diuji jika tersedia. **Jangan gunakan `set -e` langsung di terminal interaktif zsh**; jika perlu gunakan `bash <<'BASH'` dan tampilkan kegagalan perintah.

Uji HTML sintetis tanpa network. CLI *standalone* v12:

```bash
printf '%s\n' '<form method="post" action="https://other.example/submit"><input name="otp" autocomplete="one-time-code"></form>' > /tmp/bima-phishing-v12-test.html
PYTHONPATH=multi python3 -B multi/phishing_signals.py \
  --html-file /tmp/bima-phishing-v12-test.html \
  --url https://example.id/login --v12
```

Hasil yang diharapkan: `version=phishing_shadow_v12`, `status=review_recommended`, `signal_codes` memuat `otp_cross_origin_action`. **Tidak ada request ke URL dalam fixture.** Berkas fixture hanya berisi contoh publik, bukan kredensial nyata.

Setelah test sukses, **stage hanya lima file berikut** (checkout Mac mungkin memiliki fitur lain belum commit):

```bash
git add -- \
  multi/phishing_signals.py \
  multi/phishing_signals_v12.py \
  multi/phishing_shadow_report_v12.py \
  multi/tests/test_phishing_shadow_v12.py \
  docs/OPERASIONAL_PHISHING_SHADOW_V12.md

git diff --cached --check
git diff --cached --stat
git commit -m "feat: add passive phishing shadow triage v12"
git push origin main
```

## 3. Server — deploy kode tanpa mengganggu crawler

Jalankan sebagai root, sesudah push:

```bash
REPO=/home/fauzi/crawler-rebuild
runuser -u fauzi -- git -C "$REPO" status -sb
runuser -u fauzi -- git -C "$REPO" pull --ff-only origin main

for test in test_phishing_signals_v1.py test_phishing_shadow_v12.py; do
  runuser -u fauzi -- env PYTHONPATH="$REPO/multi:$REPO/multi/tests" \
    "$REPO/venv/bin/python3" -B -m unittest discover \
    -s "$REPO/multi/tests" -p "$test" -v
done

systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
```

**Code deploy saja tidak mengaktifkan shadow pada proses crawler yang telah berjalan.** Jangan restart crawler di tengah Registry Snapshot hanya untuk menguji v12. Jalankan dulu laporan read-only:

```bash
REPO=/home/fauzi/crawler-rebuild
DB="$REPO/multi/state/crawler_production.sqlite3"
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/multi/phishing_shadow_report_v12.py" \
  --db "$DB" --source jobs --limit 500 --review-limit 10
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/multi/phishing_shadow_report_v12.py" \
  --db "$DB" --source history --limit 500 --review-limit 10
```

`without_shadow` berarti tidak ada telemetri phishing pada sampel, **bukan** berarti domain aman. Laporan `history` menghitung event, bukan jumlah domain unik. Sampel dengan `ORDER BY id DESC LIMIT` bias terhadap job/event terbaru, bukan random sample. Jika v12 belum aktif, `v12_records=0` adalah hal normal.

## 4. Canary produksi: hanya pada restart terencana

Setelah meninjau dampak CPU, NFS, dan stabilitas crawler, operator dapat menyiapkan systemd drop-in (tanpa menyentuh konfigurasi rahasia atau env yang lain):

```ini
[Service]
Environment=BIMA_PHISHING_SHADOW=1
Environment=BIMA_PHISHING_V12=1
Environment=BIMA_PHISHING_SHADOW_RATE=0.01
```

Gunakan `systemctl edit pandi-crawler-multi.service`, periksa konflik override yang sudah ada, `systemctl daemon-reload`, lalu **restart hanya dalam jendela perubahan terencana**. Pastikan status job `http_inflight` / `browser_queued` pulih sesuai mekanisme recovery. BIMA tetap melakukan klasifikasi judi online dengan IndoBERT, dan `phishing_triage` v12 tidak boleh mengubah `status`/`reason`/`ml_conf` hasil legacy atau memicu alert baru.

Verifikasi setelah ada pemrosesan baru dengan laporan di atas, periksa `v12_records` dan `triage_statuses`. Gunakan `--review-limit` untuk ID job tanpa URL. Bila terjadi `v12_error=scanner_error` atau dampak kinerja: turunkan rate / nonaktifkan `BIMA_PHISHING_V12=0` pada konfigurasi, lalu gunakan restart terencana. **Jangan reset DB, menghapus history, mengulang import 499.999 domain, atau mematikan IndoBERT.**

## 5. Lanjutan setelah pilot

1. Kumpulkan label kasus phishing benar, legitimate SSO, login biasa, dan uncertain dari analis. Buat evaluasi precision/recall pada sampel yang berlabel; jangan memakai `review_recommended` sebagai ground truth.
2. Tambahkan tinjauan brand impersonation berbasis referensi resmi/allowlist milik PANDI, dan korelasi RDAP/DNS. URL/host dilindungi dalam laporan umum.
3. Integrasikan reputasi phishing/malware (Web Risk, URLhaus) sebagai enrichment **read-only** dengan kuota dan kebijakan sumber, bukan perintah download/execute malware.
4. Rancang case schema terpisah `phishing/malware/botnet/pharming/spam/gambling` dengan bukti terverifikasi serta review manusia sebelum tindakan.

### Keterbatasan pengujian

Paket diverifikasi secara offline dengan sumber `phishing_signals.py` yang sesuai dengan file di GitHub pada commit `1abd46a`; bukan uji produksi langsung. Sesudah diaplikasikan, uji ulang di Mac dan Linux, cek `git apply --check`, dan cek seluruh service. Tidak ada perubahan manual pada sistem produksi oleh paket ini.
