# Penyimpanan dan tata kelola repository BIMA

**Prinsip:** Git berisi kode, dokumentasi, test, dan konfigurasi contoh. Data
produksi, dataset, model IndoBERT, cache dan backup berada di NFS. Server
menjalankan kode di `/home/fauzi/crawler-rebuild` dengan venv lokal.

## Tata letak produksi (23 November 2026)

| Path aplikasi | Target/penyimpanan |
|---|---|
| `models/` | Symlink ke `/mnt/registrar-sandbox/bima/models/legacy` |
| `/home/fauzi/bima-shadow/models` | Symlink ke `/mnt/registrar-sandbox/bima/models/shadow` (IndoBERT kandidat) |
| `urls.json` | Symlink ke `/mnt/registrar-sandbox/bima/datasets/urls.json` |
| `multi/state/` | Symlink ke `/mnt/registrar-sandbox/bima/state` |
| `logs/` | Symlink ke `/mnt/registrar-sandbox/bima/logs` |
| Chrome cache | `/mnt/registrar-sandbox/bima/cache/chrome` |
| Backup rollover terkelola | `/mnt/registrar-sandbox/bima/backups/registry-rollover/` |

`urls.json.next` adalah file staging reguler di direktori NFS `datasets/`.
**Jangan** memasukkan database, dataset, `config.json`, file `.env`, token,
APK, checkpoint model, atau symlink runtime ke Git.

Empat file tokenizer/konfigurasi lama di `models/indobert-judol/` tidak lagi
dilacak setelah cleanup repository. Menghapus *index Git* tidak berarti
menghapus model maupun file aktual di Mac/NFS. Model IndoBERT **tidak dinonaktifkan**.

`gambling_classifier.pkl` dan `arial.ttf` tetap tracked untuk kompatibilitas
kode lama; jangan memindahkan/menghapusnya tanpa memvalidasi path runtime.

## Rollover dataset

Pada server, setelah validasi `urls.json.next`:

```bash
sudo python3 /home/fauzi/crawler-rebuild/tools/bima_registry_rollover_v2.py
```

Untuk penurunan jumlah URL lebih dari 50% yang memang disengaja,
`--allow-major-shrink` dapat dipakai setelah memverifikasi sumber datanya.
Snapshot v6 mengantrekan ulang **semua** URL valid pada siklus baru; riwayat
alert/review tetap dipertahankan dalam SQLite. Statistik Telegram `/status`
mengacu ke snapshot aktif, bukan seluruh riwayat `jobs`.

Backup rollover dibuat sebelum cutover dan backup rollover terkelola sebelumnya
baru dihapus setelah siklus baru sehat. Arsip recovery dan backup lain di
luar lokasi terkelola **tidak ikut dihapus**.

## Aturan deploy Git

1. Edit dan test di Mac; stage file spesifik; `git commit` dan `git push`.
2. Sebelum `git pull` di server, periksa `git status --short` dan symlink NFS.
3. Pada commit yang menghapus 4 file model tracked, gunakan prosedur
   migrasi symlink khusus; `git pull` biasa bisa gagal karena Git melihat
   model yang dipindahkan sebagai file terhapus.
4. Jangan menjalankan `git reset --hard`, `git clean -fd`, atau `git add .`
   di server produksi.
5. Jangan ubah ownership seluruh direktori proyek secara rekursif;
   sesuaikan metadata `.git` saja apabila diperlukan.

**Catatan SQLite NFS:** produksi memakai journal mode `DELETE` (bukan WAL)
untuk mengurangi masalah WAL pada NFS. Meski demikian, SQLite di NFS tetap
memiliki risiko locking dan gangguan jaringan. Pertahankan backup independen,
monitor integritas database, dan pertimbangkan pemindahan SQLite ke disk lokal
berikut strategi backup NFS jika risiko operasional dinilai terlalu tinggi.

Sistem `journald` tidak termasuk dalam prosedur cleanup repository.
