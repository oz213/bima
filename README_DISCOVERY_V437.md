# BIMA v4.3.7 — Priority after Telegram `/cek` (Google/urlscan; Brave reserved)

## Perubahan keputusan antrean

**Kebijakan:** pemeriksaan Telegram `/cek` adalah prioritas pertama; job dari sumber Discovery berada di jalur prioritas kedua; baseline Registry `urls.json` adalah jalur reguler. Agar Registry tidak kehabisan giliran, dispatcher memakai pembagian **maksimal 4 job Discovery lalu 1 job Registry** apabila kedua antrean memiliki pekerjaan `pending`. Due DNS/HTTP retries tetap mendapat slot terbatas sesuai `RetryPacer` (biasanya 1/5 giliran non-Telegram, maksimal 10/menit). Telegram selalu dapat mendahului keduanya.

**Brave:** kode jalur prioritas siap menandai penyedia `brave`, tetapi **pengambilan dan penyimpanan URL hasil Brave tetap terlarang/tidak aktif** dalam implementasi sekarang karena pembatasan ToS yang belum mendapat izin khusus. Jangan mengubah `multi_source_discovery.py` untuk menulis URL Brave ke JSONL, SQLite atau memasukkan URL Brave ke antrean. Saat ini sumber yang benar-benar bisa dinaikkan prioritasnya hanya **Google dan urlscan**, yang sudah disetujui di whitelist importer v4.3.6. Pengambilan URL Brave baru dapat diintegrasikan setelah izin dan desain kepatuhan diverifikasi. Kode ini tidak otomatis mengaktifkan Brave di cron.

### Alur

```text
/cek Telegram (tertinggi)
   |
   v
Discovery: Google / urlscan, 4 giliran (high priority)
   |   \-- Brave: reserved only, not ingested
   v
Registry baseline jobs, 1 giliran (fairness)
   |
   +-- retry DNS/HTTP jatuh tempo: 1 per 5 dispatch, dengan rate limit
   v
BIMA existing HTTP-first, shared IndoBERT + browser fallback + BIMA evidence
```

Tidak ada perubahan pada IndoBERT, `urls.json`, `domains.json`, `crawler_google.py`, Telegram monitor, ataupun rutinitas registri. Aplikasi tetap menggunakan **database SQLite produksi yang sama** dan tabel `jobs` lama. Modul baru membuat **tabel metadata kecil** `discovery_job_priority` **hanya pada `discovery_queue.py --apply --acknowledge-queue-write`**; preview tidak membuat tabel. Tabel ini menyimpan `job_id`, provider, dan waktu; tidak menyimpan respons API ataupun menambah URL Brave. `jobs.status`/`attempts`/`reason`/`classifier_json` untuk job yang sudah ada tidak diubah oleh importer.

## Pengamanan dan batas

- `multi/discovery_queue.py` tetap **preview/read-only by default**, hanya memproses hasil Google dan urlscan yang sudah ada di `/home/fauzi/bima-logs/multi-source.jsonl`.
- `--apply --acknowledge-queue-write` masih wajib untuk membuat job dan metadata prioritas. **Ini adalah write ke produksi; crawler dapat langsung memproses/alert setelah dispatcher baru aktif.** Tidak ada otomatisasi apply pada cron.
- `--max-new` maksimum **100** per panggilan, default **20**; `--max-priority` maksimum **100** job Discovery aktif, default **20**. Metadata dari job terminal dibersihkan secara bertahap saat importer apply berikutnya.
- Jika domain sudah ada dalam `jobs` berstatus `pending`, importer dapat menambahkan **tag prioritas pada job yang sama**, tanpa menambahkan job kedua atau mengubah hasil pemeriksaan. Status terminal seperti `alerted`, `complete`, `pending_alert` tidak diaktifkan ulang.
- Batas default `--max-pending 10000` masih menahan pembuatan job baru saat backlog Registry besar. Bila operator sudah memeriksa metrik dan memutuskan prioritas diperlukan walaupun melewati batas, tambahkan **`--allow-priority-over-pending-ceiling` secara eksplisit**; tetap wajib `--apply --acknowledge-queue-write`, dan tetap tunduk pada `--max-new` serta `--max-priority`.
- Existing `/cek` tetap prioritas pertama dalam `choose_next_job`. Jika service tidak memakai `--telegram-checks`, fitur Telegram juga perlu diatur pada service; patch ini tidak mengubah opsi service.
- `RetryPacer` tetap mengatur due retries: job Discovery dihitung sebagai giliran non-Telegram untuk mencegah retry tertunda tanpa batas.
- Metadata ditahan ketika status `http_inflight`/`retry_http` agar bisa pulih saat job direqueue setelah crash. Tidak ada reset database.

## Mac → GitHub → server (patuhi prosedur PANDI)

**Pilih SATU patch sesuai versi repository Mac:**

1. Jika `main` Mac masih v4.3.5 (`fc97ce6`), gunakan **patch gabungan v4.3.5→v4.3.7**; jangan terapkan patch v4.3.6 terpisah terlebih dahulu.
2. Jika patch v4.3.6 sudah diterapkan di Mac, gunakan **patch incremental v4.3.6→v4.3.7**.

Setelah Git patch berhasil, source penuh `crawler_multi.py` pada Mac harus diperbarui melalui alat aman yang memeriksa empat anchor dispatch. Jalankan:

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
python3 tools/upgrade_discovery_priority_crawler.py --check
python3 tools/upgrade_discovery_priority_crawler.py --apply
PYTHONPATH="$PWD/multi:$PWD" "$HOME/.venvs/bima-unit/bin/python3" -B \
  -m unittest discover -s multi/tests -q
git diff --check
git status --short
```

Installer menolak source yang berbeda/terpasang sebagian; `--check` tidak mengubah file, `--apply` hanya mengubah empat anchor, tidak menulis SQLite. Pastikan semua diff Git hanya source yang dimaksud sebelum commit. **Jangan menjalankan script installer ini pada server.** Staging yang dianjurkan:

```bash
git add -- README_DISCOVERY_V436.md README_DISCOVERY_V437.md \
  multi/discovery_queue.py multi/discovery_priority.py \
  multi/network_retry_queue.py multi/crawler_multi.py \
  multi/tests/test_discovery_queue_v436.py \
  multi/tests/test_discovery_priority_v437.py \
  tools/upgrade_discovery_priority_crawler.py

git diff --cached --check
git commit -m "feat: prioritize discovered jobs after Telegram checks"
git push origin main
```

Pada server:

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H git status --short
sudo -u fauzi -H git pull --ff-only origin main
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B -m unittest discover -s multi/tests -q
```

Pertahankan ` M crawler_google.py` dan `?? domains.json`; tidak ada `git reset --hard`, `git clean`, `git add .`, perubahan Chrome sandbox, atau restart otomatis. **Proses crawler yang sudah berjalan masih memakai kode lama sampai di-restart secara terkendali**, sehingga belum ada efek prioritas di runtime hanya karena `git pull` berhasil. Jadwalkan restart terkontrol setelah tes server dan rencana rollback dipastikan; sebelum itu jangan menggunakan importer `--apply` pada database live.

## Audit prioritas read-only (SEBELUM APPLY)

Setelah cron harian berhasil menghasilkan JSONL:

```bash
cd /home/fauzi/crawler-rebuild
sudo -u fauzi -H env PYTHONPATH="$PWD/multi:$PWD" \
  ./venv/bin/python3 -B multi/discovery_queue.py \
  --input /home/fauzi/bima-logs/multi-source.jsonl \
  --db multi/state/crawler_production.sqlite3 \
  --domains domains.json \
  --max-new 5 --max-priority 20 --max-pending 10000 \
  | ./venv/bin/python3 -m json.tool
```

Output penting: `pending_before`, `would_promote_existing`, `would_insert`, `pending_discovery_priority`, `blocked_by_pending_limit`, `excluded_brave_entries`. Bila `pending_before` besar, boleh menguji **preview** tambahan dengan `--allow-priority-over-pending-ceiling` tanpa write; **jangan langsung `--apply`**.

## Pengujian dan batas validasi

Fixture lokal menjalankan 342 tes, 341 lulus dan 1 tes lama dilewati. Installer crawler dicek dengan anchor exact terhadap `crawler_multi.py` pada commit v4.3.5 GitHub. Karena fixture lokal hanya memuat fragmen sumber crawler, **syntax/diff dari file crawler penuh harus diuji di Mac dan hasil pengujian server perlu dikirim sebelum restart**. Tidak ada API call Brave, Google, urlscan, Telegram, BIMA, maupun penulisan SQLite produksi pada pengujian fixture.

Unit test baru secara khusus mencakup prioritas `/cek`, fairness 4:1, retry pacing, import yang mempromosikan job Registry yang sudah `pending`, idempotensi, batas antrean, dan penolakan URL Brave.
