# BIMA v4.2.1 — Smart Robots & Redirect Diagnostics

**Basis:** GitHub BIMA `main` commit `56d0d5a` (v4.2). Ini patch **incremental** khusus modul Discovery, bukan patch seluruh crawler. Terapkan di Mac terlebih dahulu; uji sebelum commit/push; server kemudian `git pull` dan uji **preview**.

## Masalah yang ditangani

Preview 30 seed sebelumnya memperlihatkan 37 kandidat, 7 redirect internal, 5 percobaan HTTP fallback, dan kegagalan `robots.txt` pada beberapa domain yang mengalihkan apex ke `www`. Nilai `http_fallback_blocked` sebelumnya juga menggabungkan penolakan kebijakan dengan kegagalan koneksi. v4.2.1 memisahkan diagnostik agar alasan kegagalan terlihat tanpa membocorkan query/token URL.

## Perubahan keamanan dan fungsional

1. **Transport berhenti pada pergantian origin.** `SafeFetcher` tidak lagi otomatis mengakses origin baru saat menerima redirect apex→`www` atau HTTPS→HTTP, walaupun host masih berada dalam satu seed `.id`. Redirect tetap dicatat sebagai bukti yang disanitasi. Ini perubahan hardening yang berlaku di modul Discovery, termasuk ketika `--canonical-www` belum diaktifkan.
2. **Opt-in `--canonical-www`.** Hanya redirect HTTP dari seed ke **tepat** `www.<seed>` dengan skema dan path tetap (`/robots.txt` → `/robots.txt`, atau `/` → `/`) yang layak diikuti oleh alur Discovery. Sebelum mengambil homepage `www`, BIMA melakukan **permintaan terpisah ke `www.<seed>/robots.txt`** dan wajib mendapatkan izin dari origin tersebut. Tidak menggunakan `robots.txt` apex sebagai izin untuk `www`.
3. **Fail closed.** `robots.txt` yang berakhir di `/auth/signin` atau path non-`/robots.txt`, HTTP 401/403/429/503, error DNS/TLS/timeout, konten nonteks/terlalu besar, atau aturan `Disallow: /` tidak membuka izin crawling. Redirect ke subdomain selain `www`, ke TLD luar `.id`, atau HTTPS→HTTP karena redirect tidak menjadi alasan melakukan fetch lintas origin.
4. **Statistik rinci.** Tambahan `robots_reasons`, `http_response_codes`, `redirect_stop_reasons`, `canonical_www_attempts`, `canonical_www_verified`, `canonical_www_blocked`, `canonical_www_homepage_success`.
5. **Opt-in diagnostik per seed.** `--diagnostic-seeds 30` menghasilkan `preview_diagnostics` (maksimal 100 seed) berisi hanya hostname seed `.id`, origin terpilih, kategori robots/homepage/redirect, jumlah kandidat, dan statistik aman. Tidak menyimpan error mentah, query string, fragment, atau token. Hanya boleh dalam mode `--scan-web` **tanpa `--apply`**.

**Tidak berubah:** `urls.json` adalah baseline domain yang memiliki NS dan diperbarui secara manual. Target discovery aktif tetap `.id`; redirect keluar `.id` dicatat tanpa fetch global; IndoBERT, Classic ML, API BIMA, Telegram Monitor, Automatic Network Retry v4.1, database utama, dan service systemd tidak diubah.

## 1. Mac — periksa patch incremental

Unduh `bima-v4.2-to-v4.2.1-smart-robots.patch` ke `~/Downloads`.

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
# HEAD harus 56d0d5a sebelum apply
git log -1 --oneline

git apply --check ~/Downloads/bima-v4.2-to-v4.2.1-smart-robots.patch && \
git apply ~/Downloads/bima-v4.2-to-v4.2.1-smart-robots.patch
```

Jangan apply jika `--check` gagal. Tidak diperlukan `git reset --hard`.

## 2. Mac — pengujian

```bash
python3 -B -m unittest discover -s multi/tests -p 'test_proactive_discovery.py' -v
python3 -B -m unittest discover -s multi/tests -p 'test_discovery_v42.py' -v
python3 -B -m unittest discover -s multi/tests -p 'test_discovery_v421.py' -v
# Opsional: seluruh fixture bila lingkungan sudah memenuhi dependensinya
python3 -B -m unittest discover -s multi/tests -q

git diff --check
git diff --stat
git status --short
```

## 3. Mac — commit/push (jika semua test lulus)

```bash
git add -- multi/proactive_discovery.py multi/tests/test_discovery_v421.py README_DISCOVERY_V421.md
git diff --cached --check
git diff --cached --stat
git commit -m "feat: verify canonical www robots and add discovery diagnostics"
git push origin main
```

Jangan stage `urls.json`, `config.json`, model IndoBERT, SQLite, atau virtualenv.

## 4. Server — pull dan tes, tanpa restart crawler

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main

for suite in test_proactive_discovery.py test_discovery_v42.py test_discovery_v421.py; do
  sudo -u fauzi -H ./venv/bin/python3 -B -m unittest discover \
    -s multi/tests -p "$suite" -q || break
done
```

**Pengujian preview**, dibatasi 10 seed tanpa menyimpan data:

```bash
sudo -u fauzi -H ./venv/bin/python3 -B multi/proactive_discovery.py \
  --input urls.json --db multi/state/crawler_production.sqlite3 \
  --scan-web --http-fallback --canonical-www --diagnostic-seeds 10 \
  --seed-limit 10 --start-index 500000 \
  --max-candidates-per-seed 10 --max-sitemaps 1 \
  --probe-candidates 0 --timeout 6 --request-interval 0.5
```

Mode tetap `preview`, `new_jobs=0`, `pending_before=pending_after`; tidak ada migrasi DB atau alert otomatis. **Jangan tambahkan `--apply`**, jangan aktifkan timer Discovery/Recrawl, dan jangan restart service crawler utama untuk perubahan modul ini.

### Cara membaca output

- `robots_reasons`: kategori per *permintaan robots*, mis. `robots_timeout`, `robots_dns`, `robots_access_denied`, `robots_disallow`, `robots_origin_redirect`, `robots_origin_or_path_mismatch`, `robots_allowed`, `robots_not_found`.
- `http_response_codes`: jumlah respons akhir yang terlihat oleh modul untuk semua fetch (termasuk robots, homepage, sitemap, probe); bukan proporsi domain.
- `redirect_stop_reasons`: alasan penghentian setelah redirect, mis. `redirect_origin_requires_robots` atau `redirect_out_of_scope`.
- `canonical_www_attempts`: banyaknya perpindahan canonical yang diusulkan dan diverifikasi secara terpisah; `canonical_www_verified` berarti izin robots `www` didapat, **bukan** berarti halaman berhasil diakses.
- `canonical_www_homepage_success`: halaman `www` berhasil diakses setelah robots host itu tervalidasi.
- `preview_diagnostics`: penjelasan aman per seed yang diaktifkan via `--diagnostic-seeds`.

## Batasan sebelum otomatisasi penuh

- Hanya canonical apex→`www.<seed>` dengan path identik yang diperlakukan sebagai kandidat follow-up otomatis. Redirect ke path berbeda, host lain, atau perubahan scheme yang eksplisit tidak diikuti demi keamanan.
- Redirect **dalam origin yang sama** masih mengikuti perilaku fetcher lama; pemeriksaan setiap hop terhadap aturan path `robots.txt` perlu audit sebelum aktivasi crawling otomatis dalam skala besar.
- Statistik respons dan kategori adalah diagnostik, **bukan bukti judol**. URL kandidat perlu melewati klasifikasi konten BIMA termasuk IndoBERT. Browser verifikasi Javascript dan sandbox Chrome (`--no-sandbox` sekarang) masih perlu perbaikan terpisah.
- Tidak menambah antrean saat `pending` masih sekitar 1,5 juta. Set `--apply` hanya setelah kebijakan prioritas antrean, verifikasi keamanan, dan pengaturan kapasitas dievaluasi.

## Rollback

Pada Mac, sebelum perubahan di-commit dan bila belum ada edit tambahan pada tiga file:

```bash
git apply --reverse --check ~/Downloads/bima-v4.2-to-v4.2.1-smart-robots.patch && \
git apply --reverse ~/Downloads/bima-v4.2-to-v4.2.1-smart-robots.patch
```

Tidak ada migrasi database pada v4.2.1.
