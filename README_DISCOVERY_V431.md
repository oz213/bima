# BIMA v4.3.1 — DNS-Aware Discovery & Deterministic Stratified Sampling

**Basis Git yang diuji:** `b5142e8` (`main`, BIMA v4.3). **Alur rilis:** Mac → unit test → `git push` → Linux `git pull` → preview kecil. **Tidak mengubah crawler utama, IndoBERT, Telegram, retry scheduler, `urls.json`, database, atau layanan systemd.**

## Mengapa versi ini dibuat

- `urls.json` sudah berasal dari Registry PANDI dan berisi hostname `.id` **yang memiliki NS** (sekitar 1,55 juta). Pembaruannya masih **manual**. Domain ber-NS tidak selalu memiliki A/AAAA atau DNS yang berfungsi.
- Pengujian `--start-index 1000000 --seed-limit 30` sebelumnya memilih 30 domain berurutan yang semuanya gagal DNS, sedangkan sampel tersebar berjumlah 30 domain dari 5 rentang menghasilkan 60 kandidat URL dan 16 halaman probe. **Pengamatan itu belum membuktikan prevalensi pada seluruh `.id`.**
- Empat domain `.web.id` contoh memperlihatkan `SERVFAIL` pada resolver lokal, Cloudflare dan Google. Pemeriksaan `dig` langsung terhadap dua NS yang didelegasikan untuk `asazuzuqidisa.web.id` memberikan `REFUSED`, `EDE 20`, tanpa `aa`. Ini bukti operasional *lame delegation* saat diperiksa, **bukan** bukti judi online.
- BIMA sudah memiliki `multi/dns_audit.py`, yang menelusuri referral dari zona `.id`, memverifikasi SOA authoritative secara terbatas, dan mensyaratkan *complete coverage* sebelum menghasilkan `authoritative_refused`. Versi ini **menggunakan kembali auditor tersebut secara read-only**.

## Fitur, default, dan batasan

| Opsi baru | Default | Fungsi |
|---|---|---|
| `--seed-selection sequential\|stratified` | `sequential` | `stratified`: satu indeks acak deterministik dari setiap strata indeks berukuran hampir sama, tersebar sepanjang `urls.json` |
| `--sampling-seed N` | `0` | Nomor stabil untuk mengulang sampel stratified; hanya bersama `--seed-selection stratified` |
| `--dns-audit-limit N` | `0` | Audit **maksimal 3** seed yang gagal pada pemeriksaan DNS `robots.txt`; hanya setelah `--scan-web` |
| `--dns-audit-timeout S` | `1.5` | Timeout **per DNS query** (0.2–3.0 detik); total audit dapat membutuhkan beberapa query |

- `stratified`, audit DNS, `--advanced-js` dan output review tetap **preview-only**. Kombinasi dengan `--apply` ditolak; `stratified` juga menolak `--start-index` agar tidak disalahartikan sebagai cursor sequential.
- Default sequential **tetap sama seperti v4.3**: indeks berurutan, cursor dan operasi `--apply` lama tidak diubah. Hanya JSON output tambahan yang muncul.
- Sampling mempertimbangkan **strata posisi dalam inventaris**, belum strata TLD (`.co.id`, `.web.id`, dsb.) atau kualitas DNS. Ini sampel tersebar yang dapat direproduksi, **bukan** estimasi statistik tentang ketersediaan situs. Hasil bergantung pada urutan dan digest isi `urls.json`; jika file berubah, indeks sampel juga dapat berubah.
- DNS audit terjadi **hanya** jika pemeriksaan `robots.txt` pada seed berakhir dengan `robots_dns` (`getaddrinfo` gagal). Kegagalan HTTP, TLS, dan homepage setelah robots berhasil tidak otomatis diaudit; status DNS atas domain yang tidak diaudit adalah `dns_resolution_failed_unverified`.
- Pemeriksaan DNS mendalam **tidak** mengakses situs dan tidak mengubah resolver server atau delegasi Registry, namun melakukan **query DNS publik langsung** ke nameserver otoritatif dan resolver referensi; perlakukan sebagai trafik jaringan terpisah.
- **Tidak ada write** ke SQLite: tidak mengubah `jobs.status`, `next_retry_at`, `dns_host_audits`, atau tabel lain. Tidak ada update otomatis pada `urls.json`, aksi registri, alert abuse, atau crawl domain global. Tidak ada pengubahan kebijakan retry HTTP dalam rilis ini.
- `dns_audit.py` memakai `dnspython>=2.6,<3` dan membutuhkan import `crawler_multi.py` (`requests`); kedua dependensi sudah merupakan bagian dari setup crawler Linux. Impor dijalankan hanya ketika audit opt-in benar-benar dibutuhkan.

### Kategori bukti DNS

| `dns_status` | Kriteria / interpretasi |
|---|---|
| `dns_lame_delegation` | Auditor mendapati referral child `.id`, cakupan authority lengkap, dan semua endpoint SOA `REFUSED` |
| `dns_lame_delegation_suspected` | Semua endpoint yang sempat diperiksa `REFUSED` tetapi cakupannya belum lengkap; **tidak terkonfirmasi** |
| `dns_audit_inconclusive` | Hasil campuran, `SERVFAIL`, audit parsial, bootstrap tidak lengkap atau bukti tidak cukup |
| `dns_delegated_ns_unresolvable` | NS delegasi tidak bisa di-resolve oleh auditor |
| `dns_authority_unreachable` | Semua endpoint SOA yang diperiksa tidak merespons |
| `dns_nxdomain_authoritative` | Parent authoritative memberi NXDOMAIN; tidak sama dengan `SERVFAIL` |
| `dns_no_direct_address_observed` | SOA authoritative tersedia, tetapi tidak ada A/AAAA langsung yang teramati; bukan pernyataan bahwa tidak ada CNAME |
| `dns_authoritative_address_observed` | SOA dan address ada, tetapi resolver web sebelumnya gagal; perlu analisis jalur resolver |
| `dns_audit_unavailable_or_error` | Dependency atau pemeriksaan DNS gagal; tidak ada detail exception sensitif di output |
| `dns_resolution_failed_unverified` | Seed gagal DNS, tetapi tidak diaudit karena fitur nonaktif atau limit habis |

**Jangan** menganggap `dns_lame_delegation` sebagai judol/abuse; ini diagnosis operasional DNS yang dapat berubah. Tidak ada pembekuan permanen domain.

## 1. Mac: terapkan patch incremental dari b5142e8

Unduh `bima-v4.3-to-v4.3.1-dns-aware-sampling.patch` ke `~/Downloads`.

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

git status --short
git log -1 --oneline
# HARUS b5142e8 dan working tree bersih.

git apply --check \
  ~/Downloads/bima-v4.3-to-v4.3.1-dns-aware-sampling.patch && \
git apply \
  ~/Downloads/bima-v4.3-to-v4.3.1-dns-aware-sampling.patch
```

Jangan gunakan `--reject`, `git reset --hard`, atau langsung mengubah kode di server.

## 2. Mac: tes (unit test baru tidak melakukan akses jaringan)

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima

PYTHONPATH="$PWD/multi:$PWD" \
  "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -p 'test_discovery_v431.py' -v

PYTHONPATH="$PWD/multi:$PWD" \
  "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -q

git diff --check
git status --short
```

**Hasil QA pada fixture lokal (dengan kode v4.3 upstream yang byte-for-byte cocok):** 30 tes baru dan 190 tes fixture keseluruhan berjalan; 189 lulus, 1 skip (fixture tidak memuat semua integrasi crawler). Pada repository Mac lengkap, jalankan semuanya; jumlah tes di Mac diharapkan sekitar **247** (217 sebelumnya + 30 baru), tetapi verifikasi hasil aktual sebelum commit.

## 3. Mac: commit dan push

```bash
git add -- \
  README_DISCOVERY_V431.md \
  multi/discovery_dns.py \
  multi/proactive_discovery.py \
  multi/tests/test_discovery_v431.py

git diff --cached --check
git diff --cached --stat

git commit -m "feat: add preview DNS evidence and deterministic .id seed sampling"
git push origin main
```

Hanya empat file ini yang perlu di-stage; **jangan** `git add .` ketika ada `urls.json`, `config.json`, SQLite, venv, atau bobot IndoBERT di working tree.

## 4. Linux server: pull + tests (sesudah push Mac)

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main
sudo -u fauzi -H git log -1 --oneline

sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover \
  -s multi/tests -p 'test_discovery_v431.py' -v

sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover -s multi/tests -q
```

**Tidak perlu restart `pandi-crawler-multi.service`** untuk perubahan Discovery ini. IndoBERT dan automatic retry v4.1 tetap berjalan.

## 5. Preview stratified, tanpa DNS audit mendalam (awal yang direkomendasikan)

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H ./venv/bin/python3 -B \
  multi/proactive_discovery.py \
  --input urls.json \
  --db multi/state/crawler_production.sqlite3 \
  --seed-selection stratified --sampling-seed 21 \
  --seed-limit 20 \
  --scan-web --http-fallback --canonical-www --advanced-js \
  --browser-review-limit 5 --redirect-chain-preview 5 \
  --diagnostic-seeds 10 \
  --max-candidates-per-seed 10 --max-sitemaps 1 \
  --probe-candidates 2 --timeout 6 --request-interval 0.5
```

Perhatikan `sampled_indices`, `candidate_urls`, `probed_pages`, `js_static_redirects`, `js_dynamic_pages`, `dns_failed_seeds` dan `dns_status_counts`. Ulangi command dengan seed yang sama untuk mereproduksi **pemilihan** domain, bukan menjamin respons DNS/HTTP sama pada waktu berbeda.

## 6. Preview DNS audit: maksimum 1 seed DNS-failed

Untuk tahap kedua, tambahkan pada perintah di atas:

```bash
  --dns-audit-limit 1 --dns-audit-timeout 1.5
```

Maksudnya tambahkan dua flag di akhir **baris perintah yang utuh**, bukan menjalankan baris tersebut sebagai shell command berdiri sendiri. `preview_dns_audits` akan menampilkan ringkasan tanpa jejak DNS mentah, IP NS, token, atau URL query.

Jika tidak ada seed yang gagal DNS, `dns_audits_performed` bisa 0; itu bukan bug. Untuk verifikasi kasus tertentu tanpa memasukkan hostname tersebut ke `urls.json` atau DB, gunakan auditor **read-only** existing:

```bash
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B - <<'PY'
import json
from dns_audit import audit_domain
from discovery_dns import summarize_dns_audit
host = 'asazuzuqidisa.web.id'
raw = audit_domain(host, timeout=1.5)
print(json.dumps(summarize_dns_audit(host, raw), indent=2))
PY
```

Audit bisa menghasilkan `dns_audit_inconclusive` meskipun query manual IPv4 menunjukkan kedua NS `REFUSED`: contoh jika verifikasi seluruh endpoint (termasuk IPv6 yang tidak terjangkau server) belum lengkap. Ini **disengaja**, bukan berarti pengamatan manual tadi salah.

## Output baru

```json
{
  "seed_selection": "stratified",
  "sampling_seed": 21,
  "start_index": null,
  "next_index": null,
  "sampled_indices": [1200, 83002],
  "dns_failed_seeds": 2,
  "dns_audits_performed": 1,
  "dns_status_counts": {
    "dns_audit_inconclusive": 1,
    "dns_resolution_failed_unverified": 1
  },
  "preview_dns_audits": [
    {
      "seed": "example.web.id",
      "dns_status": "dns_audit_inconclusive",
      "delegated_zone": "example.web.id.",
      "authority_rcodes": ["REFUSED", "NO_RESPONSE"],
      "authority_checks": 6,
      "coverage_complete": false,
      "authoritative_refused_observed": 4
    }
  ]
}
```

**Contoh ilustratif**, bukan data pemeriksaan sebenarnya. `sampled_indices` pada output asli berupa integer; jumlah item ditentukan `--preview-samples` dengan batas tampilan 20. Semua field lama v4.3 tetap tersedia.

## Rollback

Sebelum commit di Mac, jika hanya perubahan patch ini pada empat file:

```bash
git apply --reverse --check \
  ~/Downloads/bima-v4.3-to-v4.3.1-dns-aware-sampling.patch && \
git apply --reverse \
  ~/Downloads/bima-v4.3-to-v4.3.1-dns-aware-sampling.patch
```

Setelah commit/push, gunakan `git revert` terkontrol di Mac dan push/pull, bukan mengedit kode produksi langsung. Tidak ada migrasi database pada v4.3.1.
