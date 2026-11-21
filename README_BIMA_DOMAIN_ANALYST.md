# BIMA sebagai asisten analis domain

Patch ini menambahkan percakapan analisis melalui Telegram menggunakan bukti
yang sudah tersimpan di BIMA. Basis patch adalah commit
`202a5bcd073c1caa6f217c9f6534d48ce9dc2b9f` (audit tahap 1).

**Dua mode tersedia:** laporan bukti yang dapat digunakan tanpa model tambahan,
dan penjelasan generatif melalui penyedia yang dikonfigurasi operator. Mode
generatif memakai OpenAI Responses API atau Ollama. Tidak ada penyedia/model
yang diaktifkan otomatis. Fitur Telegram juga nonaktif secara default.

## 1. Perintah dan hasil

| Perintah | Kegunaan |
|---|---|
| `/analisis contoh.id` | Membaca maksimal lima job dengan hostname awal persis tersebut. |
| `/analisis https://contoh.id/path` | Mencari URL persis yang tersimpan. |
| `/analisis #123 Mengapa ditandai?` | Menjelaskan satu job dan pertanyaan terkait. |
| `/tanya Apakah hasil model dan aturan berbeda?` | Pertanyaan lanjutan pada job dalam konteks pribadi yang sama. |
| `/bandingkan #123 #456` | Membandingkan dua job tersimpan. |
| `/lupakan` | Menghapus konteks sementara dan membatalkan publikasi jawaban yang masih menunggu. |

Pesan biasa dapat menjadi pertanyaan lanjutan setelah ada konteks analisis
yang berhasil dikirim. Pesan biasa tanpa konteks tetap diabaikan. Semua contoh
ID dan domain di dokumen ini adalah ilustrasi; ganti dengan data yang tersedia.

Setiap hasil menyebut sumber seperti `[job#123]`, status proses, risiko tersimpan,
waktu pembaruan status, serta keterbatasan bukti. Dalam mode laporan, rincian
model, perbedaan sinyal, dan review ditampilkan bila tersedia. Dalam mode
generatif, penjelasan AI ditandai dan diperiksa agar rujukannya hanya memakai
job yang dipilih. Validasi rujukan tidak membuktikan semua interpretasi model
benar; analis tetap menilai kesesuaian penjelasan dengan bukti.

Contoh kemampuan yang dituju:

- Menjelaskan bahwa `pending_alert` berarti pengiriman belum terkonfirmasi.
- Menunjukkan klasifikasi risiko menengah yang berbeda dari prediksi IndoBERT.
- Membedakan kegagalan DNS/HTTP, antrean browser, dan hasil analisis selesai.
- Menyebutkan bahwa cuplikan teks atau hasil model tidak tersedia.
- Membandingkan status/risk/model dua job tanpa mengarang urutan perubahan situs.

## 2. Makna bukti dan batas pencarian

Asisten membaca SQLite melalui koneksi `mode=ro` dan `query_only=ON`. Tidak ada
migrasi, model yang dimuat, halaman yang diambil, antrean pemeriksaan yang dibuat,
label review yang diubah, atau alert BIMA yang dikirim oleh jalur analisis.
IndoBERT dan proses crawler tetap dijalankan oleh komponen yang sudah ada.
Permintaan pemeriksaan baru tetap dilakukan operator melalui `/cek`.

Hal-hal berikut sengaja dijelaskan dalam jawaban:

- `jobs` menyimpan hasil terkini per URL, bukan riwayat crawl lengkap. Dua ID job
  tidak otomatis merupakan keadaan sebelum/sesudah pada situs yang sama.
- `updated_at` adalah waktu pembaruan status. Pengiriman alert atau pengantrean
  ulang dapat mengubahnya; nilai itu bukan jaminan waktu pengambilan halaman.
- Snapshot review adalah rekaman terpisah yang dapat lebih lama dari job saat
  ini. Keputusan situs dan label teks untuk pelatihan adalah dua penilaian berbeda.
- `ml_conf` merupakan skor model terpilih; `alert_confidence` bukan penggantinya.
  Nol pada keluaran aturan aman tanpa inferensi tidak ditampilkan sebagai
  probabilitas model. `bert_used` sendiri belum membuktikan inferensi berhasil.
- Tidak ada screenshot tersimpan yang dapat dilihat asisten. Cuplikan teks hanya
  tersedia jika sebelumnya ditangkap/ditambahkan oleh alur yang sudah ada.
- Hasil `failed`, `retry_http`, dan `inconclusive` bukan bukti situs aman.

Pencarian `#job` memakai primary key. Pencarian URL memakai indeks URL dan
mempertahankan query untuk pencocokan persis; query/path tidak ditampilkan atau
dikirim ke penyedia. Pencarian hostname memakai rentang awalan URL terindeks,
membatasi kandidat, lalu memeriksa ulang identitas host. Ia tidak memindai seluruh
1,5 juta URL. `.id`, subdomain dan IDN ditangani; `www` tidak digabung otomatis.

Pencarian domain hanya untuk **hostname awal persis**, bukan semua subdomain atau
semua tujuan redirect. Variasi ejaan/case URL yang tidak dicakup pencarian dapat
terlewat. Hasil kosong berarti tidak ditemukan pada pencarian ini, bukan belum
pernah dicrawl. Gunakan `#job` atau URL persis untuk hasil paling tegas. Beberapa
job dalam satu jawaban dibaca berurutan; perbandingan bukan snapshot transaksi
serentak dari seluruh database.

## 3. Akses, konteks, dan batas sumber daya

Fitur hanya melayani chat yang teramati sebagai `private`, dengan chat ID sama
dengan sender ID, terdaftar dalam `TELEGRAM_ALLOWED_CHAT_IDS`, dan lolos aturan
reviewer yang sudah ada. `TELEGRAM_REVIEW_USER_IDS` yang tidak disetel mewarisi
chat pribadi yang diizinkan; nilai kosong secara eksplisit menolak semua reviewer.

Dua worker latar belakang menjalankan analisis. Maksimal delapan permintaan
diterima sekaligus, termasuk jawaban yang menunggu pengiriman. Satu pengguna
memiliki paling banyak satu permintaan belum selesai. Adapter mengizinkan satu
generasi model aktif; jika sibuk, laporan bukti tetap diberikan. Polling dan
perintah monitor tetap berjalan selama model bekerja.

Konteks dibatasi maksimal 100 sesi, dua putaran riwayat pendek, dan masa aktif
30 menit menggunakan waktu monotonic. Konteks, deduplikasi update, dan jawaban
menunggu hanya berada di RAM. Deduplikasi menyimpan maksimal 1.024 update yang
diterima selama 24 jam, terpisah dari konteks percakapan. Restart monitor
menghapus semuanya; perlindungan replay tidak melintasi restart atau batas
cache tersebut. Jawaban yang belum terkirim dapat hilang saat
restart. `/lupakan` membatalkan publikasi hasil dalam antrean atau yang sedang
dibuat; permintaan jaringan yang sudah dikirim tidak dapat ditarik kembali dan
tetap dapat dikenai biaya penyedia.

Jawaban dikirim sebagai teks biasa, maksimal 3.800 karakter. Sumber tidak dipotong
untuk memaksakan jawaban model panjang. Respons model yang tidak valid, tidak
lengkap, ditolak, atau gagal akan digantikan laporan bukti. Kegagalan sementara
Telegram menahan jawaban untuk dicoba lagi; penolakan pasti HTTP 400/403,
otorisasi dicabut, atau konteks kedaluwarsa membatalkan pengiriman.

## 4. Data menuju penyedia model

`disabled` tidak mengirim permintaan model. Penyedia aktif menerima pertanyaan,
riwayat pendek, dan field bukti terpilih: ID job, host, waktu/status/risiko,
skor dengan maknanya, cuplikan terbatas, serta review tanpa identitas reviewer.
URL dengan query, fragment, userinfo dan path pribadi disamarkan. Error mentah,
HTML penuh, screenshot, nama berkas lokal, token bot, dan catatan reviewer tidak
dimasukkan. Penyaringan mengurangi paparan, tetapi bukan jaminan mendeteksi semua
rahasia yang sengaja ditempel dalam teks bebas; jangan memasukkan kredensial ke
pertanyaan atau bukti situs.

Model tidak diberi SQL, shell, alat eksekusi, atau kemampuan mengubah BIMA.
Tulisan situs dan teks percakapan diperlakukan sebagai data yang tidak tepercaya.
Tidak ada retry generasi otomatis, pengalihan HTTP, perpindahan penyedia, atau
pengiriman ulang prompt untuk memperbaiki jawaban.

Batas adapter: permintaan 32 KiB, respons 128 KiB, penjelasan 3.000 karakter,
keluaran 768 token, connect timeout 5 detik dan read timeout 45 detik. Timeout
socket bukan batas total waktu permintaan; koneksi yang terus mengirim sebagian
data dapat berlangsung lebih lama. Batas worker menjaga bot tetap responsif.
Proxy/netrc dari environment tidak dipakai adapter ini; OpenAI memerlukan akses
TLS langsung ke endpoint resmi.

Ollama memakai `num_ctx=8192`, batas prompt lokal konservatif, `stream=false`,
dan `keep_alive=0`. Batas byte bukan tokenizer universal. Operator perlu menguji
profil model dengan satu job, perbandingan dua job, dan pertanyaan lanjutan.
`keep_alive=0` melepas model setelah jawaban, bukan mengurangi puncak RAM inferensi.

## 5. Terapkan dan uji di Mac

Jalankan dari checkout yang biasa dipakai. Jangan paksa patch jika pemeriksaan
gagal, dan pertahankan perubahan lokal lain.

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git branch --show-current
git log -1 --oneline
git apply --check "$HOME/Downloads/bima-domain-analyst-202a5bc.patch"
git apply "$HOME/Downloads/bima-domain-analyst-202a5bc.patch"
```

Gunakan Python 3.11 atau 3.12. Python 3.9 bawaan Command Line Tools Mac bukan
environment pengujian untuk patch ini. Jika venv pengujian belum tersedia:

```bash
python3.11 -m venv "$HOME/.venvs/bima-test"
TEST_PY="$HOME/.venvs/bima-test/bin/python3"
"$TEST_PY" -m pip install requests beautifulsoup4 Pillow 'dnspython>=2.6,<3'
```

Jika Python yang dipasang bernama `python3.12`, gunakan itu untuk membuat venv.
Tidak perlu memasang Torch, Transformers, Selenium, Chrome, atau model baru
untuk unit test berikut.

```bash
TEST_PY="$HOME/.venvs/bima-test/bin/python3"
PYTHONPATH="$PWD/multi:$PWD" "$TEST_PY" -B \
  -m unittest discover -s multi/tests -p 'test*.py' -q
PYTHONPATH="$PWD/multi:$PWD" "$TEST_PY" -B \
  multi/tests/regression_classifier_rule.py crawler_url.py
git diff --check
```

Di Mac, empat skip pengujian cron khusus Linux memang diharapkan. Hasil akhir
harus `OK`, bukan import error. Pengujian menggunakan SQLite sementara dan
respons penyedia/Telegram sintetis; tidak membutuhkan kunci API nyata.

Setelah lulus, commit berkas fitur secara eksplisit, kemudian push dari branch
yang biasa Anda deploy (`main` dalam alur saat ini). Jangan memakai `git add .`
karena checkout berisi konfigurasi lokal.

```bash
git add -- README.md README_BIMA_DOMAIN_ANALYST.md \
  docs/superpowers/specs/2026-11-21-bima-domain-analyst-design.md \
  docs/superpowers/plans/2026-11-21-bima-domain-analyst.md \
  multi/analyst.env.example multi/analyst_evidence.py multi/analyst_llm.py \
  multi/domain_analyst.py multi/telegram_analyst.py multi/telegram_monitor.py \
  multi/tests/test_analyst_evidence.py multi/tests/test_analyst_llm.py \
  multi/tests/test_domain_analyst.py multi/tests/test_telegram_analyst.py
git diff --cached --stat
git commit -m "feat: add BIMA domain analyst assistant"
git push origin main
```

## 6. Pull dan uji di server sebagai fauzi

Jalankan sesudah push berhasil. Perubahan lokal `crawler_google.py` dan
`domains.json` yang dilaporkan sebelumnya harus tetap dipertahankan.

```bash
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H bash -c '
  cd "$1" || exit
  PYTHONPATH="$PWD/multi:$PWD" venv/bin/python3 -B \
    -m unittest discover -s multi/tests -p "test*.py" -q
' _ "$APP"
```

Pengujian harus sebagai `fauzi`. Menjalankannya sebagai root akan terkena guard
Chrome pada pengujian Telegram yang sudah ada. Patch ini tidak memerlukan stop
atau restart `pandi-crawler-multi.service`.

CLI hanya membaca database dan tidak memerlukan token Telegram:

```bash
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$APP/multi/domain_analyst.py" \
  --db "$APP/multi/state/crawler_production.sqlite3" \
  --target '#123' --evidence-only
```

Ganti `#123` dengan ID nyata. Untuk membandingkan, ulangi `--target '#456'`;
tambahkan `--question 'Apa perbedaannya?'` atau `--json` bila diperlukan.
`--evidence-only` mengalahkan konfigurasi penyedia apa pun di environment.

## 7. Aktifkan Telegram dengan laporan bukti dahulu

Simpan konfigurasi baru **di luar checkout Git**. Berkas
`multi/telegram_monitor.env` yang sudah ada tercatat dalam repo; jangan menambahkan
kunci API baru ke sana. Biarkan konfigurasi/token Telegram yang berfungsi tetap
dikelola sesuai pengaturan deployment saat ini.

```bash
APP=/home/fauzi/crawler-rebuild
ANALYST_ENV=/home/fauzi/.config/bima/analyst.env
sudo install -d -o fauzi -g fauzi -m 700 /home/fauzi/.config/bima
sudo test -e "$ANALYST_ENV" || sudo install -o fauzi -g fauzi -m 600 \
  "$APP/multi/analyst.env.example" "$ANALYST_ENV"
sudoedit "$ANALYST_ENV"
```

Set nilai aktif berikut, dengan satu definisi untuk setiap variabel:

```ini
BIMA_ANALYST_ENABLED=1
BIMA_ANALYST_PROVIDER=disabled
```

Tambahkan sumber environment untuk monitor menggunakan
`sudo systemctl edit pandi-telegram-monitor.service`. Pertahankan pengaturan
lain yang sudah ada dan tambahkan:

```ini
[Service]
EnvironmentFile=/home/fauzi/.config/bima/analyst.env
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart pandi-telegram-monitor.service
sudo systemctl is-active pandi-telegram-monitor.service pandi-crawler-multi.service
sudo journalctl -u pandi-telegram-monitor.service -n 40 --no-pager
```

Di chat pribadi reviewer, coba `/analisis #123`, `/tanya Mengapa?`, lalu
`/lupakan`. Pastikan jawaban menyebut mode laporan bukti. Untuk mencoba model
generatif nanti, ubah file environment yang sama dan restart monitor saja.

## 8. Pilihan model generatif

### OpenAI

Operator memilih model yang tersedia untuk akunnya. Konfigurasi berikut
mengizinkan pengiriman paket bukti tersaring ke OpenAI dan permintaan dapat
dikenai biaya sesuai akun/model.

```ini
BIMA_ANALYST_ENABLED=1
BIMA_ANALYST_PROVIDER=openai
BIMA_ANALYST_ALLOW_CLOUD=1
BIMA_ANALYST_MODEL=REPLACE_WITH_AN_AVAILABLE_MODEL
BIMA_ANALYST_OPENAI_KEY_FILE=/home/fauzi/.config/bima/openai.key
```

Simpan hanya kunci dalam file absolut reguler milik `fauzi`, izin `600`, di
direktori pribadi di atas. Alternatifnya `OPENAI_API_KEY` dapat dibaca dari
environment service; jangan mengatur keduanya. Jangan menempelkan kunci dalam
chat, argumen perintah, commit, atau log.

Adapter menggunakan endpoint resmi `/v1/responses`, `store=false`, tanpa tools
dan tanpa streaming model. `store=false` tidak berarti semua retensi penyedia
nol; penyimpanan respons dan pemantauan penyalahgunaan memiliki aturan terpisah.
Lihat [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
dan [data controls](https://developers.openai.com/api/docs/guides/your-data).

### Ollama pada mesin yang sudah disiapkan

Patch tidak memasang daemon, mengunduh model, atau menambah runtime LLM pada
server crawler 2 vCPU/4 GiB. Gunakan mesin inferensi yang kapasitasnya sudah
diverifikasi; origin loopback dapat berupa tunnel yang dikelola operator.

```ini
BIMA_ANALYST_ENABLED=1
BIMA_ANALYST_PROVIDER=ollama
BIMA_ANALYST_MODEL=REPLACE_WITH_AN_INSTALLED_LOCAL_MODEL
BIMA_ANALYST_OLLAMA_ORIGIN=http://127.0.0.1:11434
BIMA_ANALYST_OLLAMA_LOCAL_ONLY=1
```

Origin harus alamat numerik loopback HTTP(S), tanpa userinfo, path, query,
atau fragment. Origin jaringan umum dan nama hostname ditolak.

**Sebelum mengatur `BIMA_ANALYST_OLLAMA_LOCAL_ONLY=1`, nonaktifkan cloud pada
daemon Ollama dengan `OLLAMA_NO_CLOUD=1` atau pengaturan
`disable_ollama_cloud: true`, lalu restart daemon tersebut.** API lokal Ollama
dapat meneruskan permintaan ke model cloud; `127.0.0.1` sendiri bukan bukti
inferensi lokal. Variabel BIMA adalah pengakuan operator, bukan perintah yang
mengubah daemon atau bukti otomatis bahwa enforcement berhasil. Referensi:
[Ollama FAQ](https://docs.ollama.com/faq),
[cloud](https://docs.ollama.com/cloud), dan
[chat API](https://docs.ollama.com/api/chat).

Uji model pilihan dengan bukti yang disetujui operator. Jika jawaban terlalu
panjang, tidak selesai, tanpa sumber, atau mengarang rujukan job, BIMA menampilkan
laporan bukti. Pengujian kontrak offline tidak menggantikan validasi kualitas
bahasa, latensi, memori, biaya, dan kecocokan model pada deployment sebenarnya.

## 9. Nonaktifkan atau rollback

Untuk menghentikan generasi saja, set `BIMA_ANALYST_PROVIDER=disabled` lalu restart
monitor. Untuk menonaktifkan semua perintah asisten, set
`BIMA_ANALYST_ENABLED=0` lalu restart monitor. Tidak ada schema/data crawler yang
perlu dipulihkan karena fitur hanya membaca.

Jika kode perlu dikembalikan setelah commit dipush, gunakan `git revert` terhadap
commit fitur di Mac, uji, push, lalu pull di server dan restart monitor. Jangan
`reset --hard`, `clean`, atau menimpa perubahan lokal server. Patch yang belum
di-commit dapat dibalik hanya jika `git apply --reverse --check` lulus pada tree
yang belum diberi perubahan lanjutan.

## 10. Validasi pengembangan

Pengujian mencakup query terindeks dan read-only, identitas IDN/host, URL privat,
metadata rusak, perbedaan model/aturan, review lama, penyedia nonaktif/gagal,
format respons resmi sintetis, batas payload, rujukan tidak dikenal, otorisasi,
isolasi sesi, replay update, lupa/kedaluwarsa, antrean dan pengiriman Telegram.
Hasil suite integrasi dan pemeriksaan patch dicatat pada rencana implementasi
di `docs/superpowers/plans/2026-11-21-bima-domain-analyst.md`.
