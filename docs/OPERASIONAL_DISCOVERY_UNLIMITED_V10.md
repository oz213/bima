# BIMA Proactive Discovery: import seluruh kandidat valid (v10)

Perubahan v10 menghapus batas **volume import** `max-new=5`, antrean prioritas
`max-priority=20`, dan batas backlog `max-pending=10000` pada pekerjaan cron.
Nilai `0` berarti **tanpa pembatas jumlah**, bukan 0 pekerjaan. Perintah import
langsung juga memakai default tersebut. Pembatas positif tetap tersedia
jika operator perlu throttle manual.

**Tetap aman:** validasi domain `.id`, deduplikasi URL, larangan pemuatan ulang
job berstatus terminal, keamanan spool, pembatas ukuran berkas/rekaman, transaksi
SQLite, serta fairness dispatch `Telegram > Discovery/Registry` tidak berubah.
Tidak ada retry jaringan baru dan IndoBERT tidak disentuh.

**Tidak termasuk:** kuota API Google/URLScan/Brave, jumlah hasil yang disediakan
masing-masing provider, atau aturan penggunaan data Brave. Karena alasan
kebijakan sumber, URL Brave tetap **tidak** diimpor. Eksekusi cron dan interval
hari tetap mengikuti konfigurasi privat yang ada, termasuk `2` hari bila disetel.
Batas ukuran spool per file tetap merupakan pengaman terhadap input rusak/
malicious, bukan kuota jumlah pekerjaan per hari.

## Deploy Mac ke GitHub

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/BIMA/bima
python3 ~/Downloads/bima_discovery_unlimited_v10_generate.py \
  --repo . --output ~/Downloads/bima-discovery-unlimited-v10.patch
git apply --check ~/Downloads/bima-discovery-unlimited-v10.patch
git apply ~/Downloads/bima-discovery-unlimited-v10.patch
PYTHONPATH=multi:multi/tests python3 -m unittest discover \
  -s multi/tests -p 'test_discovery*_v*.py' -v
PYTHONPATH=multi:multi/tests python3 -m unittest discover \
  -s multi/tests -p 'test_daily_discovery_v438.py' -v
PYTHONPATH=multi:multi/tests python3 -m unittest discover \
  -s multi/tests -p 'test_discovery_unlimited_v10.py' -v
git diff --check
git add -- multi/discovery_queue.py multi/daily_discovery_import.py \
  tools/run_multisource_daily.sh multi/tests/test_daily_discovery_v438.py \
  multi/tests/test_discovery_unlimited_v10.py \
  docs/OPERASIONAL_DISCOVERY_UNLIMITED_V10.md
git commit -m "feat: import all eligible proactive discovery candidates"
git push origin main
```

## Server: pull dan uji

```bash
REPO=/home/fauzi/crawler-rebuild
runuser -u fauzi -- git -C "$REPO" pull --ff-only origin main
cd "$REPO"
runuser -u fauzi -- env PYTHONPATH=multi:multi/tests \
  "$REPO/venv/bin/python3" -m unittest discover \
  -s multi/tests -p 'test_discovery_unlimited_v10.py' -v
runuser -u fauzi -- env PYTHONPATH=multi:multi/tests \
  "$REPO/venv/bin/python3" -m unittest discover \
  -s multi/tests -p 'test_daily_discovery_v438.py' -v
```

## Cek sisa spool (read-only, tanpa request provider)

```bash
REPO=/home/fauzi/crawler-rebuild
runuser -u fauzi -- env PYTHONPATH="$REPO/multi" \
  "$REPO/venv/bin/python3" -B "$REPO/multi/daily_discovery_import.py" \
  --spool-dir /home/fauzi/bima-logs/multi-source-pending \
  --db "$REPO/multi/state/crawler_production.sqlite3" \
  --domains "$REPO/domains.json" \
  --max-new 0 --max-priority 0 --max-pending 0
```

## Manual import seluruh sisa spool (tindakan tulis ke DB produksi)

Lakukan hanya setelah memeriksa preview dan backup terverifikasi terkini.
Perintah di bawah **tidak memanggil provider**, tidak men-trigger crawler
kedua, tidak mengirim alert BIMA, dan tidak memodifikasi `urls.json`.

```bash
REPO=/home/fauzi/crawler-rebuild
runuser -u fauzi -- env PYTHONPATH="$REPO/multi" \
  "$REPO/venv/bin/python3" -B "$REPO/multi/daily_discovery_import.py" \
  --spool-dir /home/fauzi/bima-logs/multi-source-pending \
  --db "$REPO/multi/state/crawler_production.sqlite3" \
  --domains "$REPO/domains.json" \
  --max-new 0 --max-priority 0 --max-pending 0 \
  --apply --acknowledge-queue-write
```

**Catatan NFS:** import menulis SQLite produksi di NFS; perhatikan locking,
latensi dan ketersediaan backup. Jangan menggunakan `git reset --hard`,
`git clean`, atau mengubah symlink NFS. Jumlah URL pada Registry Snapshot
Telegram `/status` tetap menunjuk dataset Registry, sedangkan domain Discovery
berada di jalur prioritas terpisah.
