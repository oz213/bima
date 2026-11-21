# BIMA v4.2 — Reliable Internet Discovery (.id)

**Basis:** kode BIMA pada commit `4f070af` (v4.1). **Implementasi:** patch incremental, bukan mengganti seluruh crawler. Terapkan pertama kali di Mac, jalankan pengujian, lalu commit/push. `git pull` server **tidak** otomatis mengaktifkan fitur.

## Prinsip

- `urls.json` tetap baseline domain Registry PANDI yang **memiliki NS** dan **diperbarui secara manual**. File tidak diubah maupun digantikan feed baru.
- Hanya domain dan subdomain `.id` (termasuk punycode/IDN) yang dijadikan target discovery aktif. Redirect ke domain luar `.id` **hanya dicatat sebagai bukti**, tidak di-fetch oleh modul Discovery dan tidak di-enqueue sebagai target global.
- IndoBERT, Classic ML, proses crawler utama, API BIMA, dan Telegram tidak diubah. Redirect bukan bukti final judi online; klasifikasi dan verifikasi tetap terpisah.
- Discovery tetap **preview/read-only secara default**, tanpa `--apply` dan tanpa alert otomatis. `--http-fallback` juga **nonaktif secara default**.
- HTTPS → HTTP fallback hanya terjadi bila **transport HTTPS gagal** (timeout, koneksi ditolak/terputus, atau TLS error tertentu). Tidak dilakukan untuk **DNS gagal**, IP nonpublik, HTTP 401/403/429, pelanggaran robots, atau redirect ke luar cakupan.
- **Sebelum** mencoba HTTP, BIMA meminta `http://domain.id/robots.txt` dan memeriksa aturan untuk asal HTTP tersebut. Kebijakan robots dari HTTPS **tidak digunakan** untuk mengizinkan HTTP. Pemeriksaan subdomain membutuhkan kebijakan robots-nya sendiri.
- Fetcher tetap menggunakan resolusi IP publik terverifikasi, IP pinning untuk koneksi, pemisahan origin, batas ukuran respons, dan batas hop redirect.

## Perubahan kode

| File | Perubahan |
|---|---|
| `multi/proactive_discovery.py` | Opsi `--http-fallback`; pemeriksaan robots tiap origin; statistik kategori error; prioritas kandidat berdasarkan bukti redirect HTTP/Meta Refresh |
| `multi/tests/test_discovery_v42.py` | Unit test offline, mencakup fallback, robots/HTTP denial, redirect eksternal, DNS/SSRF, dan prioritas kandidat |
| `README_DISCOVERY_V42.md` | Panduan pengujian, aktivasi aman, dan batasan |

### Statistik tambahan di output JSON

- `http_fallback`: flag yang diminta (`true`/`false`).
- `http_fallback_attempts`: jumlah asal domain yang dicoba fallback.
- `http_fallback_success`: jumlah fallback yang mendapatkan halaman HTTP 200 atau bukti redirect eksternal.
- `http_fallback_blocked`: percobaan fallback yang **diblokir** oleh robots atau gagal memvalidasi asalnya.
- `fetch_failure_types`: kategori kegagalan (`dns`, `timeout`, `tls`, `connection`, `access_denied`, dll.), **tanpa menyimpan pesan error atau query token**. Termasuk percobaan HTTPS yang gagal tetapi bisa pulih melalui HTTP.
- `web_errors`: kegagalan akses terminal pada alur scanning, bukan jumlah total error transport mentah.

## 1. Mac: cek dan terapkan patch

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

git status --short
git log -1 --oneline
# Pastikan main di commit 4f070af atau cek konflik terlebih dahulu.

git apply --check ~/Downloads/bima-v4.1-to-v4.2-discovery.patch && \
git apply ~/Downloads/bima-v4.1-to-v4.2-discovery.patch
```

Jika `git apply --check` gagal, **jangan** jalankan apply, `git reset --hard`, atau `git apply --reject`. Kirim output konflik untuk penyesuaian yang spesifik.

## 2. Mac: pengujian

```bash
python3 -B -m unittest discover \
  -s multi/tests -p 'test_proactive_discovery.py' -v

python3 -B -m unittest discover \
  -s multi/tests -p 'test_discovery_v42.py' -v

# Unit test di atas sudah memverifikasi sintaks Python.
git diff --check
```

Patch tidak membutuhkan dependensi Python baru. Pengujian di atas menggunakan mock; tidak melakukan network fetch, tidak menyentuh database, dan tidak mengirim alert.

## 3. Mac: commit dan push

```bash
git add -- README_DISCOVERY_V42.md \
  multi/proactive_discovery.py \
  multi/tests/test_discovery_v42.py

git diff --cached --check
git diff --cached --stat
git commit -m "feat: add safe HTTP fallback to BIMA .id discovery"
git push origin main
```

## 4. Server: pull, test, dan preview

**Jangan gunakan perintah Git sebagai root** di repository produksi, agar `.git/index` tetap dimiliki `fauzi`.

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main

sudo -u fauzi -H ./venv/bin/python3 -B -m unittest discover \
  -s multi/tests -p 'test_discovery_v42.py' -v
```

Untuk melihat apakah HTTP fallback membantu sebagian domain, jalankan **preview terbatas**:

```bash
sudo -u fauzi -H ./venv/bin/python3 -B \
  multi/proactive_discovery.py \
  --input urls.json \
  --db multi/state/crawler_production.sqlite3 \
  --scan-web --http-fallback \
  --seed-limit 3 --start-index 1000 \
  --max-candidates-per-seed 10 \
  --max-sitemaps 1 --probe-candidates 0 \
  --timeout 6 --request-interval 0.5
```

Output `mode=preview`, dan tidak ada mutasi ke database. Aktivasi timer discovery serta `--apply` **tidak termasuk** dalam langkah ini. Crawler utama dengan IndoBERT/Telegram/auto-network-retry v4.1 tetap berjalan seperti biasa dan **tidak perlu direstart untuk patch discovery ini**.

## Batasan sebelum otomatisasi penuh

1. Fallback hanya mencakup origin seed/homepage, bukan semua path ditemukan atau seluruh sitemap HTTPS yang gagal.
2. Deteksi `js_literal` masih statis; JavaScript runtime dan cloaking memerlukan browser verifikasi on-demand dengan egress isolation yang kuat.
3. `fetch_failure_types` adalah kategori operasional, bukan label benar/salah atau klasifikasi abuse.
4. Saat antrean utama masih ~1,5 juta pending, batas `max_pending` Discovery tetap mencegah `--apply` menambah pekerjaan; perlu mekanisme prioritas dan kapasitas khusus sebelum aktivasi penuh.
5. `--apply` bisa menambahkan target `.id` yang kemudian dikunjungi oleh crawler utama. Audit pembatasan SSRF, redirect eksternal, sandbox Chrome (`--no-sandbox` saat ini), serta kebijakan alert sebelum produksi.
6. Jangan menyimpan kredensial/API key/model/SQLite/`urls.json` ke GitHub. Review visibilitas repository publik.

## Rollback

Jika patch baru belum di-commit dan file tidak memiliki perubahan lain:

```bash
git apply --reverse --check ~/Downloads/bima-v4.1-to-v4.2-discovery.patch && \
git apply --reverse ~/Downloads/bima-v4.1-to-v4.2-discovery.patch
```

Rollback tidak memerlukan perubahan database karena tidak ada migrasi dalam v4.2.
