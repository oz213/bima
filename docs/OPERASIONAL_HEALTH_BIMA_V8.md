# BIMA Fase 1A — Health monitor produksi (v8)

Tujuan: pemantauan **read-only** untuk `pandi-crawler-multi.service`,
`pandi-telegram-monitor.service`, proses crawler ganda, mount NFS, kapasitas penyimpanan, database
SQLite, snapshot aktif, dataset, backup rollover terkelola, dan keberadaan
berkas konfigurasi IndoBERT.

**Tidak** mengubah database, mengirim alert BIMA, melakukan backup,
me-restart service, mereset progres, atau mengganti lokasi SQLite.
Penulisan opsional `--tick` **hanya** ke `/var/lib/pandi-bima-health/state.json`
(local disk) untuk mendeteksi progres macet.

## Langkah deployment — selalu Mac → GitHub → server

1. Terapkan patch di repository Mac, uji: `python3 -m unittest discover -s tests -p 'test_bima_production_health_v8.py' -v`.
2. Commit/push file yang disebut pada patch; jangan `git add .` pada server.
3. Pull di server **tanpa menghentikan crawler** jika Git bersih.
4. Jalankan preflight read-only sebelum mengaktifkan timer:

   ```bash
   APP=/home/fauzi/crawler-rebuild
   runuser -u fauzi -- "$APP/venv/bin/python3" -B \
     "$APP/tools/bima_production_health_v8.py" --json
   ```

5. Lakukan uji dataset terhadap SHA snapshot sekali saja, ketika NFS tidak sibuk:

   ```bash
   runuser -u fauzi -- "$APP/venv/bin/python3" -B \
     "$APP/tools/bima_production_health_v8.py" --verify-input-sha --json
   ```

6. Pastikan JSON `status=healthy` dan `issues=[]`. Jika `degraded`, tinjau
   alasan dan sesuaikan path/threshold yang *sudah diverifikasi*. Jangan
   menyembunyikan kegagalan mount atau database dengan mengubah threshold.

## Aktifkan timer (opsional setelah preflight)

   ```bash
   APP=/home/fauzi/crawler-rebuild
   install -o root -g root -m 0644 "$APP/deploy/pandi-bima-health.service" /etc/systemd/system/
   install -o root -g root -m 0644 "$APP/deploy/pandi-bima-health.timer" /etc/systemd/system/
   systemctl daemon-reload
   systemd-analyze verify /etc/systemd/system/pandi-bima-health.service /etc/systemd/system/pandi-bima-health.timer
   systemctl enable --now pandi-bima-health.timer
   systemctl start pandi-bima-health.service || true
   journalctl -u pandi-bima-health.service -n 15 --no-pager
   systemctl list-timers pandi-bima-health.timer
   ```

Default **tidak mengirim Telegram**, hanya laporan JSON sanitasi ke journal.
Monitor tidak mengirim pesan jika proses berjalan sehat. Peringatan "stalled"
terjadi bila pending snapshot tidak berkurang selama 30 menit saat crawler
aktif, bukan bila total URL sudah habis.

## Aktifkan notifikasi Telegram (hanya setelah baseline sehat)

Jika `multi/telegram_monitor.env` berisi kredensial yang digunakan monitor
Telegram (jangan pernah commit/print), buat drop-in. Kredensial **tidak**
dibaca oleh health monitor sampai opt-in ini diaktifkan:

   ```bash
   mkdir -p /etc/systemd/system/pandi-bima-health.service.d
   cat > /etc/systemd/system/pandi-bima-health.service.d/20-telegram.conf <<'CONF'
   [Service]
   EnvironmentFile=/home/fauzi/crawler-rebuild/multi/telegram_monitor.env
   Environment=BIMA_HEALTH_NOTIFY=1
   CONF
   systemctl daemon-reload
   systemctl start pandi-bima-health.service || true
   ```

Peringatan dikirim **setelah dua tick bermasalah dengan kode masalah yang sama**;
pemulihan dikirim sekali. Data status notifikasi disimpan per-chat untuk
mengurangi pengiriman berulang apabila hanya satu chat gagal. Jika ada
watchdog Telegram lama yang masih aktif, audit dulu agar tidak menggandakan
notifikasi.

## Penjelasan pemeriksaan

- NFS diperiksa dengan `findmnt`. Jika mount tidak dikenali sebagai `nfs/nfs4`,
  proses tidak akan membuka DB/dataset, menghindari fallback ke disk root.
- SQLite dibuka dengan URI `mode=ro` dan `PRAGMA query_only=ON`; secara
  periodik hanya membaca metadata snapshot serta jumlah `pending` di queue.
  **Tidak** menjalankan `PRAGMA quick_check` pada database aktif di NFS.
- Inventory backup rollover memastikan **satu** backup terkelola dengan marker
  dan file database yang tidak kosong. Ini **bukan** uji integritas SQLite;
  `quick_check` backup dan latihan restore adalah pekerjaan Fase 1B.
- Jika lebih dari satu proses `crawler_multi.py` dengan argumen database produksi
  terdeteksi, beri peringatan. Angka `0` hanya berarti deteksi proses tidak
  memastikan PID; itu **bukan** bukti IndoBERT sedang dimuat/aktif.
- `journal_mode` harus `delete` pada setup NFS saat ini. Mode WAL ditandai
  bermasalah.
- `--verify-input-sha` membaca seluruh dataset untuk memastikan hash sesuai
  snapshot, dan **tidak** dinyalakan dalam timer 5 menit.
- File `config.json` model kandidat IndoBERT harus tersedia. Ini **bukan**
  bukti model benar-benar dimuat; verifikasi startup IndoBERT tetap melalui
  log service crawler.
- Kapasitas root/NFS default minimal 5 GiB, bisa disesuaikan sesuai kapasitas
  disk yang sebenarnya.
- Exit code `0`: healthy; `1`: degraded, termasuk kegagalan cek atau snapshot.
  Service oneshot mungkin tercatat `failed` saat gangguan; timer berikutnya tetap
  melakukan pemeriksaan.
- Pada outage NFS berat proses filesystem mungkin menggantung di kernel;
  `TimeoutStartSec` membatasi service normal tetapi tidak menjamin
  terminasi I/O yang berada di state uninterruptible.

## Menonaktifkan monitor

   ```bash
   systemctl disable --now pandi-bima-health.timer
   ```

Tidak perlu menghentikan crawler/Telegram. Jangan menghapus jurnal sistem atau
backup NFS. Pertahankan SQLite aktif di NFS untuk fase 1A; evaluasi opsi
migrasi ke disk lokal atau PostgreSQL secara terpisah dengan rencana rollback.
