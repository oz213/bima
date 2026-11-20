# PANDI Crawler Multi-Worker

**Versi aplikasi:** 1.2.4.3 (memory-only screenshot)  
**Terakhir diperbarui:** 20 November 2026  
**Platform referensi:** Ubuntu Linux / Python 3.11 / SQLite (WAL) / Google Chrome for Testing / systemd  
**Fungsi:** Pemantauan dan klasifikasi konten situs berdomain `.id`, termasuk indikasi judi online, dengan opsi pelaporan ke BIMA.  
**Repository tujuan:** `https://github.com/oz213/bima.git` (pastikan akses dan visibilitasnya sesuai kebijakan PANDI).

Dokumen ini mencakup **instalasi baru**, **konfigurasi**, **crawling mulai dari 0%**, **deploy systemd**, **Telegram Monitor interaktif**, **laporan Telegram otomatis setiap 5 menit**, **backup/rollback**, **Git**, dan **troubleshooting**. Semua path `/home/fauzi/crawler-rebuild` di bawah merupakan contoh deployment yang digunakan pada server `registrar-sandbox.pandi.id`; sesuaikan pada host lain.


> **PENTING — Produksi:** Crawler dapat mengirim alert nyata ke BIMA. Selalu gunakan `--dry-run` untuk pengujian dan **jangan menjalankan dua instance pada database atau antrean yang sama**. Saat migrasi dari proses lama, pastikan proses tersebut benar-benar berhenti sebelum menjalankan service baru. Database baru tidak memiliki riwayat deduplikasi BIMA.

## 0. Kondisi deployment referensi (20 November 2026)

Berikut adalah status yang **terakhir dikonfirmasi melalui log operator**, bukan hasil pemeriksaan real-time oleh README:

| Komponen | Temuan / kondisi terakhir |
|---|---|
| Crawler multi-worker | `pandi-crawler-multi.service` aktif; Python PID `2441702` pada 07:00 UTC |
| IndoBERT | Berhasil dimuat **satu kali** di CPU; `batch=2`, `torch_threads=2` |
| Unit test crawler | **51 dari 51** lulus setelah dependensi `dnspython` dan permission venv diperbaiki |
| Input | `urls.json` baru; operator memilih membuat SQLite dari nol, **bukan** memakai database recovery lama |
| Telegram interaktif | `pandi-telegram-monitor.service` aktif dengan PID `2446450` pada 07:29 UTC; `/status` berhasil diproses |
| Telegram berkala | Berkas `telegram_periodic.py` dan unit/timer telah **disiapkan sebagai paket terpisah**, **belum ada bukti aktivasi timer** |
| Watchdog lama | Sebelumnya nonaktif; unit lama masih dapat mengacu ke path yang dihapus, **jangan aktifkan tanpa revisi** |
| Cron legacy `run_crawler.sh` | Dinonaktifkan agar `crawler_google.py` tidak memulai instance kedua |

> **Perbedaan penting:** Telegram Monitor **interaktif** menjawab `/status`, `/now`, dan sebagainya. Laporan **berkala setiap 5 menit** memerlukan `telegram_periodic.py` + `pandi-telegram-periodic.service` + `pandi-telegram-periodic.timer`, atau mekanisme watchdog yang memang dikonfigurasi untuk laporan. Menjalankan bot interaktif saja **tidak** membuat laporan terjadwal.

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
Monitoring SQLite / journald
       |
       +--> Telegram interaktif (GET polling; /status, /now, /recent, ...)
       |
       +--> Timer 5-menit (oneshot read-only; kirim ringkasan)
       |
       +--> Watchdog opsional (status proses, jika dikonfigurasi)
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
│   ├── telegram_monitor.py       # Bot interaktif, read-only
│   ├── telegram_periodic.py      # Pengirim ringkasan tiap 5 menit (paket terpisah)
│   ├── telegram_monitor.env      # Token/chat ID, rahasia; JANGAN commit
│   ├── crawler_watchdog.py       # Opsional; bukan komponen wajib
│   ├── tests/
│   └── state/                   # SQLite/WAL/SHM runtime, JANGAN commit
└── venv/                        # Virtual environment lokal, JANGAN commit

# Unit systemd terpasang di /etc/systemd/system/ (bukan di root repo):
# pandi-crawler-multi.service
# pandi-telegram-monitor.service
# pandi-telegram-periodic.service (oneshot)
# pandi-telegram-periodic.timer   (setiap 5 menit)
```

**Catatan repositori:** `telegram_periodic.py` dan dua berkas unit/timer berasal dari paket tambahan `pandi-telegram-periodic.zip`. Tambahkan ketiga berkas itu ke repository hanya setelah ditinjau dan diuji; service/timer pada `/etc/systemd/system` tidak ikut masuk Git secara otomatis.

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

**Izin akses:** Install dependensi ke `venv` menggunakan user `fauzi`, bukan `root`. Pada pemulihan November 2026 pernah terjadi `ModuleNotFoundError: dns.exception` dan kegagalan import `torch`/`transformers` karena berkas yang dibuat `root` tidak bisa dibaca oleh proses systemd yang berjalan sebagai `fauzi`. `importlib.util.find_spec()` **tidak cukup**: uji `import` sebenarnya dengan identitas user service.

```bash
APP=/home/fauzi/crawler-rebuild
runuser -u fauzi -- "$APP/venv/bin/python3" -c   'import dns.exception, torch; from transformers import AutoTokenizer, AutoModelForSequenceClassification; print("Runtime import OK")'
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

### 8.1 Service yang sudah terpasang: periksa drop-in dan perbedaan opsi

Saat pemulihan November 2026, server menggunakan unit:

- `/etc/systemd/system/pandi-crawler-multi.service`
- `/etc/systemd/system/pandi-crawler-multi.service.d/10-base-dir.conf`
- `/etc/systemd/system/pandi-crawler-multi.service.d/20-fresh-start.conf`

Drop-in `10-base-dir.conf` menentukan `PANDI_CRAWLER_BASE_DIR=/home/fauzi/crawler-rebuild`. Drop-in `20-fresh-start.conf` menghapus syarat keberadaan file SQLite sehingga database baru bisa dibuat. **Jangan menjalankan ulang perintah pembuatan unit tanpa memeriksa konfigurasi yang sudah aktif.**

```bash
sudo systemctl cat pandi-crawler-multi.service
sudo systemctl show pandi-crawler-multi.service -p ExecStart -p Environment
sudo systemctl status pandi-crawler-multi.service --no-pager -l
```

**Perhatikan:** `ExecStart` yang dikonfirmasi operator pada 20 November 2026 **belum memakai** `--retry-network`. Contoh unit rekomendasi di atas sudah menyertakan flag itu untuk *run berikutnya*. Mengubah `ExecStart` memerlukan *restart*, sehingga **jangan ubah/restart service produksi tanpa jadwal dan pemeriksaan status serta backup**.

### 8.2 Penting: retry jaringan bekerja saat awal run

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

### 9.1 Telegram Monitor interaktif (`pandi-telegram-monitor.service`)

Modul `multi/telegram_monitor.py` berjalan terus-menerus, melakukan **GET polling** ke Telegram Bot API dan membaca database SQLite **read-only**. Ia tidak mengubah status job dan tidak mengirim alert BIMA. **Jangan menjalankan dua proses monitor polling memakai token bot yang sama** karena `getUpdates` dapat saling berebut pembaruan.

Perintah yang didukung source yang dipulihkan:

| Perintah bot | Kegunaan |
|---|---|
| `/start` atau `/id` | Menampilkan Chat ID untuk pendaftaran izin |
| `/help` | Bantuan |
| `/status` | Total URL, sudah dicoba, selesai, retry, alert, antrean |
| `/now` | HTTP `inflight` dan browser yang mengantre/diproses |
| `/recent` atau `/recent 10` | Hingga 10 hasil tersimpan terbaru |
| `/bima` | Jumlah `alerted`, `pending_alert`, `dry_run_alert` |
| `/dns` | Rangkuman alasan retry DNS/HTTP |
| `/inconclusive` | Alasan URL yang belum dapat disimpulkan |

**Berkas rahasia**: `/home/fauzi/crawler-rebuild/multi/telegram_monitor.env`. Gunakan token bot lama yang sah dan Chat ID Telegram yang diizinkan. **Jangan mengirim token ke chat atau meng-commit file ini ke Git.**

```ini
# CONTOH saja: jangan tulis nilai asli di README/Git
TELEGRAM_BOT_TOKEN=GANTI_DENGAN_TOKEN_RAHASIA
TELEGRAM_ALLOWED_CHAT_IDS=GANTI_DENGAN_CHAT_ID
CRAWLER_DB_PATH=/home/fauzi/crawler-rebuild/multi/state/crawler_production.sqlite3
TELEGRAM_OFFSET_PATH=/home/fauzi/crawler-rebuild/multi/state/.telegram_monitor_offset
TELEGRAM_POLL_SECONDS=5
```

```bash
APP=/home/fauzi/crawler-rebuild
# Pada host baru, buat file kosong terlebih dahulu; isi lewat editor/secret deployment.
sudo install -d -o fauzi -g fauzi -m 750 "$APP/multi/state"
sudo test -f "$APP/multi/telegram_monitor.env" || \
  sudo install -o fauzi -g fauzi -m 600 /dev/null "$APP/multi/telegram_monitor.env"
sudo chown fauzi:fauzi "$APP/multi/telegram_monitor.env"
sudo chmod 600 "$APP/multi/telegram_monitor.env"
# Isi menggunakan: sudo nano "$APP/multi/telegram_monitor.env"
```

**Instalasi service baru (jangan menimpa unit yang sudah berfungsi):**

```ini
# /etc/systemd/system/pandi-telegram-monitor.service
[Unit]
Description=PANDI Crawler Telegram Monitor (read-only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=fauzi
Group=fauzi
WorkingDirectory=/home/fauzi/crawler-rebuild/multi
EnvironmentFile=/home/fauzi/crawler-rebuild/multi/telegram_monitor.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/fauzi/crawler-rebuild/venv/bin/python3 /home/fauzi/crawler-rebuild/multi/telegram_monitor.py
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/fauzi/crawler-rebuild/multi/state
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
```

```bash
# Periksa proses sebelum start; jangan gandakan bot yang sedang polling.
pgrep -af '[t]elegram_monitor.py' || true
sudo systemctl cat pandi-telegram-monitor.service
sudo systemd-analyze verify pandi-telegram-monitor.service
sudo systemctl status pandi-telegram-monitor.service --no-pager -l
sudo journalctl -u pandi-telegram-monitor.service -n 30 --no-pager
```

Jika **baru** dibuat atau sudah dimigrasikan dan belum aktif, `sudo systemctl start pandi-telegram-monitor.service`, kirim `/status`, lalu `sudo systemctl enable pandi-telegram-monitor.service`. Untuk unit yang **sudah aktif**, `systemctl start` tidak mereload program atau mengganti PID; perubahan unit/env memerlukan `daemon-reload` dan restart monitor **saja** (tidak perlu restart crawler utama).

**Migrasi dari unit lama yang menunjuk `/home/fauzi/crawler/`:**

- Periksa `systemctl cat pandi-telegram-monitor.service` dan semua berkas dalam `.service.d/`.
- Perbaiki `WorkingDirectory`, `ExecStart`, `EnvironmentFile`, `CRAWLER_DB_PATH`, dan `TELEGRAM_OFFSET_PATH` agar mengarah ke `crawler-rebuild`.
- Jika service lama memiliki `ReadWritePaths=/home/fauzi/crawler/multi/state`, systemd bisa gagal dengan `status=226/NAMESPACE` **sebelum Python berjalan**. Hapus daftar `ReadWritePaths` lama lalu set `ReadWritePaths=/home/fauzi/crawler-rebuild/multi/state` melalui drop-in. Untuk direktif sandbox lain (`BindPaths`, `ReadOnlyPaths`, `BindReadOnlyPaths`) sesuaikan direktif yang benar; jangan menonaktifkan seluruh isolasi begitu saja.
- Pastikan file offset di database baru dapat ditulis oleh `fauzi`. Jika tidak ada, monitor dapat mulai dari `offset=0` sehingga pemrosesan perintah Telegram lama bisa terulang. Periksa perubahan offset setelah bot merespons.

Verifikasi sukses yang telah terlihat dalam log operator: `PANDI monitoring bot aktif (hanya baca)`, `Allowed chats: 1`, dan `Telegram command handled: /status`.

### 9.2 Laporan Telegram **otomatis setiap 5 menit** (`pandi-telegram-periodic.timer`)

**Ini fitur tambahan yang berbeda dari bot interaktif.** Paket `pandi-telegram-periodic.zip` berisi:

```text
telegram_periodic.py               # Satu kali kirim ringkasan, tanpa polling
pandi-telegram-periodic.service    # systemd Type=oneshot
pandi-telegram-periodic.timer      # systemd OnCalendar=*:0/5
```

Alurnya: timer setiap 5 menit menjalankan service sekali → service membaca tabel `jobs` lewat SQLite `mode=ro` → membentuk ringkasan → mengirim Telegram ke setiap Chat ID yang terdaftar → menyimpan penghitung sesi sebelumnya di `multi/state/.telegram_periodic_state.json`. Tidak ada akses tulis ke tabel crawler dan tidak ada request ke BIMA.

**Catatan metrik:** Keterangan `URL diproses/non-pending` pada versi paket ini dihitung sebagai **total URL dikurangi `pending`**. Nilai tersebut *bukan* jumlah URL yang sudah pernah dicoba berdasarkan `attempts`, dan delta dapat berubah jika pekerjaan dikembalikan ke `pending`. Oleh karena itu laporan berkala dan `/status` interaktif mungkin menampilkan angka percobaan yang berbeda.

**Langkah A — pasang modul dan unit** dari ZIP yang telah diunggah ke server, sebagai root:

```bash
set -e
APP=/home/fauzi/crawler-rebuild
ZIP=/tmp/pandi-telegram-periodic.zip
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT

test -f "$ZIP"
unzip -q "$ZIP" -d "$TMP"

install -o fauzi -g fauzi -m 750 \
  "$TMP/telegram_periodic.py" "$APP/multi/telegram_periodic.py"
install -o root -g root -m 644 \
  "$TMP/pandi-telegram-periodic.service" /etc/systemd/system/
install -o root -g root -m 644 \
  "$TMP/pandi-telegram-periodic.timer" /etc/systemd/system/

systemctl daemon-reload
systemd-analyze verify pandi-telegram-periodic.service pandi-telegram-periodic.timer
systemctl cat pandi-telegram-periodic.service
```

Paket saat ini menggunakan `EnvironmentFile=/home/fauzi/crawler-rebuild/multi/telegram_monitor.env` dan `CRAWLER_DB_PATH=/home/fauzi/crawler-rebuild/multi/state/crawler_production.sqlite3`. Token dan chat ID **tidak perlu dibuat ulang**, cukup dibaca oleh unit oneshot dari file rahasia yang sama.

**Langkah B — preview tanpa mengirim pesan**:

```bash
APP=/home/fauzi/crawler-rebuild
runuser -u fauzi -- env \
  CRAWLER_DB_PATH="$APP/multi/state/crawler_production.sqlite3" \
  "$APP/venv/bin/python3" "$APP/multi/telegram_periodic.py" --preview
```

Periksa total URL, status, URL yang dicantumkan, dan kesesuaian dengan `/status`. **Perhatian privasi:** dalam versi paket saat ini, teks tautan laporan hanya menampilkan host/path pendek, tetapi `href` hyperlink memakai URL sumber lengkap. Jika input berisi query string atau token rahasia, **jangan aktifkan kiriman otomatis sebelum sumber URL disanitasi atau implementasi `display_url()` diperbaiki**.

**Langkah C — kirim satu laporan untuk uji jaringan**, hanya setelah preview aman:

```bash
sudo systemctl start pandi-telegram-periodic.service
sudo systemctl status pandi-telegram-periodic.service --no-pager -l
sudo journalctl -u pandi-telegram-periodic.service -n 30 --no-pager
```

`Type=oneshot` normalnya akan berakhir menjadi `inactive (dead)` dengan `Result=success`; **bukan** `active (running)` terus-menerus. Pastikan satu pesan benar-benar masuk Telegram.

**Kompatibilitas jaringan penting:** Bot interaktif memakai HTTPS **GET** ke Telegram, tetapi `telegram_periodic.py` dari ZIP ini menggunakan HTTPS **POST** ke `sendMessage`. Jika kebijakan egress/proxy server mengizinkan GET tetapi menolak POST, bot `/status` bisa berhasil sementara laporan berkala gagal. Periksa akses POST atau revisi metode kirim sesuai kebijakan jaringan; **jangan menganggap integrasi periodik sudah lulus hanya karena preview atau bot interaktif berhasil**.

**Langkah D — aktifkan timer setelah uji pengiriman sukses**:

```bash
sudo systemctl enable --now pandi-telegram-periodic.timer
sudo systemctl list-timers --all | grep pandi-telegram-periodic
sudo systemctl is-enabled pandi-telegram-periodic.timer
sudo journalctl -u pandi-telegram-periodic.service -n 30 --no-pager
```

`OnCalendar=*:0/5`, `AccuracySec=15s`, dan `Persistent=false`: periode setiap menit ke-0, 5, 10, 15, dan seterusnya, mengikuti waktu kalender server; timer tidak melakukan *catch-up* untuk periode yang terlewat saat mesin mati. Lihat `list-timers` untuk waktu eksekusi berikutnya.

**Menghentikan hanya laporan berkala** (bot `/status` dan crawler tetap berjalan):

```bash
sudo systemctl disable --now pandi-telegram-periodic.timer
sudo systemctl is-active pandi-telegram-monitor.service
sudo systemctl is-active pandi-crawler-multi.service
```

**Hindari notifikasi ganda**: jangan sekaligus mengaktifkan laporan berkala dari watchdog lama dengan ringkasan yang sama. Gunakan satu sumber jadwal ringkasan atau nonaktifkan fitur ringkasan watchdog sebelum memasang timer ini.

### 9.3 Watchdog (opsional, jangan gunakan unit lama tanpa migrasi)

`multi/crawler_watchdog.py` dapat digunakan untuk memeriksa keberadaan proses crawler, mengirim peringatan berhenti/macet, dan pada versi tertentu mengirim ringkasan aktivitas. Namun, versi unit sebelum pemulihan masih memakai path `/home/fauzi/crawler`, sehingga `pandi-crawler-watchdog.timer` sengaja dipertahankan **nonaktif** sampai dikonfigurasi ulang.

Jika dibutuhkan, tinjau terlebih dahulu variabel `CRAWLER_DB_PATH`, `CRAWLER_SCRIPT_PATH`, `CRAWLER_WATCHDOG_STATE`, `CRAWLER_WATCHDOG_MAINTENANCE`, `CRAWLER_WATCHDOG_MISSES`, `CRAWLER_STALL_MINUTES`, dan `CRAWLER_ACTIVITY_SUMMARY_MINUTES`. Pastikan filter PID menunjuk crawler baru, direktori state memiliki izin tepat, dan unit tidak lagi mengakses lokasi lama. Pilih apakah ingin notifikasi watchdog dan ringkasan 5 menit berada dalam satu service atau terpisah. Tidak ada instruksi untuk mengaktifkan watchdog lama secara otomatis pada README ini.

### 9.4 Alat pemeriksaan operasional gabungan

```bash
# Status crawler dan Telegram tanpa mencetak token/chat ID:
systemctl is-active pandi-crawler-multi.service || true
systemctl is-active pandi-telegram-monitor.service || true
systemctl is-active pandi-telegram-periodic.timer || true
systemctl is-active pandi-crawler-watchdog.timer || true

# Hanya satu proses crawler dan satu proses bot interaktif:
pgrep -af '[c]rawler_multi.py' || true
pgrep -af '[t]elegram_monitor.py' || true

# Log sejak 10 menit terakhir:
journalctl -u pandi-crawler-multi.service --since '10 minutes ago' --no-pager
journalctl -u pandi-telegram-monitor.service --since '10 minutes ago' --no-pager
journalctl -u pandi-telegram-periodic.service --since '10 minutes ago' --no-pager

# Jadwal laporan berkala (jika sudah diaktifkan):
systemctl list-timers --all | grep pandi-telegram-periodic || true
```

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
/multi/telegram_monitor.env
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

## 12. Troubleshooting cepat

| Gejala | Pemeriksaan / tindakan yang benar |
|---|---|
| `FATAL: PyTorch or transformers is not installed; IndoBERT is mandatory` | Tes **import sungguhan sebagai `fauzi`** (`import torch; from transformers import ...`); periksa izin venv dan versi dependensi. **Jangan mematikan IndoBERT** sebagai workaround. |
| `ModuleNotFoundError: No module named dns.exception` saat service/test user `fauzi` | Periksa izin `site-packages/dns`, bukan hanya `pip check` sebagai root; pastikan `fauzi` dapat membaca package. |
| Model bisa ditemukan tetapi gagal load | Periksa `PANDI_CRAWLER_BASE_DIR`, file `model.safetensors`, `config.json`, vocabulary/tokenizer, dan hak baca user service. |
| `226/NAMESPACE` pada Telegram Monitor | Path sandbox `ReadWritePaths`, `ReadOnlyPaths`, atau bind mount masih menunjuk direktori lama. Gunakan `systemctl cat` dan perbaiki direktif yang tepat. |
| Bot `/status` balas `Database crawler belum bisa dibaca` | Pastikan `CRAWLER_DB_PATH` menunjuk SQLite baru, file tersedia, serta `fauzi` bisa membaca file/WAL dan direktori parent. |
| Bot interaktif aktif tapi tidak ada laporan setiap 5 menit | `pandi-telegram-periodic.timer` belum aktif, unit oneshot gagal, atau paket periodik belum terpasang. Cek `list-timers` dan `journalctl -u pandi-telegram-periodic.service`. |
| Bot interaktif berhasil tetapi laporan periodik gagal kirim | Bedakan metode API: bot interaktif **GET**, paket periodik **POST**; cek proxy/firewall egress dan response Telegram. |
| `FileNotFoundError` pada script lama atau watchdog | Cek masih ada path `/home/fauzi/crawler/` di source, env, unit, dan drop-in. |
| `Google Chrome 157` vs ChromeDriver berbeda | Jalankan `setup_chrome.py` sebagai `fauzi`; jangan instal cache sebagai root; browser mode `managed` menyimpan pasangan yang cocok. |
| Service crawler normal lalu `inactive (dead)` | Bisa berarti antrean run telah habis dan exit `0`; `Restart=on-failure` **tidak** memulai ulang. Tinjau strategi retry/timer crawler. |
| Banyak `retry_http` tetapi tidak diproses lagi | Pastikan startup berikutnya memakai `--retry-network` dan retry sudah jatuh tempo; flag tanpa run baru tidak serta-merta mengaktifkan retry. |
| Ada `pending_alert` BIMA | Respons belum terkonfirmasi; **jangan** jalankan `--retry-pending-alerts` sebelum rekonsiliasi BIMA. |
| `git push` ke GitHub gagal `Password authentication is not supported` | HTTPS memerlukan **Personal Access Token** dengan `Contents: Read and write` pada repo yang dipilih, atau SSH key. Jangan commit token. |
| Proses mati / mesin kehabisan RAM | Periksa `journalctl -k --since '15 minutes ago'`, `free -h`, OOM, dan proses Selenium; kurangi worker **setelah** investigasi. |

## 13. Prosedur pembaruan dan pemulihan singkat

### 13.1 Sebelum deployment versi baru

1. Catat versi Git, `ExecStart` aktif, seluruh drop-in, dan status daemon. Jangan menyimpan keluaran kredensial ke log publik.
2. Backup `config.json`/`telegram_monitor.env` secara privat dan backup SQLite secara **konsisten** (SQLite backup API atau writer berhenti); jangan menghapus WAL aktif.
3. Pastikan tidak ada crawler kedua, cron legacy, atau dua bot polling Telegram dengan token yang sama.
4. Jalankan tes offline dan verifikasi import PyTorch/Transformers **sebagai `fauzi`**. Validasi `urls.json` dan model.
5. Gunakan *maintenance window* bila mengubah kode crawler. Mengganti file source pada disk saja **tidak otomatis** memutakhirkan proses Python yang sedang berjalan.

```bash
APP=/home/fauzi/crawler-rebuild
cd "$APP"

git status --short
git log -1 --oneline
systemctl cat pandi-crawler-multi.service
systemctl cat pandi-telegram-monitor.service
systemctl list-timers --all | grep 'pandi-' || true
```

### 13.2 Perubahan README, Telegram periodik, dan source

- Perubahan **README saja** cukup `git add README.md`, commit, dan push; **tidak memerlukan restart crawler**.
- Perubahan `telegram_periodic.py` atau unit oneshot: cek preview dan uji sekali, lalu verifikasi timer; tidak perlu restart crawler utama.
- Perubahan `telegram_monitor.py`, env, atau unit bot interaktif: restart **hanya** `pandi-telegram-monitor.service` setelah path dan sandbox benar; crawler utama tidak perlu restart.
- Perubahan `crawler_multi.py`, `crawler_url.py`, model, atau parameter utama: jadwalkan penghentian terkontrol, backup database, lalu restart **hanya crawler**. Restart dapat memicu import URL baru, requeue `http_inflight`, dan (dengan opsi terkait) retry jaringan; evaluasi konsekuensi dulu.
- Jangan menggunakan `rm -rf` di direktori aplikasi, model, atau database sebagai bagian dari deploy biasa.

### 13.3 Mulai dari nol (hanya bila disengaja)

Untuk reset total: siapkan daftar URL baru, pastikan semua instance crawler berhenti, backup data yang diperlukan, dan pastikan tidak ada pembaca/penulis yang memakai file SQLite. Hanya setelah itu gunakan prosedur aman pada **Bagian 6** untuk menghapus tiga file SQLite staging (`.sqlite3`, `-wal`, `-shm`). Periksa riwayat alert BIMA sebelum mengulangi domain yang pernah diproses.

### 13.4 Checklist selesai deploy

- [ ] `pandi-crawler-multi.service` sehat dan proses tunggal.
- [ ] Log `IndoBERT loaded ONCE` muncul dan tidak ada error dependensi.
- [ ] Database baru berisi jumlah URL yang masuk akal; progres bergerak dan nilai status konsisten.
- [ ] Selenium/Chrome berfungsi pada situs uji yang diizinkan (bukan cuma unit test).
- [ ] Status `alerted`/`pending_alert` sudah dipahami; tidak ada duplikasi BIMA tidak disengaja.
- [ ] Telegram interaktif membalas `/status` dan `/now` dari database baru.
- [ ] Jika memilih ringkasan otomatis: preview aman, pesan percobaan berhasil, timer aktif dan jadwal berikutnya terlihat.
- [ ] Watchdog lama tidak aktif kecuali sudah dimigrasikan dan diuji.
- [ ] Tidak ada secret, database, venv, atau model proprietary yang ikut `git add`.

---

**Catatan versi:** Panduan ini mengikuti crawler multi-worker v1.2.4.3, Telegram Monitor interaktif yang berhasil dipulihkan pada 20 November 2026, dan paket laporan berkala 5 menit yang sudah disiapkan. **Keberhasilan pengiriman berkala ke Telegram belum dikonfirmasi dari log produksi**. Selalu verifikasi unit/timer, izin akses, endpoint, dan file yang benar pada setiap server sebelum mengaktifkan pengiriman nyata.
