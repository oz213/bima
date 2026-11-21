# BIMA — perbaikan audit tahap 1

Patch ini dibuat terhadap commit `16191fd58d0b8b53bef631a8af4986e0ab612392`.
Ruang lingkupnya adalah tiga temuan audit: prioritas aturan konteks berita,
transport pengiriman alert BIMA, dan contoh domain pada kartu Telegram `/pola`.

## Perubahan perilaku

### 1. Konteks berita dan bukti promosi

Penanda berita pada URL, judul, atau teks tidak lagi langsung menghasilkan
`low_risk` apabila pemeriksaan yang sudah ada menemukan promosi operator yang
kuat atau tautan pendaftaran yang memenuhi aturan `Slot Thailand`. Halaman
tersebut diteruskan ke inferensi dan aturan risiko yang sudah ada. Bukti ini
tidak langsung menetapkan `high_risk`; hasil model dan ambang yang berlaku
tetap menentukan keputusan.

Pengecualian berita/fact-check tanpa bukti tersebut tetap berlaku. Pengujian
juga mencakup berita anti-judi, pendaftaran pada footer, kebijakan penolakan
promosi judi, dan portal pelaporan. Hasil pemeriksaan tautan pendaftaran
digunakan kembali agar `classify_risk()` tidak mengulangi pemeriksaan
pendaftaran. Pemeriksaan HTML lain, misalnya tautan tersembunyi, tetap
mengikuti alur yang sudah ada.

### 2. Satu upaya transport per pemanggilan pengiriman alert

`send_alert()` memakai sesi Requests tersendiri dengan retry koneksi, baca,
status, dan redirect dinonaktifkan. Requests juga menerima
`allow_redirects=False`, sehingga respons 301/302/303/307/308 tidak meneruskan
permintaan, API key, atau screenshot ke URL tujuan redirect. Sesi alert tidak
berbagi cookie dengan sesi crawling. Pengaturan proxy dan CA dari lingkungan
tetap didukung; respons dan sesi ditutup sesudah pengiriman.

Payload, timeout, serta kontrak sukses **HTTP 200** tetap sama. Respons 201/204
tetap tidak dianggap sukses. Retry GET pada sesi crawling tetap berfungsi.
Kegagalan atau respons yang tidak mengonfirmasi penerimaan secara default
menghasilkan `False`. Opsi `ALERT_RAISE_ON_FAILURE=True` tetap dapat meneruskan
exception kepada pemanggil. Alur multi-worker mempertahankan job sebagai
`pending_alert` pada kedua jalur kegagalan tersebut.

Ini membatasi pengulangan otomatis oleh klien, tetapi **tidak menjamin satu
laporan saja di backend**. Timeout dapat terjadi setelah BIMA menerima laporan.
Periksa BIMA sebelum menjalankan pemulihan manual `--retry-pending-alerts`.
Gunakan URL endpoint akhir pada konfigurasi: endpoint yang mengandalkan
redirect sekarang akan menghasilkan kegagalan terkontrol dan pending alert.

Perubahan berlaku pada engine `crawler_url.py` yang digunakan service utama.
Pengirim dalam engine alternatif/arsip tidak ikut diubah.

### 3. Contoh domain `/pola`

Setiap kartu mengambil contoh domain dari anggota kelompoknya sendiri.
Contoh tetap unik, diurutkan, dan dibatasi tiga domain. Peringkat kelompok,
jumlah anggota, label TP/FP, dan kontrol reviewer memakai aturan yang sama.

## Batas perubahan

IndoBERT, ambang klasifikasi, kombinasi Classic ML/IndoBERT, input manual
`urls.json`, cakupan `.id` beserta subdomain/IDN, Selenium, dan screenshot dalam
memori dipertahankan. Tidak ada migrasi database atau perubahan state job oleh
patch. Temuan lain tentang koneksi keluar, koordinasi proses, normalisasi
recrawl, dan pengelompokan learning masih merupakan pekerjaan terpisah.

## Verifikasi

- Baseline: 489 test lulus.
- Setelah perubahan: 509 test lulus, tanpa failure, error, atau skip.
- Regresi classifier mandiri: 8 test lulus.
- Regresi baru terlebih dahulu gagal pada kode baseline karena bug yang diuji.
- Suite dijalankan dengan socket/DNS jaringan diblokir, respons transport
  sintetis, dan database sementara. Pengujian transport memakai mekanisme
  retry/redirect Requests asli, dengan respons diganti di bawah adapter.
- Pengujian classifier mengeksekusi helper dari source asli dengan keluaran
  model tetap. Ini memverifikasi aturan keputusan, bukan mengukur akurasi
  bobot IndoBERT pada korpus produksi.

Lingkungan verifikasi menggunakan Python 3.12.14. Patch tidak menambah
dependensi dan sintaks Python diperiksa agar kompatibel dengan Python 3.11.
Tidak ada crawl live, alert BIMA, pesan Telegram, atau operasi service produksi
yang dilakukan untuk pengujian ini.

## Penerapan lokal

Unduh `bima-audit-stage1-16191fd.patch`, kemudian jalankan dari root repository
BIMA. Tinjau perubahan lokal terlebih dahulu. Jika pemeriksaan patch gagal,
selesaikan perbedaan commit/file sebelum menerapkan; jangan memakai
`--reject`, `git reset --hard`, atau `git clean` sebagai jalan pintas.

```sh
git status --short
git rev-parse --short HEAD
bima_patch="$HOME/Downloads/bima-audit-stage1-16191fd.patch"
git apply --check "$bima_patch"
git apply "$bima_patch"
git diff --check
```

Gunakan interpreter dari virtual environment BIMA yang sudah memiliki
dependensi proyek. Dengan environment tersebut aktif:

```sh
PYTHONPATH="$PWD/multi:$PWD" python3 -B \
  -m unittest discover -s multi/tests -p 'test*.py' -q
PYTHONPATH="$PWD/multi:$PWD" python3 -B \
  multi/tests/regression_classifier_rule.py crawler_url.py
```

Pada baseline di atas, hasil yang diharapkan adalah 509 dan 8 test lulus.
Tinjau diff sebelum commit. Stage hanya file patch berikut agar perubahan
lokal lain tidak ikut masuk:

```sh
git add -- crawler_url.py multi/learning/patterns.py \
  multi/tests/test_learning_patterns.py \
  multi/tests/test_news_promotion_precedence.py \
  multi/tests/test_alert_transport.py README_AUDIT_STAGE1.md \
  docs/superpowers/specs/2026-11-21-bima-audit-stage1-design.md \
  docs/superpowers/plans/2026-11-21-bima-audit-stage1.md
git diff --cached --check
git diff --cached --stat
git commit -m "fix: harden news precedence, BIMA alert transport and pattern examples"
```

## Aktivasi di server

Setelah perubahan masuk melalui alur Git yang digunakan proyek, jalankan
kembali test dengan `./venv/bin/python3` dari direktori aplikasi server.
Pertahankan konfigurasi, database, `urls.json`, dan perubahan lokal server.

Proses yang sudah berjalan masih memakai kode yang telah dimuat. Untuk
mengaktifkan perubahan pada instalasi systemd yang didokumentasikan proyek,
lakukan restart terkendali pada jadwal deployment:

```sh
sudo systemctl restart pandi-crawler-multi.service pandi-telegram-monitor.service
sudo systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
sudo journalctl -u pandi-crawler-multi.service -n 50 --no-pager
sudo journalctl -u pandi-telegram-monitor.service -n 30 --no-pager
```

Perintah ini merupakan panduan operator, bukan tindakan yang dijalankan saat
pembuatan patch. Pastikan endpoint BIMA tidak mengandalkan redirect sebelum
aktivasi. Jangan me-requeue laporan lama hanya untuk menguji perubahan ini.

## Mengembalikan patch sebelum staging/commit

Jika patch baru diterapkan dan file terkait belum berubah lagi:

```sh
git apply -R --check "$bima_patch"
git apply -R "$bima_patch"
```

Jika sudah di-commit dan dibagikan, gunakan `git revert` atas commit perbaikan
melalui alur deployment normal. Pembalikan kode tidak menghapus laporan yang
sudah diterima BIMA dan tidak mengubah riwayat job.
