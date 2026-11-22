# BIMA Dataset Evidence Enrichment v2.3 — ambil bukti untuk kandidat review

Masalah: pada pack v2.2, `candidates_with_excerpt` dapat bernilai **0** karena
`multi/crawler_multi.py` hanya menyimpan `review_excerpt` jika
`BIMA_REVIEW_EXCERPTS=1` **dan** klasifikasi medium/high. Karena 206 kandidat
sudah dipilih, v2.3 mengambil ulang halaman secara **terbatas, serial,
read-only terhadap SQLite**. Tidak perlu menunggu PDNS/Registry cycle baru.

## Penting: catatan bukti dan label

- Hasil yang diambil ulang adalah **observasi HTTP baru** (`evidence_source`
  `live_http_refetch_v23`, `evidence_observed_at`). Ini tidak membuktikan bahwa
  halaman yang sama tampil ketika reviewer Telegram memilih keputusan situs.
- Status alert/Telegram tetap menjadi *site verdict*; teks baru belum boleh
  mendapat label otomatis, walaupun situs itu pernah divonis perjudian.
- `training_approved=false`, `text_label=null`. Model IndoBERT, alert,
  database crawler/learning, `urls.json`, semua backup dan service tidak diubah.
- Teks halaman publik masih mungkin mengandung PII meskipun redaksi URL, IP,
  email, telepon, dan token dilakukan. Tinjau paket sebelum membagikan.
- Halaman JS-only/CAPTCHA/redirect/HTTPS error dapat tidak menghasilkan bukti;
  tidak berarti domain itu aman.

## Keamanan akuisisi

- Maksimal 250 host **berbeda** per sekali jalan, default 60, **serial**.
  Gunakan `--offset 60` untuk batch berikutnya tanpa mengulang 60 kandidat pertama.
- Mengambil **URL asli** dari `jobs` pada SQLite read-only, bukan menjalankan
  URL arbitrer yang ditulis di paket JSONL. Host harus identik dengan kandidat.
- Reuse `crawler_multi.fetch_http` untuk validasi target dan setiap redirect,
  port HTTP(S) standar, respons streaming dengan batas **512 KiB**, timeout
  koneksi/baca 5/9 detik, jeda host minimal 1 detik.
- Modul sendiri hanya mengekstrak visible text sederhana dengan `HTMLParser`;
  tanpa Selenium, IndoBERT, Torch, model, atau API alert. `fetch_http` masih
  mengandalkan egress firewall server untuk mencegah DNS rebinding antara
  pemeriksaan DNS dan koneksi jaringan.
- Output baru mode 0700, file JSONL/manifest mode 0600, tanpa menimpa paket
  sebelumnya atau menulis database produksi.

## Pasang patch incremental di Mac

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git pull --ff-only origin main
PATCH="$HOME/Downloads/bima-dataset-evidence-enrichment-v23.patch"
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_dataset_enrichment_v23.py' -v

git add -- multi/learning/dataset_enrichment_v23.py \
  tools/bima_dataset_enrich_v23.py \
  multi/tests/test_dataset_enrichment_v23.py \
  README_BIMA_DATASET_EVIDENCE_ENRICHMENT_V23.md

git diff --cached --check
git commit -m "feat: enrich BIMA review candidates with bounded live evidence"
git push origin main
```

## Jalankan di server setelah `git pull` dan tests lulus

Pastikan host memiliki egress firewall yang membatasi akses ke private address
(IPv4 dan IPv6), dan jalankan hanya dalam jaringan sesuai kebijakan PANDI.

```bash
APP=/home/fauzi/crawler-rebuild
PACK=/home/fauzi/bima-dataset-packs/collect-20261122T125650Z
OUT=/home/fauzi/bima-dataset-packs/enriched-$(date -u +%Y%m%dT%H%M%SZ)
TOOL="$APP/tools/bima_dataset_enrich_v23.py"

# Preview offline, tanpa fetch dan tanpa membuat output
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" \
  --pack "$PACK" --production-db "$APP/multi/state/crawler_production.sqlite3" \
  --out "$OUT" --limit 60

# Mulai fetch eksplisit, hanya 60 domain maksimum
sudo -u fauzi -H "$APP/venv/bin/python3" -B "$TOOL" \
  --pack "$PACK" --production-db "$APP/multi/state/crawler_production.sqlite3" \
  --out "$OUT" --limit 60 --fetch

# Batch kedua bila diperlukan: gunakan OUT berbeda dan tambah --offset 60
```

Output di `$OUT`: `enriched_review_candidates.jsonl`, `enrichment_manifest.json`.
Status mencatat berapa yang mendapat bukti, yang gagal HTTP, dan URL yang tidak
cocok terhadap `jobs`. Tidak ada kasus yang otomatis masuk data latih.

## Kerja bersama ChatGPT

Unggah manifest dan **subset** dari `enriched_review_candidates.jsonl` setelah
pemeriksaan PII; ChatGPT dapat membantu mengusulkan `suggested_label`, alasan
bukti, dan prioritas kasus reviewer. Untuk model training, bawa usulan tersebut
ke pemeriksaan manusia; label dari site verdict atau model tidak langsung
menjadi label teks. Jangan unggah file privat berisi token/kredensial atau DB.

## Tahap selanjutnya

Setelah validasi hasil enrichment, aktifkan **capture konten selektif saat
crawling** untuk menghindari re-fetch historis. Simpan observasi dengan
provenance dan retensi terbatas, tidak semua 1,5 juta halaman. Pipeline ini
juga harus mencakup negative/hard-negative seperti berita dan fact-checking
mengenai judi online, bukan hanya alert positif.
