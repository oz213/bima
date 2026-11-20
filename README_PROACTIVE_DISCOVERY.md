# BIMA Proactive .ID Discovery — Patch v2 (Registry `urls.json`)

**Tujuan:** melengkapi crawler BIMA yang sudah ada dengan *URL discovery* dan *passive subdomain discovery*. **Belum dideploy** dan **belum diaktifkan** di server PANDI.

Patch ini **menggantikan rancangan** `bima-discovery-v1.patch`. **Jangan apply kedua patch.**

## 1. Klarifikasi sumber data dan arsitektur

- `urls.json` adalah daftar domain dari Registry PANDI yang **sudah memiliki NS**. File tersebut **diperbarui manual** oleh operator.
- `multi/crawler_multi.py` telah menerima `urls.json` berupa `[...]` atau `{"urls": [...]}`; elemen array berupa URL/domain string. Modul v2 mendukung kedua format tersebut.
- `urls.json` tetap menjadi *seed inventory*: hanya dibaca, **tidak diubah atau dibuat ulang**.
- Tidak ada koneksi database langsung ke Registry, impor ekspor Registry kedua, atau sinkronisasi domain baru otomatis dalam patch ini.
- Kandidat baru akan ditambahkan ke tabel `jobs` **hanya ketika** flag `--apply` digunakan. Proses crawler yang ada tetap bertanggung jawab atas klasifikasi IndoBERT, Selenium, dan pelaporan BIMA.
- Dengan `--apply`, dua tabel dan satu indeks baru akan dibuat secara aditif pada SQLite crawler: `proactive_discovery_state` dan `proactive_discovery_candidates`. Tidak ada penghapusan atau reset job.

```text
urls.json (Registry delegated .id; update manual)
          |
          v
multi/proactive_discovery.py (baru)
  |- home page + tautan HTML (hanya di bawah seed .id)
  |- robots.txt + sitemap (mematuhi larangan robots)
  |- crt.sh public CT (opsional; perlu flag --ct)
  |- filter IDN .id / URL bersih / dedup / skor prioritas
          |
          +--> PREVIEW (default; tanpa perubahan database)
          |
          +--> --apply --> BIMA SQLite jobs(pending) --> crawler_multi.py
                                           (IndoBERT tetap)
```

## 2. Apa yang sudah dan belum dikerjakan

**Sudah (patch ini):**

1. Seed otomatis dari data `urls.json`, dengan pembagian batch dan cursor SHA-256; ketika operator memperbarui `urls.json`, scan dimulai lagi dari seed pertama.
2. Pencarian halaman HTML yang ditautkan dari homepage, `robots.txt`, dan XML sitemap sampai batas per domain. Tidak melakukan brute-force path, tidak menjalankan JavaScript, dan tidak memuat Selenium.
3. CT lookup opsional via `crt.sh` untuk hostname subdomain yang cocok dengan seed. Lookup mengirim nama domain ke penyedia eksternal, sehingga **dimatikan secara default**.
4. Validasi domain `.id`, IDN/punycode, hanya subdomain di bawah seed, tanpa query/fragment, port nonstandar maupun kredensial URL, dan menghindari path GET aksi seperti `logout`/`delete`.
5. HTTP fetcher sendiri mengikuti redirect **hanya di bawah seed**; tidak memakai proxy environment, memverifikasi IP DNS global dan mengunci koneksi ke IP tervalidasi untuk mengurangi risiko DNS rebinding. Browser tidak dipakai.
6. Batas ukuran respons, timeout, rate limit per-host, pembatasan sitemap, penyimpanan kandidat yang tertahan saat antrean penuh, dan idempotensi `INSERT OR IGNORE` terhadap `jobs(url UNIQUE)`.
7. Mode default **preview/read-only** (tidak membuat tabel, tidak menambah job, tidak memicu alert BIMA). `--apply` adalah tindakan opt-in tersendiri.
8. Unit service/timer Linux disediakan sebagai *file di repo*, tetapi tidak diinstal atau diaktifkan oleh `git pull`.

**Belum (fase berikutnya):**

- Sinkronisasi `urls.json` langsung dari Registry; domain baru yang belum masuk file tidak terjamin tercakup.
- Discovery via search engine, passive DNS, urlscan.io, dan threat intelligence otomatis.
- *Risk-based re-crawling*, deteksi perubahan DNS, skor AI khusus phishing/malware, dan dashboard SOC.
- Pembatasan menyeluruh atas redirect dan resource pihak ketiga **di proses crawler lama**: `crawler_multi.py` saat ini bisa mengikuti redirect ke luar `.id` dan browser dapat mengakses sumber daya eksternal. Filter `.id` pada **discovery** bukan firewall keseluruhan. **Jangan aktifkan --apply produksi sampai kebijakan scope, browser, dan egress diselesaikan.**

**Catatan akurasi:** `priority_score` hanya memprioritaskan pemeriksaan, **bukan vonis abuse**. Semua keputusan klasifikasi tetap diserahkan ke classifier BIMA/IndoBERT dengan verifikasi yang sesuai. Mesin discovery menghormati `robots.txt` untuk host utama saat memanen URL.

## 3. Isi patch

```text
README_PROACTIVE_DISCOVERY.md
multi/proactive_discovery.py
multi/tests/test_proactive_discovery.py
deploy/pandi-proactive-discovery.service
deploy/pandi-proactive-discovery.timer
deploy/90-enable-proactive-enqueue.conf  # OPSIONAL, JANGAN PASANG SEKARANG
```

Hanya menambah **enam file baru**. Tidak menyentuh `crawler_url.py`, `crawler_multi.py`, model IndoBERT, `urls.json`, `config.json`, data SQLite, atau file unit service aktif.

## 4. Apply patch di Mac (rekomendasi)

Unduh `bima-proactive-id-v2.patch` ke `~/Downloads`.

```bash
cd ~/Projects/bima                # ganti sesuai path repo BIMA di Mac

git remote -v                    # pastikan remote oz213/bima.git
git branch --show-current
git status --short                # amankan perubahan lokal jika ada

git switch main
git pull --ff-only origin main

git apply --check ~/Downloads/bima-proactive-id-v2.patch
git apply ~/Downloads/bima-proactive-id-v2.patch

# Python 3.10+; 28 unit test offline, tanpa dependensi tambahan
python3 -m unittest discover -s multi/tests -p 'test_proactive_discovery.py' -v

# Lint/check patch dan staged diff
git diff --check
git add README_PROACTIVE_DISCOVERY.md multi/proactive_discovery.py \
    multi/tests/test_proactive_discovery.py \
    deploy/pandi-proactive-discovery.service \
    deploy/pandi-proactive-discovery.timer \
    deploy/90-enable-proactive-enqueue.conf
git diff --cached --check
git diff --cached --stat

git commit -m "feat: discover .id URLs and subdomains from registry seeds"
git push origin main
```

**Jangan** `git add .` untuk kasus ini. `urls.json`, token Telegram, `config.json`, model privat, dan DB produksi tidak boleh masuk commit. Repo GitHub saat dicek masih berstatus public; pastikan kebijakan visibilitas repository sudah sesuai.

Jika `main` dilindungi atau ada konflik, kerjakan di branch `feature/proactive-id-discovery`, lalu merge lewat PR; server baru menarik branch tujuan setelah merge. Jangan paksa push.

## 5. Setelah push, di server hanya perlu `git pull` untuk mengambil kode

```bash
cd /home/fauzi/crawler-rebuild

git status --short          # tinjau; jangan timpa file lokal yang dimodifikasi
git branch --show-current

# Sesuaikan user yang memiliki checkout. Jangan pakai sudo bila sudah login fauzi.
sudo -u fauzi git pull --ff-only origin main

sudo -u fauzi -H ./venv/bin/python3 -B -m unittest discover \
  -s multi/tests -p test_proactive_discovery.py -v
```

**Kode dan test sudah ada setelah pull.** Unit systemd dan timer di `/etc/systemd/system` **tidak otomatis ikut terpasang** (instalasi satu kali dijelaskan di bawah). Tidak ada restart service crawler atau Telegram Monitor yang diperlukan untuk menarik kode discovery saja.

## 6. Uji discovery manual di server tanpa menambah antrean

Periksa crawler sudah sehat (error permission `crawler_url.py` sebelumnya harus diselesaikan lebih dahulu):

```bash
systemctl is-active pandi-crawler-multi.service pandi-telegram-monitor.service
sudo -u fauzi test -r /home/fauzi/crawler-rebuild/urls.json && echo 'urls readable'
sudo -u fauzi test -r /home/fauzi/crawler-rebuild/crawler_url.py && echo 'engine readable'
```

Preview offline (cek seed dan JSON saja, tidak ada akses situs):

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H ./venv/bin/python3 -B multi/proactive_discovery.py \
  --input urls.json --db multi/state/crawler_production.sqlite3 \
  --seed-limit 20
```

Preview jaringan (hanya homepage/robots/sitemap dari seed `.id`, dan hanya HTTP(S) publik yang tervalidasi):

```bash
sudo -u fauzi -H ./venv/bin/python3 -B multi/proactive_discovery.py \
  --input urls.json --db multi/state/crawler_production.sqlite3 \
  --scan-web --seed-limit 5 --max-candidates-per-seed 20 \
  --max-new 10 --max-pending 1000
```

**Tanpa `--apply`, tidak ada penambahan job maupun perubahan cursor.** Karena itu preview akan mengulang batch yang sama sampai diberi `--start-index` berbeda, atau digunakan mode `--apply` setelah review.

Untuk melihat batch berbeda tanpa perubahan database:

```bash
sudo -u fauzi -H ./venv/bin/python3 -B multi/proactive_discovery.py \
  --input urls.json --db multi/state/crawler_production.sqlite3 \
  --scan-web --seed-limit 5 --start-index 100
```

CT pasif bersifat **opsional** dan tidak berjalan kecuali `--ct` diminta. Gunakan dulu pada jumlah domain kecil dan setelah persetujuan penggunaan feed eksternal:

```bash
sudo -u fauzi -H ./venv/bin/python3 -B multi/proactive_discovery.py \
  --input urls.json --db multi/state/crawler_production.sqlite3 \
  --ct --seed-limit 2 --timeout 8
```

Output berupa JSON dengan `candidate_urls`, `new_jobs` (selalu 0 dalam preview), `pending_before`, `pending_after`, `web_errors`, `ct_errors`, dan beberapa contoh kandidat. URL yang ditampilkan sudah tanpa query/fragment.

## 7. Instal service/timer satu kali (tetap PREVIEW)

Khusus saat ingin menggunakan systemd; setelah `git pull`, jalankan sebagai root:

```bash
install -o root -g root -m 0644 \
  /home/fauzi/crawler-rebuild/deploy/pandi-proactive-discovery.service \
  /etc/systemd/system/pandi-proactive-discovery.service
install -o root -g root -m 0644 \
  /home/fauzi/crawler-rebuild/deploy/pandi-proactive-discovery.timer \
  /etc/systemd/system/pandi-proactive-discovery.timer
systemctl daemon-reload
systemctl start pandi-proactive-discovery.service
systemctl status pandi-proactive-discovery.service --no-pager -l
journalctl -u pandi-proactive-discovery.service -n 30 --no-pager
```

Unit `Type=oneshot` selesai dengan status sukses `inactive (dead)` setelah eksekusi, berbeda dari service crawler yang terus berjalan. Jika mode preview cukup, jangan aktifkan timer tanpa alasan: preview tidak menyimpan cursor dan akan memeriksa batch awal yang sama setiap kali berjalan.

Bila sudah waktunya menjadwalkan eksekusi (setelah disetujui), timer tersedia tetapi belum diaktifkan otomatis:

```bash
systemctl enable --now pandi-proactive-discovery.timer
systemctl list-timers pandi-proactive-discovery.timer --all
```

## 8. Aktivasi --apply: PERLU REVIEW OPERASIONAL TERPISAH

**Jangan lakukan langkah ini saat tahap pengujian di Mac atau preview server.** `--apply` memasukkan kandidat baru sebagai `jobs(status='pending')` ke database crawler produksi. Pada crawler non-`--dry-run`, job dapat langsung diambil dan menghasilkan alert nyata ke BIMA.

File opsional `deploy/90-enable-proactive-enqueue.conf` mengubah ExecStart untuk menambahkan `--apply`. Simpan file ini di Git untuk diaudit, tetapi **jangan salin ke systemd sebelum** keamanan redirect/browser, pembatasan egress, verifikasi alert, kapasitas antrean dan otorisasi pemrosesan disetujui. Lakukan smoke test pada DB terpisah, dan mulai dengan `--max-new` kecil.

Untuk rollback layanan discovery saja (tidak menghapus job BIMA yang telah diproses):

```bash
systemctl disable --now pandi-proactive-discovery.timer
systemctl stop pandi-proactive-discovery.service
```

Jangan hapus `jobs` atau `proactive_discovery_candidates` dari SQLite produksi tanpa rencana backup dan rekonsiliasi.

## 9. Pengujian dan batasan

Pengujian offline dijalankan dengan Python 3.13 pada modul ini (kode ditargetkan kompatibel Python 3.10+), dan mensimulasikan HTTP maupun SQLite: **28/28 lulus**. Ini bukan uji langsung terhadap `urls.json` produksi, internet, sistem BIMA, model IndoBERT, sistemd, maupun integrasi browser server.

Beberapa keterbatasan disengaja untuk keamanan:

- Penelusuran link hanya satu tingkat dari homepage plus sitemap, tidak melaksanakan brute force dan tidak mengikuti JavaScript/SPA; situs kompromi tersembunyi mungkin belum tertangkap.
- HTTP 403 pada `robots.txt` atau robots tidak dapat dijangkau menyebabkan pemanenan konten web dilewati; ini konservatif untuk menjaga kebijakan crawl.
- URL dengan query dihapus untuk mengurangi kemungkinan kebocoran token dan ledakan jumlah target; risiko pada query-only route mungkin terlewat.
- CT tidak mengandung semua subdomain, dan API publik memiliki kuota/availability tersendiri.
- Daftar Registry yang diisi manual bisa menjadi usang. Otomasi feed read-only dari Registry merupakan pengembangan berikutnya setelah ada akses/skema resmi.
- **Tidak melakukan crawling internet global.** CT merupakan kueri ke API pihak ketiga untuk **nama domain `.id` saja**, bukan perluasan scanning ke TLD lain.

## 10. Prioritas fase selanjutnya

1. Memperbaiki perimeter `.id` pada **crawler utama**, termasuk pemeriksaan redirect, Selenium, dan konfigurasi firewall egress.
2. Mengembangkan discovery dari indeks pencarian publik / feed threat intelligence khusus indikator `.id`.
3. Menambahkan antrian *review* dan workflow konfirmasi untuk mencegah auto-report dari temuan yang belum diverifikasi.
4. Sinkronisasi read-only domain ber-NS dari Registry; atur jadwal/sumber tanpa menaruh akses database Registry pada repo publik.
5. Integrasi SOC dan dashboard tren, termasuk klasifikasi phishing dan malware terpisah dari model judi online.
