# BIMA Tahap 2 — Browser resilience dan anti-bot precision (incremental)

Basis pemeriksaan: kode publik BIMA pada commit `724883c`. Patch ini **independen**
dari Stage 1 Pre-Alert Stability v2; tidak mengubah `multi/selective_prealert.py`,
Telegram, jadwal Discovery, DB produksi, payload IDADX, atau konfigurasi privat.

## Masalah yang diperbaiki

1. WebDriver dapat mengalami `UnexpectedAlertPresentException`, misalnya dari
   dialog JavaScript DataTables. Pembacaan page title/source/current URL kini
   mendismiss dialog *satu kali*, kemudian mengulang hanya pembacaan tersebut.
   Jika masih gagal: tetap `inconclusive/browser_error` dan browser dibersihkan.
   Tidak ada retry navigasi, submit, klik, atau pengiriman alert.
2. Pemeriksaan anti-bot lama menggunakan substring sangat umum seperti
   `waiting for`, `cloudflare`, `captcha` dan `verification successful`.
   Halaman berita/panduan bisa salah dianggap anti-bot. Detektor baru
   memerlukan sinyal yang lebih spesifik (judul challenge, frase verifikasi,
   atau gabungan beberapa petunjuk) dan membatasi interpretasi teks panjang.
3. Mesin klasifikasi asal (`crawler_url.py`) melakukan pemeriksaan anti-bot
   **di dalam** `classify_text_content` dan rutinitas menunggu. BIMA mengganti
   referensi fungsi deteksi pada instance engine saat `load_engine()` (sekali,
   sebelum memulai worker) sehingga pemeriksaan konsisten di semua tahap.
   **IndoBERT tetap dimuat dan tetap wajib**; seluruh aturan keputusan judol
   lainnya tetap menggunakan classifier asal.
4. Exception WebDriver disimpan sebagai nama kelas exception, bukan pesan
   mentah dari situs/JavaScript yang mungkin mengandung data sensitif.

Batasan: pengecekan CAPTCHA berbasis teks/heading adalah heuristik, bukan
bukti pasti. Halaman yang tidak dapat dibaca tetap `inconclusive`; tidak otomatis
menjadi `low_risk`. Pengaruh pada recall harus diukur dengan sampel nyata.

## Isi patch

- `multi/browser_resilience.py` (baru): penilaian challenge dan safe read retry
- `multi/crawler_multi.py` (perubahan minimal): memanggil helper saat load
  engine dan observasi browser
- `multi/tests/test_browser_resilience_v1.py` (baru): 20 tes offline, tanpa
  jaringan atau DB produksi
- `README_BIMA_BROWSER_RESILIENCE_V1.md` (dokumen ini)

## Instalasi di Mac (Mac > GitHub > server)

Dari root repository lokal BIMA:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git pull --ff-only origin main
PATCH="$HOME/Downloads/bima-browser-resilience-v1.patch"
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
```

Jika `git apply --check` gagal, **jangan gunakan `--reject`, `-3`, `reset`
atau `clean` secara otomatis**. Periksa selisih versi dahulu. Pastikan Stage 1
sudah diterapkan sebelum menguji BIMA lengkap jika deployment itu direncanakan.

Uji pada Mac:

```bash
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_browser_resilience_v1.py' -v
python3 -B -m py_compile multi/browser_resilience.py multi/crawler_multi.py
# Jalankan suite test BIMA yang biasa digunakan jika venv/dependencies tersedia.
```

Commit **hanya** empat file dari patch, jangan menambahkan konfigurasi privat:

```bash
git add -- multi/browser_resilience.py multi/crawler_multi.py \
  multi/tests/test_browser_resilience_v1.py \
  README_BIMA_BROWSER_RESILIENCE_V1.md
git diff --cached --check
git diff --cached --name-status
git commit -m "fix: recover Selenium page alerts and reduce false CAPTCHA detections"
git push origin main
```

## Rollout pada Linux

Periksa perubahan lokal server terlebih dahulu (file lain jangan disentuh):

```bash
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" diff -- multi/crawler_multi.py
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_browser_resilience_v1.py' -v
sudo -u fauzi -H "$APP/venv/bin/python3" -B -m py_compile \
  "$APP/multi/browser_resilience.py" "$APP/multi/crawler_multi.py"
```

Jika tes berhasil, **hanya saat aman secara operasional** aktifkan melalui
restart crawler (bukan Telegram; biarkan peninjauan dan pengiriman tetap hidup):

```bash
sudo systemctl restart pandi-crawler-multi.service
systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
journalctl -u pandi-crawler-multi.service --since '3 minutes ago' --no-pager -n 50
```

Pastikan log menunjukkan `IndoBERT loaded ONCE`; tidak perlu menjalankan
ulang jobs lama atau mengirim ulang kasus `send_unknown`.

## Verifikasi setelah rollout

Amati persentase `browser_error`, `captcha_or_antibot` dan jumlah `inconclusive`
selama beberapa hari. Periksa manual hasil untuk situs yang sebelumnya memicu
DataTables dialog dan kemungkinan false-positive CAPTCHA. Jangan otomatis
memaksa semua job lama di-retry; lakukan sampling terkontrol setelah review.

## Yang belum diklaim

- Tes unit offline tidak membuktikan semua Chrome/ChromeDriver versi atau
  situs riil bebas popup.
- Belum ada evaluasi precision/recall dengan data terverifikasi.
- Tidak mengubah retry database, pengiriman API, atau perangkat Selenium.
- File `crawler_url.py` asal tidak ditulis ulang sehingga dapat tetap digunakan
  oleh proses legacy terpisah sesuai perilaku lamanya.
