# BIMA Dataset Collection v2.2 — kumpulkan, kurasi, lalu evaluasi bersama ChatGPT

Tahap ini **bukan** training otomatis, bukan perubahan model IndoBERT, bukan bagian
Registry Snapshot Auto-Rollover, dan tidak mengganti mekanisme backup. Tujuannya
memperbanyak contoh pembelajaran dari sumber yang berbeda secara terpisah.

## Sumber dataset

1. **BIMA crawler production (read-only)**: sampling reservoir `alert`, `complete`,
   dan `uncertain`, masing-masing default 50/100/50. Metadata model dan
   `review_excerpt` bila tersedia. Sampel `complete` atau `inconclusive` **belum
   diberi label negatif**. Semua contoh dari crawler dianggap kandidat review,
   bukan ground truth. Kasus Pre-Alert yang telah diputuskan manusia disertakan
   meski tidak terambil melalui sampling, tetapi keputusan itu hanya `site_label`.
2. **BIMA learning v1/v2 (read-only)**: hanya teks dengan label manusia yang
   eksplisit, bukti tersedia, dan hash bukti sesuai. Bukti duplikat berlabel
   bertentangan ditahan agar tidak menjadi contoh pelatihan.
3. **Korpus publik opsional**: dataset
   `0xRafie/indonesian-gambling-multimodal-dataset`, commit
   `9239598edc7f3d72becf7d7a105fcab6b06a22d6`, file **dataset.csv saja**
   (tidak mengunduh gambar ±938 MB). Lisensi yang dinyatakan di kartu dataset
   adalah **CC BY 4.0**. Atribusi: Ekaputra, D. R. (2026), *Indonesian Online
   Gambling Promotion Multimodal Dataset*, Hugging Face.
   URL sumber: https://huggingface.co/datasets/0xRafie/indonesian-gambling-multimodal-dataset

Korpus publik berisi teks *unggahan sosial media*, **bukan bukti langsung domain
.id**. Anotasi penyusun dataset dipertahankan sebagai `third_party_author_annotation`,
tidak otomatis dianggap label manusia yang diverifikasi PANDI. Jangan gabungkan
ke data **held-out test** BIMA; gunakan hanya sebagai kandidat tambahan untuk
train/validasi setelah audit dan verifikasi izin penggunaan serta sumber data.

## Instalasi patch

Di Mac BIMA:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
git status --short
git pull --ff-only origin main
PATCH="$HOME/Downloads/bima-dataset-collection-v22.patch"
git apply --check "$PATCH" && git apply "$PATCH"
git diff --check
PYTHONPATH="$PWD/multi:$PWD" python3 -B -m unittest discover \
  -s multi/tests -p 'test_dataset_collection_v22.py' -v
git add -- multi/learning/dataset_collection_v22.py \
  tools/bima_dataset_collect_v22.py \
  multi/tests/test_dataset_collection_v22.py \
  README_BIMA_DATASET_COLLECTION_V22.md
git diff --cached --check
git commit -m 'feat: collect labeled BIMA evidence and auxiliary corpus safely'
git push origin main
```

Di server (tidak perlu restart service):

```bash
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
 "$APP/venv/bin/python3" -B -m unittest discover \
 -s "$APP/multi/tests" -p 'test_dataset_collection_v22.py' -v
```

## Jalankan koleksi satu kali (crawler tetap berjalan)

```bash
APP=/home/fauzi/crawler-rebuild
OUT="/home/fauzi/bima-dataset-packs/collect-$(date -u +%Y%m%dT%H%M%SZ)"
sudo -u fauzi -H mkdir -p -m 700 /home/fauzi/bima-dataset-packs
sudo -u fauzi -H "$APP/venv/bin/python3" -B \
 "$APP/tools/bima_dataset_collect_v22.py" \
 --production-db "$APP/multi/state/crawler_production.sqlite3" \
 --learning-db "$APP/multi/state/bima_learning_v450.sqlite3" \
 --out "$OUT" --download-public
```

`--download-public` melakukan satu permintaan HTTPS ke Hugging Face untuk CSV
pinned. **Tidak** melakukan web request terhadap situs `.id`, dan tidak memuat
model. Jika koneksi ke Hugging Face gagal, ulangi tanpa `--download-public`
untuk membuat paket internal saja, dengan `OUT` baru. Untuk CSV yang sudah
tersedia secara lokal, ganti opsi menjadi `--public-csv /path/to/dataset.csv`.

Output dibuat di folder **baru** (0700), empat file (0600):

- `review_candidates.jsonl`: sekitar 200 kandidat dari crawler + semua Pre-Alert
  dengan label situs manusia. Dapat mencakup kandidat tanpa cuplikan bukti;
  ini dicatat sebagai `evidence_available=false`. **Tidak ada label teks otomatis**.
- `verified_bima.jsonl`: contoh teks BIMA v1/v2 yang sudah diberi **label teks
  oleh manusia**; teks di output hanya salinan dengan masking terbatas.
- `external_auxiliary.jsonl`: contoh yang terambil dari CSV publik, label dari
  pihak ketiga, bukan evaluasi independen BIMA. CSV yang rusak atau tidak sesuai
  struktur ditolak.
- `manifest.json`: rincian jumlah, provenance, kelas, identitas snapshot,
  peringatan privasi dan lisensi.

Paket bersifat **read-only terhadap SQLite** (walaupun SQLite crawler terus
berjalan), dan tidak mengubah review/alert, mengirim ulang POST, menghentikan
service, atau menjalankan training. Simpan paket secara privat, jangan `git add`
folder data, dan jangan upload seluruh SQLite atau konfigurasi rahasia.

## Bekerja bersama ChatGPT

Periksa potensi informasi pribadi, rahasia, dan teks yang tak semestinya dibagi.
Masking otomatis URL, email, telepon, dan token **tidak menjamin PII hilang
sepenuhnya**. Bila aman, unggah ke chat **manifest.json** serta **sample yang
terpilih dari ketiga JSONL**, bukan database produksi atau seluruh cuplikan
sensitif. ChatGPT dapat mengaudit label, menemukan pola false positive/false
negative, menyarankan tambahan sumber, merancang split berdasarkan domain,
dan membantu menyiapkan eksperimen offline yang dapat direproduksi.

**Ground truth BIMA tetap dari label teks manusia.** Usulan ChatGPT atau label
model otomatis diperlakukan sebagai *suggested_label* untuk diverifikasi;
tidak pernah dipromosikan sendiri menjadi label evaluasi. Harus ada audit
kebocoran data antar domain/halaman dan eksperimen independen sebelum fine-tuning
IndoBERT atau promosi model ke produksi.

## Keterbatasan

- Cuplikan `review_excerpt` tidak selalu tersedia; pengambilan snapshot dari
  DB tidak dapat mengembalikan full page HTML yang tidak disimpan crawler.
- Penanganan CSV publik dan contoh di README adalah langkah akuisisi; pengujian
  lisensi, kesesuaian domain, dan anotasi perlu dilakukan sebelum training.
- Sampler melewati baris `jobs` secara streaming. Pada DB 1,5 juta pekerjaan
  proses ini dapat mengambil waktu dan I/O; tidak menghentikan crawler.
- Output tidak otomatis memuat semua 1,5 juta URL. Kualitas sampel berlabel
  lebih penting daripada memindahkan seluruh database ke training.
- Pengujian patch dilakukan pada SQLite sintetis; validasi di repository dan
  server tetap diperlukan.
