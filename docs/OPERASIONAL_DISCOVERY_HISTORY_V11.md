# BIMA Discovery History v11 — jejak audit permanen

**Tujuan:** simpan asal kandidat Discovery dan hasil pemeriksaan di database BIMA, meskipun `discovery_job_priority` dibersihkan oleh import berikutnya. Tidak mengubah antrean Registry, seleksi priority, IndoBERT, sumber Google/urlscan, Telegram, atau jumlah URL dataset.

## Mengapa perlu

Pada v10 `discovery_job_priority` adalah metadata **antrean aktif**, bukan arsip. `cleanup_finished_priorities()` menghapus metadata pekerjaan yang sudah selesai pada saat import berikutnya. Hasil utama tetap ada di `jobs`, tetapi sumber Discovery (`google`/`urlscan`), waktu ditemukan, dan keterkaitan historis dapat hilang. Ketika URL sama dimasukkan ulang oleh Registry Snapshot, status/hasil terkini pada `jobs` juga dapat berubah.

## Desain (aditif)

- `discovery_history_sources_v11`: satu catatan per `job_id`; URL, sumber pertama/terakhir, waktu pertama/terakhir ditemukan. Tidak pernah dihapus otomatis.
- `discovery_history_outcomes_v11`: catatan per perubahan status yang bermakna, termasuk `risk`, `reason`, `attempts`, `bert_ms`, dan salinan `classifier_json`/path bukti pada saat pengamatan. Tidak pernah dihapus otomatis.
- Tiga trigger SQLite kecil: `INSERT discovery_job_priority` menyimpan provenance; `UPDATE OF status ON jobs` mencatat hasil *hanya bagi job yang saat itu berada di antrean Discovery*; `DELETE discovery_job_priority` membuat snapshot terakhir jika belum tercatat.
- Status transient `pending`, `http_inflight`, `browser_queued`, `retry_http` tidak dicatat sebagai hasil. `pending_alert` dicatat sebagai status ambigu, **bukan** bukti alert berhasil terkirim.
- `migrate` memasang tabel + trigger dan **backfill** semua metadata yang *masih ada* pada antrean Discovery saat migrasi, termasuk hasil terminal yang masih ada. Semua ini dalam **satu transaksi** (`BEGIN IMMEDIATE`).
- Tabel lama, status `jobs`, cursor, alert, hasil Registry Snapshot, model IndoBERT, dan symlink **tidak diubah**.
- `summary` dan `recent` **read-only**; tidak menjalankan crawler, API, alert, atau model.

**Batasan penting:** catatan Discovery yang sudah terhapus dari `discovery_job_priority` **sebelum** migrasi tidak bisa direkonstruksi secara pasti hanya dari `jobs`; migrasi tidak akan mengarang sumber penemuan atau mengklaim hasil yang tidak diketahui.

## 1. Patch dan uji di Mac

Ekstrak ZIP v11 atau gunakan patch dari paket. Jalankan dari repository Mac:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

git status -sb

git apply --check ~/Downloads/bima-discovery-history-v11.patch
git apply ~/Downloads/bima-discovery-history-v11.patch

PYTHONPATH=multi python3 -m unittest discover \
  -s multi/tests -p 'test_discovery_history_v11.py' -v

git diff --check

git add -- \
  multi/discovery_history_v11.py \
  tools/bima_discovery_history_v11.py \
  multi/tests/test_discovery_history_v11.py \
  docs/OPERASIONAL_DISCOVERY_HISTORY_V11.md

git diff --cached --check
git diff --cached --stat

git commit -m "feat: preserve BIMA Discovery provenance and outcomes"
git push origin main
```

Jangan gunakan `git add .` karena ada file lain di working tree Mac yang tidak terkait.

## 2. Deploy kode di server (belum migrasi)

```bash
REPO=/home/fauzi/crawler-rebuild
runuser -u fauzi -- git -C "$REPO" status -sb
runuser -u fauzi -- git -C "$REPO" pull --ff-only origin main

runuser -u fauzi -- env PYTHONPATH="$REPO/multi" \
  "$REPO/venv/bin/python3" -B -m unittest discover \
  -s "$REPO/multi/tests" -p 'test_discovery_history_v11.py' -v

# Preflight READ ONLY: "installed": false sebelum migrasi.
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/tools/bima_discovery_history_v11.py" summary
```

Tidak perlu `systemctl restart`, Registry Rollover, maupun perubahan cron.

## 3. **Backup baru sebelum perubahan schema**

Di `registrar-sandbox` sebagai root:

```bash
REPO=/home/fauzi/crawler-rebuild
systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
findmnt -T /mnt/registrar-sandbox -o TARGET,SOURCE,FSTYPE

# Menggunakan backup online terverifikasi v9 (tidak menghentikan crawler).
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/tools/bima_resilience_v9.py" backup
```

Pastikan backup mengembalikan `ok: true`, `files` berisi SQLite produksi dan `urls.json`, dan `cycle` sama dengan snapshot aktif. Salin **persis** `backup_path` yang dilaporkan; verifikasi terhadap path tersebut:

```bash
# Contoh: ganti hanya dengan "backup_path" nyata dari output.
BACKUP_PATH=/mnt/registrar-sandbox/bima/backups/verified-sqlite/ISI_BACKUP_PATH_DARI_OUTPUT
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/tools/bima_resilience_v9.py" verify \
  --backup "$BACKUP_PATH" --full
```

**Jangan lanjut migrasi** kalau backup tidak valid, NFS tidak terpasang, service tidak sehat, atau sudah ada pekerjaan rollover/restore lain yang sedang berjalan.

## 4. Migrasi satu kali — menulis schema secara aditif

Setelah backup diverifikasi, jalankan:

```bash
REPO=/home/fauzi/crawler-rebuild
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/tools/bima_discovery_history_v11.py" migrate \
  --acknowledge-production-write
```

Proses membuat dua tabel dan tiga trigger baru, lalu menyimpan metadata antrean Discovery saat itu. Ia meminta SQLite **write lock singkat**; kalau sedang sibuk dapat gagal tanpa migrasi parsial dan aman untuk dicoba lagi nanti. SQLite WAL ditolak, karena produksi pada NFS saat ini memakai `DELETE`.

Verifikasi secara **read-only**:

```bash
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/tools/bima_discovery_history_v11.py" summary

runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/tools/bima_discovery_history_v11.py" recent --limit 10

systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
```

Sukses jika `installed=true`, `triggers_ready=true`, `missing_source_from_priority=0`, dan kedua service aktif. Saat laporan sebelumnya ada 43 entri prioritas (11 complete, 4 inconclusive, 28 belum terminal); angka pada waktu migrasi mungkin berubah.

`recent` mengembalikan ID pekerjaan, sumber, status yang **disimpan**, status saat ini, risiko, alasan, dan waktu; tidak mencetak URL, path bukti, atau isi classifier JSON. Untuk ID tertentu:

```bash
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/tools/bima_discovery_history_v11.py" recent --job-id 123 --limit 50
```

Setelah migrasi dan validasi, buat **backup terverifikasi baru** dengan v9 agar skema riwayat juga tercakup dalam arsip terbaru.

## 5. Jika ada gangguan akibat trigger: nonaktifkan tanpa menghapus riwayat

```bash
REPO=/home/fauzi/crawler-rebuild
runuser -u fauzi -- "$REPO/venv/bin/python3" -B \
  "$REPO/tools/bima_discovery_history_v11.py" disable-triggers \
  --acknowledge-production-write
```

Ini menghapus **hanya tiga trigger v11**, bukan tabel riwayat, database, status pekerjaan, atau antrean. Apabila masalah selesai, `migrate --acknowledge-production-write` akan memasang ulang trigger dan melakukan backfill hasil yang *masih* bisa dilihat dari antrean aktif. Perubahan status yang berlangsung saat trigger dinonaktifkan mungkin tidak sepenuhnya dapat dipulihkan jika metadata antrean sudah dihapus.

Jangan melakukan `DROP TABLE`, mengganti file SQLite, `git reset --hard`, atau mengubah symlink NFS untuk rollback v11.

## 6. Catatan operasional

- v11 merupakan riwayat klasifikasi ketika URL berada dalam antrean Discovery. Tidak semua perubahan selanjutnya pada jalur Registry yang berbeda diklaim sebagai hasil Discovery.
- Riwayat disimpan di SQLite yang sama dengan produksi. Backup v9 **harus tetap aktif**; v11 bukan backup maupun pengganti disaster recovery.
- Retensi data riwayat bersifat permanen/tanpa pembersihan otomatis. Pantau pertumbuhan file DB dan kebijakan retensi serta akses karena `classifier_json` dan bukti bisa mengandung data sensitif.
- v11 tidak mengubah batas 0/unlimited yang sudah dipasang di v10, tidak mengubah interval cron dua hari, dan tidak mengubah mekanisme deduplikasi, prioritas Telegram/Registry/Discovery, atau penggunaan IndoBERT.
