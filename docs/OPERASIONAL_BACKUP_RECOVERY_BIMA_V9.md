# BIMA Fase 1B–1D — Backup, Storage Reliability, Disaster Recovery (v9)

**Basis produksi:** commit `10e3e92` + cleanup NFS v7. Berlaku dengan maupun tanpa patch
health monitor v8 untuk backup, storage audit, dan restore rehearsal. Untuk status
`readiness`, **health monitor v8 harus sudah terpasang** dan berjalan sehat.

## Perlindungan utama

- Kode v9 **tidak** menulis database produksi, tidak mengganti symlink NFS, tidak
  mengubah journal mode, tidak menghapus/mereset riwayat job dan alert, tidak
  memulai ulang crawler/Telegram, serta tidak menonaktifkan IndoBERT.
- Backup menggunakan SQLite **Online Backup API** (konsisten terhadap transaksi
  pada masing-masing SQLite ketika crawler masih berjalan), bukan `cp` atas
  file SQLite aktif. **Tidak ada atomic snapshot lintas dua database**;
  database learning disalin secara terpisah dari database produksi.
- Setiap backup dibuat pada direktori sementara `.partial-*` di **NFS**, lalu
  dipublikasikan sebagai `verified-*` setelah `quick_check`, hash SHA-256 dan
  SHA dataset cocok dengan snapshot aktif. Backup yang gagal tidak diterbitkan.
- *Restore rehearsal* membuat salinan ke direktori **lokal terisolasi**, melakukan
  SHA-256 dan `PRAGMA integrity_check` penuh terhadap hasil restore. Salinan
  sementara dihapus setelah sukses; sumber produksi tetap utuh.
- Jika mount NFS hilang, operasi backup/restore ditolak; tidak diam-diam menulis
  backup ke disk root. Tidak ada mekanisme rollback otomatis yang menimpa DB.
- Backup ini **terpisah dari**
  `/mnt/registrar-sandbox/bima/backups/registry-rollover/` yang menerapkan
  retensi tepat **satu backup rollover**. v9 tidak pernah menghapus folder
  rollover, `local-recovery`, `sqlite`, atau arsip manual.
- Default backup CLI tidak menghapus backup apa pun. Opsi `--retain 7` yang
  digunakan timer hanya membatasi arsip yang **dibuat dan dikenali oleh v9**;
  tanpa `--retain`, arsip v9 dibiarkan. Minimum retensi adalah 2.
- SQLite pada NFS tetap punya risiko penguncian/jaringan; v9 tidak boleh
  dianggap menghilangkan risiko tersebut. Migrasi disk lokal butuh cutover
  terpisah setelah service, snapshot, disk, dan rollback siap.

## Struktur yang diperiksa

| Item | Path |
|---|---|
| SQLite produksi | `/mnt/registrar-sandbox/bima/state/crawler_production.sqlite3` |
| Learning SQLite | `/mnt/registrar-sandbox/bima/state/bima_learning_v450.sqlite3` (opsional) |
| Dataset aktif | `/mnt/registrar-sandbox/bima/datasets/urls.json` |
| Arsip backup v9 | `/mnt/registrar-sandbox/bima/backups/verified-sqlite/` |
| Rekaman uji restore | `/var/lib/pandi-bima-health/last-rehearsal.json` (disk lokal) |
| Scratch restore | `/var/tmp/bima-restore-drill-*` (disk lokal, sementara) |

## 0. Preflight awal — tanpa menghentikan crawler

Jalankan sebagai root di server setelah file di-push dan `git pull` sukses.

```bash
APP=/home/fauzi/crawler-rebuild
NFS=/mnt/registrar-sandbox

findmnt -T "$NFS" -o TARGET,SOURCE,FSTYPE
systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
systemctl show pandi-crawler-multi.service -p ExecStart --value | grep -oE -- '--max-network-attempts[ =][0-9]+' || true

# Periksa dulu hak akses parent NFS. Jangan chown rekursif.
runuser -u fauzi -- mkdir -p "$NFS/bima/backups/verified-sqlite"
runuser -u fauzi -- chmod 700 "$NFS/bima/backups/verified-sqlite"

# State lokal khusus lock dan attestasi. Tidak pada NFS.
install -d -o fauzi -g fauzi -m 0700 /var/lib/pandi-bima-health

runuser -u fauzi -- "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_resilience_v9.py" storage-audit
```

Jika `storage-audit` menampilkan `status=blocked`, **jangan lanjut** ke backup,
rehearsal, maupun timer. `status=at_risk` dengan warning
`sqlite_on_nfs_locking_risk` adalah kondisi yang diharapkan selama DB aktif
masih di NFS. Pastikan jurnal database = `delete` dan bukan `wal`.

## 1B. Backup terverifikasi secara manual

```bash
APP=/home/fauzi/crawler-rebuild
runuser -u fauzi -- "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_resilience_v9.py" backup
```

Backup bisa berlangsung selama beberapa menit karena ukurannya bergantung DB
aktif, database learning, dan performa NFS. Perintah mengeluarkan JSON:
`ok`, `backup_path`, `cycle`, `total`, `integrity=quick`, `files`.
**Simpan nilai `backup_path` dari output**, jangan menebak folder ID.

Untuk memeriksa backup yang baru dibuat:

```bash
APP=/home/fauzi/crawler-rebuild
BACKUP=/mnt/registrar-sandbox/bima/backups/verified-sqlite/<id-dari-output>
runuser -u fauzi -- "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_resilience_v9.py" verify --backup "$BACKUP" --full
```

Perhatikan pemeriksaan `--full` dilakukan pada **file backup**, bukan pada
SQLite live di NFS.

## 1C. Audit strategi penyimpanan

```bash
APP=/home/fauzi/crawler-rebuild
runuser -u fauzi -- "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_resilience_v9.py" storage-audit
```

Output memuat `db_bytes`, kapasitas kosong NFS/root, jurnal SQLite,
visibilitas konfigurasi IndoBERT, dan ruang minimum lokal untuk rencana
cutover (`db_size x3 + 3 GiB`). Audit **tidak** mengubah storage.

Keputusan fase 1C:

1. **Sekarang:** DB tetap di NFS agar siklus 499.999 URL dan status terjaga,
   dengan backup v9 harian + drill dan health monitor v8.
2. **Target jangka menengah:** SQLite aktif di SSD/NVMe lokal *hanya jika*
   seluruh konsumen DB (crawler, Telegram, learning, snapshot, watchdog,
   discovery, rollover, backup) sudah diinventarisasi dan disesuaikan.
3. **Sebelum cutover:** siapkan disk lokal, preflight hash dan backup konsisten,
   service dihentikan pada maintenance terencana, DB dipindahkan melalui
   staging terverifikasi, service dijalankan, lalu uji progres dan alert;
   siapkan prosedur rollback yang diuji. Jangan `mv` symlink `multi/state`
   dalam skrip otomatis ini.
4. **Jika kemudian multi-host:** evaluasi PostgreSQL, bukan beberapa writer
   SQLite ke satu file NFS.

## 1D. Latihan restore terisolasi, tanpa menyentuh DB produksi

```bash
APP=/home/fauzi/crawler-rebuild
runuser -u fauzi -- "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_resilience_v9.py" rehearse
```

`rehearse` otomatis memilih backup v9 terbaru. Ia menyalin ke scratch disk
lokal, menguji `PRAGMA integrity_check` penuh pada kedua database, menegaskan
jumlah Registry Snapshot, dan memvalidasi dataset SHA-256. Setelah sukses,
attestasi terakhir dicatat pada
`/var/lib/pandi-bima-health/last-rehearsal.json`.

Untuk menguji pemulihan ke direktori baru yang bisa diinspeksi manual:

```bash
APP=/home/fauzi/crawler-rebuild
BACKUP=/mnt/registrar-sandbox/bima/backups/verified-sqlite/<id-dari-output>
DEST=/var/tmp/bima-drill-inspection-$(date -u +%Y%m%dT%H%M%SZ)
runuser -u fauzi -- "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_resilience_v9.py" restore-copy \
  --backup "$BACKUP" --destination "$DEST"
```

`restore-copy` **menolak destination yang sudah ada**, lokasi di dalam
repository atau NFS, dan filesystem NFS lainnya. Data hasil latihan adalah
salinan sensitif; hapus secara **manual dan terkontrol** setelah pemeriksaan.
Jangan pernah menunjuk `--destination` ke path database produksi.

## Readiness keseluruhan

Pastikan health monitor v8 ada (jika belum, terapkan patch Fase 1A terlebih dahulu).

```bash
APP=/home/fauzi/crawler-rebuild
runuser -u fauzi -- "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_production_health_v8.py" --json
runuser -u fauzi -- "$APP/venv/bin/python3" -B \
  "$APP/tools/bima_resilience_v9.py" readiness
```

Keluaran yang diharapkan: `status=ready_with_nfs_risk`, dengan **tidak ada
`failures`**. Ini berarti backup, rehearsal, dan health cukup baik untuk
operasi bersyarat; **bukan** berarti risiko SQLite NFS sudah selesai.

Default readiness:
- health v8 harus `healthy`;
- backup v9 terbaru berumur paling lama 36 jam, SHA dan `quick_check` valid;
- drill pemulihan penuh terakhir paling lama 8 hari;
- NFS terpasang, journal SQLite `delete`, dan konfigurasi model terlihat.

## Timer terpisah (setelah manual backup + rehearsal sukses)

```bash
APP=/home/fauzi/crawler-rebuild
for name in backup rehearsal readiness; do
  install -o root -g root -m 0644 "$APP/deploy/pandi-bima-$name.service" /etc/systemd/system/
  install -o root -g root -m 0644 "$APP/deploy/pandi-bima-$name.timer" /etc/systemd/system/
done
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/pandi-bima-{backup,rehearsal,readiness}.{service,timer}

# Ketiga timer ini terpisah, dan TIDAK me-restart crawler.
systemctl enable --now pandi-bima-backup.timer \
  pandi-bima-rehearsal.timer pandi-bima-readiness.timer
systemctl list-timers 'pandi-bima-*'
```

Jadwal dalam UTC agar sama meskipun timezone server berubah:
- Backup harian 19:20 UTC = **02:20 WIB** (hari berikutnya).
- Rehearsal tiap Minggu 20:30 UTC = **Senin 03:30 WIB**.
- Readiness harian 21:00 UTC = **04:00 WIB** (hari berikutnya).

Jika ada backup rollover bersamaan, backup v9 menolak snapshot yang
sudah berubah/mismatch. Timer dapat dijalankan kembali setelah rollover selesai.

Untuk membaca laporan tanpa menampilkan kredensial:

```bash
journalctl -u pandi-bima-backup.service -n 15 --no-pager
journalctl -u pandi-bima-rehearsal.service -n 15 --no-pager
journalctl -u pandi-bima-readiness.service -n 15 --no-pager
```

## Jika terjadi insiden sebenarnya

1. Hentikan proses penulisan hanya dalam prosedur pemulihan resmi, bukan
   ketika sekadar menjalankan drill. Dokumentasikan siklus dan waktu gagal.
2. Verifikasi backup v9 atau backup rollover yang akan dipakai secara offline.
   Periksa apakah backup berasal dari waktu sebelum masalah.
3. Jalankan `restore-copy` ke **direktori lokal baru**, lakukan verifikasi
   penuh dan cocokkan SHA snapshot/dataset.
4. Rencanakan cutover atomik beserta unit systemd dan dependensi semua
   konsumen DB; **jangan** `cp` ke DB aktif dan jangan ubah symlink NFS langsung.
5. Validasi IndoBERT benar-benar loaded, health status, total snapshot,
   progres, dan perilaku alert; jika gagal gunakan rollback terencana.
6. Simpan arsip insiden dan evidence; jangan menghapus journal 2.2 GB atau
   backup migrasi yang ada.

**Tidak ada tindakan restore ke database produksi yang diotomatisasi v9.**

## Menonaktifkan timer

```bash
systemctl disable --now pandi-bima-backup.timer \
  pandi-bima-rehearsal.timer pandi-bima-readiness.timer
```

Data backup dan attestation yang telah dibuat tidak dihapus otomatis oleh
perintah penonaktifan tersebut.

## Skrip praktis dari paket v9

Di **Mac**: simpan `bima-phase1a-health-v8.patch` dan
`bima-phase1bcd-v9.patch` di `~/Downloads`, lalu:

```bash
bash ~/Downloads/bima_phase1_apply_mac_v9.sh
# Periksa staged files sebelum commit
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git commit -m "feat: BIMA Phase 1 production health and recovery"
git push origin main
```

Di **server** setelah push:

```bash
REPO=/home/fauzi/crawler-rebuild
runuser -u fauzi -- git -C "$REPO" pull --ff-only origin main
bash "$REPO/tools/bima_phase1_run_server_v9.sh"
```

Jika baseline berhasil dan ingin menyalakan keempat timer sekaligus, jalankan
script yang sama dengan `--enable-timers`. Operasi backup manual berikutnya
akan membuat **backup tambahan** dan tidak menghapus cadangan saat ini.
