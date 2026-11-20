# PANDI Crawler Multi-Worker

**Versi aplikasi:** 1.2.4.3 (memory-only screenshot)  
**Platform:** Linux / Python 3.11 / SQLite / Google Chrome for Testing  
**Fungsi:** Pemantauan dan klasifikasi konten situs berdomain `.id`, termasuk indikasi judi online, dengan opsi pelaporan ke BIMA.

> **PENTING — Produksi:** Crawler dapat mengirim alert nyata ke BIMA. Selalu gunakan `--dry-run` untuk pengujian dan **jangan menjalankan dua instance pada database atau antrean yang sama**. Saat migrasi dari proses lama, pastikan proses tersebut benar-benar berhenti sebelum menjalankan service baru. Database baru tidak memiliki riwayat deduplikasi BIMA.

## 1. Ringkasan arsitektur

```text
urls.json (input .id)
       |
       v
  Import & deduplikasi URL
       |
       v
SQLite: multi/state/crawler_production.sqlite3 (WAL)
       |
       +--> HTTP workers (Requests, pemeriksaan target publik)
       |          |
       |          +--> Keyword + aturan konteks + Classic ML + IndoBERT
       |          |
       |          +--> Selesai / retry HTTP / antrean browser
       |                                          |
       |                                          v
       |                                 Satu worker Chrome/Selenium
       |                                          |
       |                                 Screenshot + watermark (RAM)
       |                                          |
       |                                  dry-run ATAU BIMA API
       v
Monitoring SQLite / log systemd / Telegram (opsional)
```

Fitur utama:

- HTTP crawler paralel (`--http-workers`, 1–16), satu model IndoBERT bersama, dan satu browser worker.
- Deteksi gabungan dari **Classic ML**, IndoBERT, kata kunci, dan aturan konteks.
- Browser **Chrome for Testing** versi terkelola, dipasangkan dengan ChromeDriver yang sesuai.
- Screenshot PNG untuk alert dibentuk dan dikirim dari **memori**; gambar **tidak disimpan** pada disk maupun SQLite (versi 1.2.4.3).
- SQLite dengan pelacakan URL/status, retry jaringan, dan pembatasan target HTTP(S) publik.
- Input hanya hostname berakhiran `.id`, port HTTP(S) standar 80/443. Redirect publik dapat dianalisis sesuai validasi crawler.

**Batasan:** Unit test dan validasi model lokal tidak membuktikan keberhasilan Selenium pada setiap situs ataupun pengiriman BIMA produksi. Status `pending_alert` harus diperiksa ke BIMA sebelum pengiriman ulang karena respons yang tidak terkonfirmasi bisa saja sudah diterima.

## 2. Struktur repository

Struktur yang diharapkan setelah source di-push ke Git dan aset lokal dikonfigurasi:

```text
crawler-rebuild/
├── README.md
├── crawler_url.py                 # Engine klasifikasi dan pengiriman BIMA
├── gambling_classifier.pkl       # Model Classic ML (aset tepercaya)
├── arial.ttf                      # Font watermark
├── config.json                    # Konfigurasi lokal / rahasia, JANGAN commit
├── urls.json                      # Daftar URL lokal, JANGAN commit
├── models/
│   └── indobert-judol/
│       ├── config.json
│       ├── model.safetensors      # Bobot fine-tuned asli; simpan di storage privat
│       ├── vocab.txt             # Atau tokenizer.json yang kompatibel
│       └── special_tokens_map.json   # Bila tersedia
├── multi/
│   ├── crawler_multi.py          # Entry point multi-worker
│   ├── chrome_runtime.py
│   ├── setup_chrome.py
│   ├── dns_audit.py
│   ├── telegram_monitor.py       # Opsional
│   ├── crawler_watchdog.py       # Opsional
│   ├── tests/
│   └── state/                   # SQLite/WAL/SHM runtime, JANGAN commit
└── venv/                        # Virtual environment lokal, JANGAN commit
```

**Aset model tidak disertakan dalam source Git secara otomatis.** Khusus IndoBERT, gunakan bobot fine-tuned PANDI, bukan model dasar IndoBERT dari Hugging Face. File `gambling_classifier.pkl` juga harus berasal dari sumber tepercaya (file pickle dapat mengeksekusi kode ketika dimuat).

## 3. Persyaratan server

- Linux Ubuntu (contoh deployment menggunakan user `fauzi` dan direktori `/home/fauzi/crawler-rebuild`).
- Python **3.11** direkomendasikan; paket `python3-venv`, `python3-pip`, dan Git.
- CPU minimal 2 vCPU dan RAM sekitar 4 GiB untuk konfigurasi awal yang digunakan; sesuaikan worker/batch dengan kapasitas server.
- Ruang disk untuk model, cache browser, SQLite, WAL, log, dan backup.
- Akses HTTPS ke sumber Chrome for Testing pada pemasangan pertama jika cache belum tersedia.
- Akses ke situs yang **diizinkan untuk dipindai** dan, hanya untuk produksi, endpoint BIMA resmi.
- Identitas Linux non-root untuk menjalankan browser. Jangan menambahkan opsi Chrome `--no-sandbox`.

### 3.1 Instalasi source pada server baru

Contoh berikut mengasumsikan repository **private** telah tersedia. Ganti URL contoh dengan URL repository sebenarnya.

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip

# Pastikan user aplikasi sudah tersedia; contoh ini memakai user fauzi.
id fauzi

# HANYA untuk server baru / direktori yang belum ada.
sudo -u fauzi git clone "<URL_REPOSITORY_PRIVATE>" /home/fauzi/crawler-rebuild

APP=/home/fauzi/crawler-rebuild
sudo -u fauzi python3 -m venv "$APP/venv"
sudo -u fauzi "$APP/venv/bin/python3" -m pip install --upgrade pip
```

Pada server yang **sudah** memiliki `/home/fauzi/crawler-rebuild`, **jangan clone ulang atau hapus direktori itu**. Periksa status Git dan gunakan `git pull` hanya setelah memeriksa perubahan lokal serta backup konfigurasi.

### 3.2 Instalasi dependensi Python

Dependensi yang dipakai dalam source:

```bash
APP=/home/fauzi/crawler-rebuild

# PyTorch CPU; gunakan wheel yang kompatibel dengan OS/arsitektur/Python Anda.
sudo -u fauzi "$APP/venv/bin/python3" -m pip install \
  torch --index-url https://download.pytorch.org/whl/cpu

sudo -u fauzi "$APP/venv/bin/python3" -m pip install \
  requests beautifulsoup4 pillow joblib scikit-learn psutil \
  selenium undetected-chromedriver transformers safetensors \
  'dnspython>=2.6,<3'

sudo -u fauzi "$APP/venv/bin/python3" -m pip check
```

**Untuk reproduksibilitas:** gunakan versi dependensi yang sudah tervalidasi dengan model PANDI (terutama `torch`, `transformers`, `scikit-learn`). Source ini belum menyediakan lockfile dependensi lengkap. Jangan menganggap instalasi versi terbaru otomatis identik dengan lingkungan produksi yang telah diuji.

### 3.3 Siapkan direktori dan aset

```bash
APP=/home/fauzi/crawler-rebuild
sudo install -d -o fauzi -g fauzi -m 750 \
  "$APP/multi/state" "$APP/models/indobert-judol"
```

Salin secara aman dari penyimpanan internal:

1. `gambling_classifier.pkl` ke root proyek.
2. `arial.ttf` ke root proyek.
3. Bobot IndoBERT asli, `config.json` model, dan tokenizer/vocabulary ke `models/indobert-judol/`.

Pastikan user `fauzi` dapat membaca semua aset, dan **jangan menggunakan bobot model publik generik sebagai pengganti classifier terlatih**. Untuk model yang telah dipulihkan, sebaiknya verifikasi hash terhadap backup tepercaya.

## 4. Konfigurasi aplikasi (`config.json`)

`crawler_url.py` membaca `config.json`, `urls.json`, model Classic ML, dan font saat diimpor. Berkas tersebut harus tersedia **meskipun** perintah dijalankan dengan `--dry-run`.

Contoh **minimal untuk pengujian** (nilai threshold/KEYWORDS hanyalah contoh; pertahankan nilai resmi dari konfigurasi produksi yang sudah diuji):

```json
{
  "X_API_KEY": "",
  "ENDPOINT_URL": "",
  "KEYWORDS": ["judi", "slot", "togel", "casino"],
  "FONT_PATH": "/home/fauzi/crawler-rebuild/arial.ttf",
  "FONT_SIZE": 20,
  "ENABLE_INDO_BERT": true,
  "INDO_BERT_MODEL_PATH": "/home/fauzi/crawler-rebuild/models/indobert-judol",
  "INDO_BERT_THRESHOLD": 0.8,
  "INDO_BERT_STRONG_THRESHOLD": 0.92,
  "ML_THRESHOLD": 0.75,
  "ML_STRONG_THRESHOLD": 0.9,
  "MIN_ALERT_CONFIDENCE": 0.6,
  "REQUIRE_GAMBLING_CONTEXT_FOR_ML": true,
  "ALERT_CONNECT_TIMEOUT": 5,
  "ALERT_READ_TIMEOUT": 30,
  "ALERT_RAISE_ON_FAILURE": false
}
```

Sebelum **mode produksi** tanpa `--dry-run`, isi `X_API_KEY` dan `ENDPOINT_URL` dari konfigurasi/secret store BIMA resmi. Jangan gunakan endpoint percobaan atau kredensial lama tanpa verifikasi. Konfigurasi dapat memuat pengaturan tambahan; jangan membuang pengaturan asli tanpa audit.

```bash
APP=/home/fauzi/crawler-rebuild
sudo chown fauzi:fauzi "$APP/config.json"
sudo chmod 600 "$APP/config.json"
python3 -m json.tool "$APP/config.json" >/dev/null
```

**Catatan penting:** engine versi rebuild mendukung variabel `PANDI_CRAWLER_BASE_DIR`. Jika tidak diatur, ia kembali ke path historis `/home/fauzi/crawler`. Selalu set ke `/home/fauzi/crawler-rebuild` pada service atau perintah manual.

## 5. Daftar URL (`urls.json`)

Format yang dianjurkan:

```json
{
  "urls": [
    "https://contoh.id",
    "https://contoh.web.id/halaman",
    "contoh.or.id"
  ]
}
```

- `urls` wajib berupa array pada `urls.json` root aplikasi (dibaca oleh engine).
- `crawler_multi.py` juga dapat membaca input berupa array langsung melalui `--input`, tetapi **root `urls.json` tetap harus ada** untuk engine.
- URL tanpa skema dinormalisasi menjadi HTTPS; URL duplikat diabaikan saat impor.
- URL yang bukan `.id`, port selain 80/443, atau URL tidak valid dilewati dan dihitung dalam log.
- `--import-limit 0` mengimpor seluruh daftar valid; nilai positif membatasi jumlah URL valid unik yang diimpor.

Pasang dan validasi file dari sumber internal:

```bash
APP=/home/fauzi/crawler-rebuild
python3 -m json.tool /home/fauzi/urls_baru.json >/dev/null
sudo install -o fauzi -g fauzi -m 640 \
  /home/fauzi/urls_baru.json "$APP/urls.json"

python3 - <<'PY'
import json
from pathlib import Path
p = Path('/home/fauzi/crawler-rebuild/urls.json')
a = json.loads(p.read_text(encoding='utf-8'))
assert isinstance(a, dict) and isinstance(a.get('urls'), list)
assert a['urls'], 'urls.json kosong'
print('Total input:', len(a['urls']))
PY
```

## 6. Mulai dari nol dengan database baru

Database SQLite **dibuat otomatis** ketika aplikasi pertama kali berjalan melalui `--db`. Tidak perlu membuat tabel secara manual. Lokasi produksi yang direkomendasikan:

```text
/home/fauzi/crawler-rebuild/multi/state/crawler_production.sqlite3
```

**Syarat sebelum reset:** service baru berhenti; proses crawler lain tidak memakai database tersebut; URL baru sudah benar; dan data/riwayat BIMA lama memang diputuskan untuk tidak dipakai. Hati-hati: mengosongkan database juga menghilangkan deduplikasi alert lintas-run.

```bash
APP=/home/fauzi/crawler-rebuild
DB="$APP/multi/state/crawler_production.sqlite3"

# Jangan jalankan reset jika proses crawler staging masih aktif.
if systemctl is-active --quiet pandi-crawler-multi.service || \
   pgrep -u fauzi -f '/home/fauzi/crawler-rebuild/multi/crawler_multi.py' >/dev/null; then
  echo 'STOP: crawler staging masih berjalan; database tidak boleh dihapus'
  exit 1
fi

# Jalankan tiga baris berikut HANYA jika reset sudah disetujui.
# Hanya menyasar database staging; tidak menyentuh file pada path crawler lama.
rm -f -- "$DB" "$DB-wal" "$DB-shm"
ls -lah "$APP/multi/state/"
```

Pada insiden recovery November 2026, proses lama mungkin masih memegang database yang **sudah terhapus dari direktori** melalui file descriptor. Reset database staging di atas **tidak** menghentikan proses lama dan **bukan** instruksi untuk menghapus snapshot recovery. Lakukan penghentian/migrasi proses lama secara terkontrol sebelum mengaktifkan crawler baru.

## 7. Pengujian sebelum deploy

### 7.1 Unit test offline

```bash
APP=/home/fauzi/crawler-rebuild
cd "$APP/multi"
runuser -u fauzi -- env PYTHONPATH=. \
  "$APP/venv/bin/python3" \
  -m unittest discover -s tests -p 'test_*.py' -q
```

Pada lingkungan pemulihan 20 November 2026: **51 unit test PASS**. Output simulasi `ChromeDriver version issue` atau `BIMA alert sent` dalam unit test dapat berasal dari *mock*, bukan bukti adanya instalasi/download atau pengiriman aktual.

### 7.2 Verifikasi Chrome dan ChromeDriver

Mode `managed` menggunakan cache milik user `fauzi`. Pemeriksaan browser (unduh jika cache belum ada):

```bash
APP=/home/fauzi/crawler-rebuild
runuser -u fauzi -- "$APP/venv/bin/python3" \
  "$APP/multi/setup_chrome.py" \
  --cache-dir /home/fauzi/.cache/pandi-crawler/chrome
```

Output `Chrome` dan `ChromeDriver` harus menunjukkan versi pasangan yang sesuai. **Pemeriksaan ini belum menjalankan Selenium ke situs nyata**.

### 7.3 Smoke test satu URL (tidak mengirim BIMA)

Jalankan **hanya pada VM pengujian atau setelah proses crawler lama tidak lagi bersaing menggunakan RAM**. Uji HTTP/Selenium dan screenshot dapat memerlukan memori besar.

```bash
APP=/home/fauzi/crawler-rebuild
install -o fauzi -g fauzi -m 640 /dev/stdin \
  "$APP/multi/state/smoke_urls.json" <<'JSON'
{"urls":["https://contoh.id"]}
JSON

runuser -u fauzi -- env \
  PANDI_CRAWLER_BASE_DIR="$APP" \
  HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  "$APP/venv/bin/python3" "$APP/multi/crawler_multi.py" \
  --engine-file "$APP/crawler_url.py" \
  --input "$APP/multi/state/smoke_urls.json" \
  --db "$APP/multi/state/smoke_test.sqlite3" \
  --import-limit 1 --limit 1 --dry-run \
  --http-workers 1 --bert-batch-size 1 --torch-threads 1 \
  --chrome-mode managed \
  --chrome-cache-dir /home/fauzi/.cache/pandi-crawler/chrome
```

Ganti `contoh.id` dengan **domain `.id` yang memang diizinkan untuk pengujian**. `--dry-run` mencegah fungsi pengiriman BIMA, tetapi tetap membuat permintaan ke website target. Gunakan database smoke terpisah, jangan database produksi. Beberapa URL mungkin selesai pada jalur HTTP tanpa membutuhkan screenshot; untuk menguji Selenium, pilih URL uji yang memang memicu browser sesuai aturan engine.

## 8. Deploy menggunakan systemd

**Jangan aktifkan service selama crawler lama masih berjalan.** Pastikan aset model, `config.json`, `urls.json`, izin folder, Chrome, dan uji coba sudah siap.

Buat service (atau ganti unit lama setelah mengevaluasi konfigurasi sebelumnya):

```bash
sudo tee /etc/systemd/system/pandi-crawler-multi.service >/dev/null <<'UNIT'
[Unit]
Description=PANDI Crawler Multi-Worker v1.2.4.3
Wants=network-online.target
After=network-online.target
ConditionPathExists=/home/fauzi/crawler-rebuild/crawler_url.py
ConditionPathExists=/home/fauzi/crawler-rebuild/models/indobert-judol/model.safetensors

[Service]
Type=simple
User=fauzi
Group=fauzi
WorkingDirectory=/home/fauzi/crawler-rebuild
Environment=PYTHONUNBUFFERED=1
Environment=PANDI_CRAWLER_BASE_DIR=/home/fauzi/crawler-rebuild
Environment=HF_HUB_OFFLINE=1
Environment=TRANSFORMERS_OFFLINE=1
ExecStart=/home/fauzi/crawler-rebuild/venv/bin/python3 /home/fauzi/crawler-rebuild/multi/crawler_multi.py --engine-file /home/fauzi/crawler-rebuild/crawler_url.py --input /home/fauzi/crawler-rebuild/urls.json --db /home/fauzi/crawler-rebuild/multi/state/crawler_production.sqlite3 --import-limit 0 --limit 0 --http-workers 3 --bert-batch-size 2 --torch-threads 2 --chrome-mode managed --chrome-cache-dir /home/fauzi/.cache/pandi-crawler/chrome --retry-network
Restart=on-failure
RestartSec=30
TimeoutStopSec=180
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemd-analyze verify pandi-crawler-multi.service
sudo systemctl cat pandi-crawler-multi.service
sudo systemctl is-active pandi-crawler-multi.service || true
sudo systemctl is-enabled pandi-crawler-multi.service || true
```

**Penting untuk instalasi yang sudah ada:** periksa file pada `/etc/systemd/system/pandi-crawler-multi.service.d/`. Drop-in lama tetap berlaku meskipun unit utama ditulis ulang. Jika masih ada `ConditionPathExists` yang menuntut database SQLite lama, hilangkan kondisi itu: **database baru memang belum ada**. Gunakan `systemctl cat` untuk meninjau konfigurasi gabungan sebelum start.

Mode produksi di atas **tidak** memakai `--dry-run`, sehingga dapat mengirim alert BIMA sungguhan. Aktifkan hanya setelah memastikan endpoint/key benar dan risiko duplikasi terkendali.

Ketika sudah siap dan **hanya satu crawler** yang akan berjalan:

```bash
sudo systemctl start pandi-crawler-multi.service
sudo systemctl status pandi-crawler-multi.service --no-pager
sudo journalctl -u pandi-crawler-multi.service -f
```

Setelah uji operasi disetujui, jika ingin service dapat dijalankan melalui `systemctl` saat diperlukan:

```bash
sudo systemctl enable pandi-crawler-multi.service
```

`enable` pada service berarti systemd mencoba menjalankannya saat boot. Untuk operasi batch terjadwal, pertimbangkan **timer** di bawah daripada mengaktifkan service permanen sekaligus timer.

### 8.1 Penting: retry jaringan bekerja saat awal run

Flag `--retry-network` memindahkan **retry HTTP/DNS yang sudah jatuh tempo** kembali ke `pending` **saat startup**. Jika retry baru dijadwalkan ketika proses masih berjalan, retry itu tidak otomatis diambil kembali dalam run yang sama. Setelah seluruh antrean yang dapat diproses habis, program dapat keluar **normal** (`exit 0`), dan `Restart=on-failure` tidak akan menghidupkannya ulang.

Jika ingin memproses retry berkala tanpa cron lama, pilih mekanisme penjadwalan yang disetujui operator. Contoh timer **opsional**, aktifkan setelah service teruji dan tanpa menduplikasi jadwal lain:

```ini
# /etc/systemd/system/pandi-crawler-multi.timer
[Unit]
Description=Jadwal ulang crawler PANDI untuk retry yang jatuh tempo

[Timer]
OnBootSec=5min
OnUnitInactiveSec=30min
Unit=pandi-crawler-multi.service

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
# Jalankan hanya bila memang ingin mengaktifkan eksekusi berkala:
sudo systemctl enable --now pandi-crawler-multi.timer
sudo systemctl list-timers --all | grep pandi-crawler
```

Timer tidak perlu dipasang jika crawler sengaja dijalankan sekali per daftar URL. Jangan mengaktifkan timer sebelum cutover selesai.

## 9. Operasional dan monitoring

```bash
# Status layanan dan log
sudo systemctl status pandi-crawler-multi.service --no-pager
sudo journalctl -u pandi-crawler-multi.service -n 100 --no-pager
sudo journalctl -u pandi-crawler-multi.service -f

# Hentikan dengan SIGTERM yang ditangani aplikasi (dalam maintenance window)
sudo systemctl stop pandi-crawler-multi.service

# Periksa apakah ada crawler lain
ps -eo pid,user,stat,args | grep '[c]rawler_multi.py'

# Kapasitas dan RAM
free -h
df -h /home/fauzi
```

Audit status SQLite **tanpa mengubah status job**:

```bash
python3 - <<'PY'
import sqlite3
from pathlib import Path
p = Path('/home/fauzi/crawler-rebuild/multi/state/crawler_production.sqlite3')
if not p.is_file():
    raise SystemExit('Database belum dibuat')
conn = sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True)
try:
    for status, count in conn.execute(
        'SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY COUNT(*) DESC'
    ):
        print(f'{status}: {count}')
finally:
    conn.close()
PY
```

Status utama: `pending`, `http_inflight`, `retry_http`, `browser_queued`, `complete`, `inconclusive`, `alerted`, `pending_alert`, dan `dry_run_alert` (khusus tes). `pending_alert` **tidak dikirim ulang secara otomatis**. Jangan gunakan `--retry-pending-alerts` sebelum rekonsiliasi BIMA. `--retry-failed` dan `--retry-dry-run` juga tindakan operator eksplisit.

**Backup SQLite:** Saat crawler aktif dengan WAL, jangan hanya menyalin berkas `.sqlite3` tanpa mekanisme backup konsisten. Gunakan SQLite backup API atau hentikan writer dengan benar; simpan backup di storage terpisah, dan verifikasi `PRAGMA integrity_check`.

### 9.1 Telegram Monitor dan watchdog (opsional)

`multi/telegram_monitor.py` dan `multi/crawler_watchdog.py` tersedia dalam paket pemulihan, tetapi unit/env lama dapat masih menunjuk direktori `/home/fauzi/crawler` serta database sebelumnya. Perbarui lokasi aplikasi, database, dan token Telegram **secara terpisah** sebelum mengaktifkan monitor. Jangan menyimpan token bot di Git, jangan menghidupkan watchdog lama tanpa pengecekan konfigurasi.

## 10. Referensi parameter CLI

| Parameter | Fungsi |
|---|---|
| `--engine-file` | Path ke `crawler_url.py` yang telah dipatch untuk lokasi deploy |
| `--input` | JSON sumber URL |
| `--db` | SQLite job database (dibuat otomatis jika belum ada) |
| `--http-workers 3` | Jumlah worker HTTP; rentang 1–16 |
| `--import-limit 0` | Impor semua URL unik valid |
| `--limit 0` | Proses semua pekerjaan `pending` pada run ini |
| `--bert-batch-size 2` | Ukuran batch/chunk inferensi IndoBERT per URL |
| `--torch-threads 2` | Jumlah thread intra-op PyTorch |
| `--chrome-mode managed` | Chrome for Testing beserta ChromeDriver cocok |
| `--chrome-cache-dir` | Lokasi cache Chrome milik user crawler |
| `--retry-network` | Ambil retry jaringan yang jatuh tempo saat awal run |
| `--network-retry-minutes 30` | Jeda default retry jaringan sementara |
| `--dns-no-address-retry-minutes 360` | Jeda untuk DNS tanpa address |
| `--dns-not-found-retry-minutes 1440` | Jeda untuk DNS nama tidak ditemukan |
| `--max-network-attempts 3` | Jumlah maksimal percobaan HTTP sebelum menjadi inconclusive |
| `--dry-run` | Tetap proses dan screenshot, **tanpa** panggilan BIMA |
| `--http-only` | Hanya jalur HTTP + klasifikasi; pekerjaan browser tetap mengantre |
| `--retry-failed` | Ulang status `failed`/`inconclusive` dengan sengaja |
| `--retry-dry-run` | Ulang hasil `dry_run_alert` dengan sengaja |
| `--retry-pending-alerts` | **Berisiko duplikat:** hanya setelah cek BIMA |

Lihat seluruh opsi dan default aktual:

```bash
/home/fauzi/crawler-rebuild/venv/bin/python3 \
  /home/fauzi/crawler-rebuild/multi/crawler_multi.py --help
```

## 11. Keamanan dan kebijakan Git

Repository harus **private**. Minimal, tambahkan hal berikut ke `.gitignore` dan periksa semua commit sebelum push:

```gitignore
venv/
__pycache__/
*.py[cod]
.env
.env.*
/config.json
/urls.json
multi/state/
*.sqlite3
*.sqlite3-wal
*.sqlite3-shm
*.log
logs/
.cache/
*.safetensors
*.pkl
*.pt
*.pth
*.key
*.pem
```

- Jangan commit API key BIMA, endpoint rahasia, token Telegram, URL sensitif, database, screenshot, atau model proprietary tanpa persetujuan.
- Perlakukan semua input dan respons web sebagai data tak tepercaya; pertahankan pemeriksaan SSRF dan kebijakan HTTP(S) publik.
- Batasi outbound browser dan crawler sesuai kebutuhan jaringan organisasi. Jangan jalankan browser sebagai root.
- Jangan mengubah `pending_alert` menjadi `pending` secara massal tanpa pemeriksaan kemungkinan duplikat BIMA.
- Jika berkas rahasia **sudah pernah masuk riwayat Git**, `.gitignore` tidak menghapusnya dari history; lakukan rotasi kredensial dan penanganan history yang sesuai.

### Commit README ke Git

Setelah file ini disalin ke direktori repository:

```bash
cd /home/fauzi/crawler-rebuild
git add README.md
git diff --cached --check
git commit -m "docs: add crawler configuration and deployment guide"
git push -u origin HEAD
```

---

**Catatan versi:** Dokumentasi ini mengikuti implementasi multi-worker v1.2.4.3 yang diperiksa pada 20 November 2026, termasuk parameter CLI, SQLite WAL, Chrome managed mode, dan pengiriman screenshot dalam memori. Contoh `config.json` dan systemd harus disesuaikan dengan infrastruktur serta kebijakan produksi PANDI sebelum diaktifkan.
# bima
