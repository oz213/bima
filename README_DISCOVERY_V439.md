# BIMA v4.3.9 — Pemeriksaan kesehatan Discovery (read-only)

**Tujuan:** memastikan cron otomatis v4.3.8 benar-benar berjalan, log impor sehat, jumlah job Discovery sesuai batas, dan data sensitif tidak tercetak. Modul ini **hanya membaca** crontab pengguna, file log privat, metadata SQLite produksi (`mode=ro` + `query_only=ON`), dan direktori spool.

**Tidak mengubah alur crawler.** Tidak ada permintaan Google/Brave/urlscan API, penambahan job, migrasi database, pengiriman Telegram/BIMA, pemanggilan IndoBERT, restart service, atau perubahan cron. Brave tetap dinonaktifkan pada scheduler, dan URL hasil Brave tidak boleh diimpor tanpa izin penggunaan yang sesuai.

## Prasyarat yang sudah terverifikasi

- BIMA v4.3.8 commit `31f6cce` telah berhasil `git pull`; **421 dari 421 unit test lulus** di server pada 21 November 2026.
- Cron pengguna `fauzi` memuat **satu** baris: `30 20 * * * BIMA_DISCOVERY_AUTO_IMPORT=1 /home/fauzi/crawler-rebuild/tools/run_multisource_daily.sh`.
- Kedua service `pandi-crawler-multi` dan `pandi-telegram-monitor` aktif; IndoBERT telah dimuat sekali pada proses crawler v4.3.7.
- Dua canary Google (`7737184`, `7737185`) berstatus `complete`, masing-masing satu percobaan. Satu `pending_alert` lama tetap ada.
- Saat cron dikonfirmasi pada 21 November 2026 sebelum jam 20:30 UTC, file `multi-source-import.jsonl` dan spool belum ada; **normal sebelum eksekusi pertama**.

## Pengembangan

File baru:

- `multi/discovery_health.py` — CLI pemantauan read-only dengan output satu JSON, nilai agregat saja.
- `multi/tests/test_discovery_health_v439.py` — tes tanpa request jaringan dan tanpa penulisan SQLite produksi.
- `README_DISCOVERY_V439.md` — panduan ini.

Pemeriksaan yang dilakukan:

1. **Cron**: tepat satu baris scheduler pukul 20:30 UTC, auto-import aktif dan Brave daily tidak diaktifkan (termasuk variabel global crontab).
2. **Terakhir ditemukan**: status, jumlah request, kandidat dan status provider dari `multi-source.jsonl`; tidak menampilkan URL, judul, snippet atau pesan error mentah.
3. **Terakhir diimpor**: `ok`, `new_jobs <= 5`, `priority_added`, dan `brave_ingestion_enabled == false` dari `multi-source-import.jsonl`.
4. **Spool**: jumlah pending dan failed, ukuran total, serta mode akses direktori dan file. Tidak membaca atau menampilkan URL kandidat dari spool.
5. **SQLite**: status antrean penting (`pending`, `retry_http`, `pending_alert`, `http_inflight`, `browser_queued`) dan agregat status per provider di `discovery_job_priority`. Batas 20 `pending` diperiksa.
6. **Kesegaran jadwal**: mendeteksi hasil lama atau hilangnya impor setelah jadwal UTC jika tanggal aktivasi cron diberikan.

## Penerapan Mac → GitHub → server

Di Mac pada repo yang sudah di-update ke v4.3.8:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git apply --check ~/Downloads/bima-v4.3.8-to-v4.3.9-discovery-health.patch && \
  git apply ~/Downloads/bima-v4.3.8-to-v4.3.9-discovery-health.patch
PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -q
git diff --check
git add -- README_DISCOVERY_V439.md multi/discovery_health.py \
  multi/tests/test_discovery_health_v439.py
git diff --cached --check
git commit -m "feat: add read-only health audit for daily discovery"
git push origin main
```

Di server:

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover -s multi/tests -q
```

Jangan mengubah ` M crawler_google.py` dan `?? domains.json`. **Tidak perlu restart** crawler, Telegram, atau cron. Tidak perlu mengedit source pada server.

## Pemeriksaan harian setelah instalasi

Jalankan sebagai `root` (script Python sendiri berjalan sebagai user `fauzi`):

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B multi/discovery_health.py \
  --db multi/state/crawler_production.sqlite3 \
  --log-dir /home/fauzi/bima-logs \
  --check-cron \
  --enabled-date-utc 2026-11-21 \
  | ./venv/bin/python3 -m json.tool
```

`--enabled-date-utc 2026-11-21` berarti **tanggal UTC aktivasi cron otomatis**, bukan tanggal kalender WIB berikutnya. Batas jadwal 20:30 UTC (03:30 WIB keesokan hari). Modul memberi kelonggaran satu jam untuk eksekusi pertama sebelum menandai `scheduled_import_not_observed`.

**Interpretasi status**:

- `awaiting_first_run`: cron dikonfirmasi tetapi hasil impor belum ada; normal sebelum jadwal pertama.
- `ok`: tersedia hasil impor terbaru yang memenuhi batas dan tidak ada masalah yang terdeteksi. Ini **bukan jaminan** situs telah berhasil diklasifikasikan dan bukan bukti tidak ada alert.
- `attention`: ada masalah yang perlu diperiksa pada `problems`, misalnya cron ganda, impor gagal, hasil terlalu lama, impor Brave tidak diharapkan, database tidak dapat dibaca, spool tidak aman, atau jumlah job baru di atas 5.

Flag `--strict` opsional membuat exit code 1 jika `status=attention`, tetapi tidak menjalankan tindakan perbaikan atau mengirim alert.

**Penting:** pemeriksaan ini tidak mengirim notifikasi Telegram. Jika nantinya dibutuhkan alarm otomatis, tambahkan mekanisme terpisah yang hanya mengirim metrik agregat (tanpa URL dan API key), setelah pengujian operasional.

## Tindakan jika `attention`

Lihat `problems`, lalu baca file log privat melalui user `fauzi`; jangan mengirim seluruh JSONL karena mengandung URL hasil Google. Jangan jalankan scheduler secara manual untuk mengejar hasil tertinggal: skrip dapat memanggil API dan menambah job produksi. Jangan hapus marker `.started`, spool, state URL, database, `domains.json`, atau ledger Brave. Pertahankan pembatas 5 job baru per eksekusi terjadwal dan 20 job prioritas aktif hingga ada bukti performa yang memadai.

## Batasan

Tes hanya menggunakan fixture SQLite dan log sintetis. Health check tidak menjalankan Google API atau mengukur distribusi antrean live. Hasil sebenarnya baru dapat diverifikasi setelah cron pertama, yaitu **21 November 2026 pukul 20:30 UTC / 22 November 2026 pukul 03:30 WIB**, bila server tetap aktif dan API Google berhasil.
