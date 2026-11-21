# BIMA v4.3 — Advanced Redirect Intelligence (preview-only)

**Baseline:** BIMA commit `302e9cb` (v4.2.1). **Workflow:** Mac → test → `git push` → server `git pull` → preview terbatas. Patch incremental, tidak mengubah crawler utama, Telegram Monitor, model IndoBERT, file `urls.json` Registry, ataupun database produksi.

## Tujuan dan batasan

BIMA meneliti domain dan subdomain `.id` untuk menemukan indikasi redirect website, terutama yang perlu diverifikasi lebih lanjut untuk dugaan judi online. **Redirect bukan vonis abuse.** Keputusan abuse tetap membutuhkan bukti terkonfirmasi dan klasifikasi konten dengan IndoBERT/engine BIMA.

- `urls.json` adalah inventaris sekitar 1,5 juta domain `.id` dengan NS dari Registry, diperbarui **manual**. Modul Discovery tidak mengubahnya.
- **Opt-in `--advanced-js`:** analisis *pasif* JavaScript inline, bukan menjalankan JavaScript.
- Mendukung beberapa bentuk tujuan statis: `'https://' + 'tujuan.example'`, `atob(...)`, `decodeURIComponent(...)`, `String.fromCharCode(...)`, `const URL = '...'; location.replace(URL)`, dan `window.open(url, '_self')`.
- Ekspresi yang membutuhkan runtime (`location.href = getTarget()`) **hanya** menjadi *review hint* tanpa destination dan tanpa klaim redirect.
- `--browser-review-limit N` membuat **shortlist review manual**, *tidak* menjalankan Chrome/Selenium. Browser otomatis memerlukan isolasi egress, kebijakan robots, resource limit, dan pemeriksaan pembuktian tersendiri sebelum bisa diaktifkan pada rilis berikutnya.
- `--redirect-chain-preview N` menampilkan hop HTTP teramati **terpisah** dari sinyal JS/meta yang statis. Tampilan chain bersifat pengelompokan observasi, bukan reproduksi sesi browser yang pasti.
- Destinasi eksternal (TLD selain `.id`) **hanya dicatat sebagai bukti**; tidak di-fetch oleh Discovery, tidak di-queue sebagai target global, tidak mengirim alert.
- Query, fragmen dan kredensial URL tidak disimpan dalam evidence/preview; **path** masih boleh tercantum, jadi batasi penyebaran log mentah.
- HTTP-first dan HTTPS fallback v4.2 serta canonical-www v4.2.1 tetap ada. BIMA menjalankan browser hanya di pipeline utama yang sudah terpisah; tidak ada browser tambahan pada Discovery.
- Perketatan pada proses `run()` Discovery: setiap redirect HTTP **dalam origin yang telah diverifikasi** wajib cocok dengan kebijakan `robots.txt` sebelum URL tujuan diminta. Perpindahan origin masih dihentikan sampai origin baru diverifikasi. Saat robots `404`, tidak ada aturan disallow khusus.

## File dan opsi

| File | Fungsi |
|---|---|
| `multi/redirect_intelligence.py` | Parser statis JavaScript bounded; pengelompokan observasi redirect; tidak melakukan jaringan/browser |
| `multi/proactive_discovery.py` | Opsi v4.3, integrasi evidence, preview reviewer dan pemeriksaan redirect sesuai robots |
| `multi/tests/test_redirect_v43.py` | Pengujian offline JavaScript, privacy/redaksi, batas resource, HTTP, robots, dan output preview |
| `README_DISCOVERY_V43.md` | Panduan rilis dan rollback |

Opsi baru **nonaktif secara default**:

- `--advanced-js` — deteksi statis tambahan; **preview-only** (tidak boleh dengan `--apply`).
- `--browser-review-limit 10` — maksimal 10 lead untuk verifikasi browser manual; hanya preview, bukan eksekusi browser.
- `--redirect-chain-preview 10` — maksimal 10 rangkaian/observasi; hanya preview.

Output yang ditambahkan: `advanced_js`, `js_static_redirects`, `js_dynamic_pages`, `preview_browser_review`, `preview_redirect_chains`, dan statistik masing-masing seed jika `--diagnostic-seeds` digunakan. Semua nilai `priority` pada review adalah prioritas **investigasi**, bukan confidence judi online.

## 1. Mac: terapkan patch

Unduh `bima-v4.2.1-to-v4.3-redirect-intelligence.patch` ke `~/Downloads`.

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

git status --short
git log -1 --oneline
# Harus berada di 302e9cb dan tree bersih sebelum apply.

git apply --check ~/Downloads/bima-v4.2.1-to-v4.3-redirect-intelligence.patch && \
git apply ~/Downloads/bima-v4.2.1-to-v4.3-redirect-intelligence.patch
```

Jika pemeriksaan patch gagal, **jangan gunakan** `git apply --reject`, `git reset --hard`, atau patch kumulatif dari rilis lain.

## 2. Mac: test dan commit

```bash
python3 -B -m unittest discover \
  -s multi/tests -p 'test_redirect_v43.py' -v

python3 -B -m unittest discover \
  -s multi/tests -q

git diff --check
git status --short

git add -- README_DISCOVERY_V43.md \
  multi/proactive_discovery.py \
  multi/redirect_intelligence.py \
  multi/tests/test_redirect_v43.py

git diff --cached --check
git diff --cached --stat

git commit -m "feat: add passive advanced redirect intelligence to BIMA"
git push origin main
```

Pada fixture yang direkonstruksi dari versi BIMA sebelumnya, **158 tes dijalankan, 157 berhasil, 1 skip** karena fixture tidak memuat seluruh implementasi `crawler_multi.py`. Tes Mac pada repository lengkap tetap wajib. Tidak ada browser atau HTTP sungguhan yang dijalankan oleh unit test baru.

## 3. Server: pull dan test (tidak perlu restart crawler)

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main
sudo -u fauzi -H git log -1 --oneline

sudo -u fauzi -H ./venv/bin/python3 -B -m unittest discover \
  -s multi/tests -p 'test_redirect_v43.py' -v

sudo -u fauzi -H ./venv/bin/python3 -B -m unittest discover \
  -s multi/tests -q
```

Jangan menjalankan operasi Git sebagai `root`, dan jangan men-stage `config.json`, database SQLite, `urls.json`, atau model IndoBERT.

## 4. Server: preview read-only dan terbatas

```bash
sudo -u fauzi -H ./venv/bin/python3 -B \
  multi/proactive_discovery.py \
  --input urls.json \
  --db multi/state/crawler_production.sqlite3 \
  --scan-web --http-fallback --canonical-www \
  --advanced-js --browser-review-limit 10 \
  --redirect-chain-preview 10 --diagnostic-seeds 10 \
  --seed-limit 10 --start-index 500000 \
  --max-candidates-per-seed 10 --max-sitemaps 1 \
  --probe-candidates 0 --timeout 6 --request-interval 0.5
```

Perintah di atas **tidak** memakai `--apply`, tidak menulis DB, tidak menjalankan Selenium/Chrome, tidak mengaktifkan timer, tidak mengirim alert, dan tidak mengubah service crawler yang sedang aktif. Hasil reviewer mungkin kosong jika 10 seed tersebut tidak menunjukkan indikasi JS/redirect; itu normal, bukan error.

Periksa terutama `js_static_redirects`, `js_dynamic_pages`, `preview_browser_review`, `preview_redirect_chains`, `redirect_stop_reasons`, serta `robots_reasons`. Jangan menyimpulkan abuse dari `priority` atau JS literal yang belum dieksekusi.

## 5. Rollback

Sebelum commit, jika **hanya patch ini** yang mengubah file:

```bash
git apply --reverse --check ~/Downloads/bima-v4.2.1-to-v4.3-redirect-intelligence.patch && \
git apply --reverse ~/Downloads/bima-v4.2.1-to-v4.3-redirect-intelligence.patch
```

Tidak ada migrasi DB pada v4.3. Setelah commit di server, pengembalian versi harus dilakukan melalui revert Git terkontrol di Mac, kemudian push/pull, bukan mengedit produksi secara langsung.

## Keterbatasan yang sengaja dipertahankan

- Tidak men-download/menganalisis file JS eksternal, tidak menjalankan JavaScript, tidak menginterpretasikan semua kemungkinan obfuscation/DOM navigation, dan tidak mengukur cloaking berbasis browser.
- `const` dan decoder terbatas menghasilkan **sinyal statis**, bukan kepastian bahwa redirect dieksekusi di runtime.
- Review list bukan tiket atau laporan abuse; operator tetap perlu validasi manual dan pipeline IndoBERT.
- Besarnya antrean 1,5 juta job membuat `--apply` Discovery tidak disarankan; pemilahan kapasitas dan prioritas harus ditangani dalam desain antrean khusus.
- Browser terisolasi untuk memverifikasi sinyal berisiko adalah fase berikutnya setelah keamanan browser dan aturan jaringan dievaluasi. Menggunakan Chrome `--no-sandbox` untuk menjelajahi halaman yang belum dipercaya **tidak dianjurkan**.
