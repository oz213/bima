# BIMA Live IndoBERT Shadow v1 (opt-in)

**Tujuan:** mulai memakai checkpoint IndoBERT kandidat **sebagai shadow model**, tanpa mengganti model/keputusan alert produksi. Patch hanya menambah pengiriman sampel teks dari `classify_shared()`, worker independen, dan unit tests. Tidak mengubah schema DB, BIMA API, Telegram, Selenium, konfigurasi default, atau aturan klasifikasi.

Basis integrasi diperiksa pada repository `oz213/bima` branch `main` (23 November 2026), khususnya `multi/crawler_multi.py`. Jalur produksi `crawler_url.py` tetap menjadi satu-satunya pembuat keputusan. Checkout produksi harus diperiksa ulang bila berbeda dari branch tersebut.

## Prinsip operasional

- **Default OFF**. Tanpa `BIMA_INDOBERT_SHADOW_CAPTURE=1`, tidak ada perubahan perilaku, dependensi, atau tambahan model.
- Saat aktif, classifier lama selesai **lebih dahulu**. Hook mencoba merekam sampel yang memang memanggil IndoBERT lama. Kegagalan capture **fail-open**: tidak mengubah hasil klasifikasi, routing, atau alert.
- Capture deterministik per URL, default **1%**, maksimal **10%**. Pending spool maksimal 250 JSON. Crawler tidak menunggu model kandidat dan tidak pernah memuatnya.
- Raw page text ditulis sementara dalam folder privat (0700, file 0600), maksimal 16.000 karakter per observasi; hasil audit berisi **metadata dan probabilitas saja**, tanpa teks asli. Queue item dihapus setelah sukses ditulis. **Jangan commit queue/checkpoint ke Git**.
- Worker menjalankan checkpoint kandidat terpisah, memuat model sekali per proses, default **CPU, 1 Torch thread**. Preflight minimal MemAvailable 1.500 MiB pada Linux. Karena server BIMA hanya sekitar 3,8 GiB RAM, **jangan hidupkan worker tanpa memeriksa RAM dan sumber daya browser terlebih dahulu**.
- Rule chunking: salin metode `split_text_for_indobert` (batas 1.800 char, maksimal delapan chunk), gabungkan chunk terakhir apabila <32 karakter; per-chunk inference max 256 tokens; agregasi top-2 konservatif. Hasil ini **bukan** probabilitas kalibrasi atau label manusia. Jika checkpoint MPS Mac memiliki tokenizer/versi berbeda dari server, bandingkan output yang sama sebelum menarik kesimpulan.
- BIMA hidden-link alert yang langsung bypass klasifikasi teks tidak ikut sampel, tidak dikategorikan sebagai negative/false-negative.

## A. Mac: pasang patch & uji tanpa menjalankan crawler

Paling mudah, ekstrak `BIMA_LIVE_SHADOW_V1.zip` di Downloads lalu jalankan:

```bash
cd ~/Downloads && unzip -oq BIMA_LIVE_SHADOW_V1.zip && bash BIMA_LIVE_SHADOW_V1/JALANKAN_DI_MAC.command
```

Script **hanya** memasang patch dan menjalankan test; tidak melakukan push, restart, atau mengubah model produksi.

Alternatif manual: simpan `bima-live-indobert-shadow-v1.patch` di Downloads, lalu:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
PATCH="$HOME/Downloads/bima-live-indobert-shadow-v1.patch"
git apply --check "$PATCH" && git apply "$PATCH"
python3 -B -m unittest discover -s multi/tests -p 'test_indobert_shadow_live_v1.py' -v
git diff --check
```

**Jika unit test lolos**, review changes dan commit/push secara sadar (jangan commit checkpoint/data sensitif):

```bash
git status --short
git add -- multi/crawler_multi.py multi/shadow_capture_v1.py \
  tools/bima_indobert_shadow_live_v1.py tools/bima_shadow_summary_v1.py \
  multi/tests/test_indobert_shadow_live_v1.py README_BIMA_LIVE_INDOBERT_SHADOW_V1.md
git diff --cached --check
git commit -m 'feat: add opt-in read-only IndoBERT shadow sampling'
git push origin main
```

Jika repository punya perubahan lain yang belum dipush, **jangan overwrite/stash/reset**. Pastikan patch berada pada commit yang sesuai dan unit test hijau sebelum push.

## B. Server: kode dan checkpoint kandidat

Jangan copy checkpoint ke repo Git. Copy **checkpoint kandidat lengkap** (`config.json`, tokenizer dan weight files) dari Mac ke folder privat Linux. Sesuaikan akses SSH dan path:

```bash
# Di Mac (contoh: ganti SERVER dan direktori sesuai lingkungan):
SERVER=fauzi@10.1.11.51
MODEL="$HOME/BIMA-AI/runs/indobert-20261122T202156Z/best_candidate"
ssh "$SERVER" 'mkdir -p -m 700 /home/fauzi/bima-shadow/models/candidate'
rsync -av --chmod=Du=rwx,Dgo=,Fu=rw,Fgo= "$MODEL/" \
  "$SERVER:/home/fauzi/bima-shadow/models/candidate/"
```

Di server, **setelah commit dipush dan perubahan lain diperiksa**:

```bash
APP=/home/fauzi/crawler-rebuild
sudo -u fauzi -H git -C "$APP" status --short
sudo -u fauzi -H git -C "$APP" pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$APP/multi:$APP" \
  "$APP/venv/bin/python3" -B -m unittest discover \
  -s "$APP/multi/tests" -p 'test_indobert_shadow_live_v1.py' -v
sudo -u fauzi -H mkdir -p -m 700 \
  /home/fauzi/bima-shadow/spool /home/fauzi/bima-shadow/audit
```

## C. Enable sampling — **terpisah dari model produksi**

Untuk proses crawler, tambahkan environment **melalui mekanisme unit systemd yang sudah ada**, jangan ubah `config.json` model, token, atau parameter worker:

```ini
[Service]
Environment=BIMA_INDOBERT_SHADOW_CAPTURE=1
Environment=BIMA_INDOBERT_SHADOW_RATE=0.01
Environment=BIMA_INDOBERT_SHADOW_PENDING_LIMIT=250
Environment=BIMA_INDOBERT_SHADOW_SPOOL_DIR=/home/fauzi/bima-shadow/spool
```

Lakukan restart crawler **hanya pada maintenance window** setelah validasi/rollback tersedia. Pastikan log `IndoBERT loaded ONCE` model lama tetap muncul. **Tidak perlu restart Telegram atau alert sender**.

Periksa secara read-only:

```bash
sudo -u fauzi -H sh -c 'find /home/fauzi/bima-shadow/spool -maxdepth 1 -type f -name "*.json" | wc -l'
```

## D. Jalankan kandidat secara terpisah (uji 10 event dahulu)

```bash
APP=/home/fauzi/crawler-rebuild
MODEL=/home/fauzi/bima-shadow/models/candidate
sudo -u fauzi -H "$APP/venv/bin/python3" \
  "$APP/tools/bima_indobert_shadow_live_v1.py" \
  --spool /home/fauzi/bima-shadow/spool \
  --out /home/fauzi/bima-shadow/audit \
  --model "$MODEL" --once --max-events 10 \
  --device cpu --torch-threads 1
```

Jika sudah dipastikan RAM aman dan hasil benar, jalankan sebagai **service terpisah**, misalnya `pandi-indobert-shadow.service`, dengan `User=fauzi`, env terbatas, `Restart=on-failure`, dan command worker **tanpa** `--once`, **dengan `--max-events 0`** (loop memuat model sekali). Jangan restart crawler untuk mengubah worker shadow.

Laporan audit dan ringkasan otomatis:

```bash
sudo -u fauzi -H tail -n 5 /home/fauzi/bima-shadow/audit/shadow_comparisons_v1.jsonl
sudo -u fauzi -H "$APP/venv/bin/python3" \
  "$APP/tools/bima_shadow_summary_v1.py" \
  --audit /home/fauzi/bima-shadow/audit/shadow_comparisons_v1.jsonl
```

## Rollback segera

1. **Worker:** stop service shadow saja atau hentikan proses pengujian; model produksi tidak berubah.
2. **Capture:** set `BIMA_INDOBERT_SHADOW_CAPTURE=0` pada environment crawler pada maintenance window berikutnya (lalu restart crawler sesuai prosedur). Event yang sudah ada tetap privat; jangan hapus data tanpa keputusan retensi.
3. Tidak ada migrasi DB, checkpoint produksi, Telegram, atau konfigurasi alert untuk dikembalikan.

## Batasan evaluasi

- Data output **tidak memiliki ground-truth**: disagreement tidak otomatis berarti false positive/negative.
- Entri teks dapat dipotong menjadi 16k karakter di spool; field `input_capped` menandai kasusnya.
- Event gagal yang tersisa di spool perlu investigasi operator. Jika melebihi pending cap, capture berhenti menyimpan sampai antrean berkurang; produksi tetap lanjut.
- Output JSONL memiliki `event_id` untuk deduplikasi; jika proses terputus setelah append namun sebelum menghapus event, observasi bisa tercatat dua kali.
- Capture 1% dari URL yang **menggunakan IndoBERT**, bukan sampel acak semua URL; jangan gunakan hit rate sebagai estimasi prevalensi/akurasi produksi.
