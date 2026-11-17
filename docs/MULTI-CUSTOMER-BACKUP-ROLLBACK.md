# Backup & Rollback — Transcendence Multi-Customer

## 1. Backup harian (cron di VPS — temuan review N7)

Model SATU USER: cron berjalan sebagai user **deploy** — user yang sama
dengan jalur pra-deploy — menulis ke direktori yang sama
(`/var/backups/transcendence`, `deploy:deploy` 750), memakai pola
validasi yang sama dengan `backup_db.sh`. User `postgres` TIDAK PERNAH
menulis ke direktori backup; dump dibaca via peer-auth
`sudo -u postgres pg_dump` (tanpa kredensial di file cron). Cron lama
(sebagai `postgres`, tanpa pipefail/validasi) gagal senyap pada dir 750
milik deploy dan telah DIGANTI:

```bash
# /etc/cron.d/transcendence-backup
SHELL=/bin/bash
15 2 * * * deploy DB_NAME=transcendence BACKUP_DIR=/var/backups/transcendence RETENTION_DAYS=30 /opt/transcendence-hlwon-/backend/deploy/backup_daily.sh >> /var/backups/transcendence/daily-cron.log 2>&1
```

- Skrip: `backend/deploy/backup_daily.sh` — `set -euo pipefail`, dump
  ke `.part` (umask 077 → 600), validasi tiga lapis (`gzip -t` + tidak
  kosong + header dump PostgreSQL), publikasi atomik `mv`, `chmod 600`.
  Pointer `LAST_DAILY` juga ditulis atomik: `printf` ke
  `.LAST_DAILY.part` di direktori yang sama (satu filesystem) lalu
  `mv` — rename POSIX atomik; kegagalan di tengah penulisan tidak
  pernah meninggalkan pointer kosong/terpotong (pembaca selalu
  melihat isi lama ATAU baru yang utuh).
  Dites pytest: `backend/tests/test_backup_daily_script.py`.
- GAGAL (pg_dump gagal/kosong/bukan-PG/gzip korup/dir tidak sesuai
  owner-mode) → exit non-zero, TIDAK ada file final, `LAST_DAILY` tidak
  berubah, `.part` dibersihkan, penyebab tertulis di `daily-cron.log`.
- `LAST_DAILY` = stamp `daily-YYYYmmdd-HHMMSS` backup harian valid
  terakhir — informasi operasi (pantau usianya; cron tidak mengirim
  email bila MAILTO tidak dikonfigurasi).
- Retensi: hanya `daily-*.sql.gz` lebih tua dari `RETENTION_DAYS`
  (default 30) yang dihapus — `pre-deploy-*`, `manual-*`, dan file
  kontrak stamp (`LAST_*`/`APPROVED_COMMIT`/`PREVIOUS_COMMIT`) tidak
  pernah disentuh.
- Log cron `daily-cron.log` dirotasi dengan logrotate — JANGAN
  dibiarkan tumbuh tanpa batas di direktori backup 750 (kehabisan
  disk ikut mematikan backup berikutnya). Pasang kebijakan bawaan
  repo sebagai `/etc/logrotate.d/transcendence-backup`:
  ```bash
  install -o root -g root -m 644 \
    /opt/transcendence-hlwon-/backend/deploy/logrotate-transcendence-backup \
    /etc/logrotate.d/transcendence-backup
  logrotate --debug /etc/logrotate.d/transcendence-backup   # uji kering
  ```
  Kebijakan: `weekly` + `maxsize 5M`, `rotate 12`, `dateext`,
  `compress`, `copytruncate` (cron menulis via `>>` pada tiap run,
  tidak ada daemon pemegang fd — tanpa postrotate), `missingok`,
  `notifempty`, `su deploy deploy` (arsip ikut milik deploy, bukan
  root). File: `backend/deploy/logrotate-transcendence-backup`.
- Prasyarat: direktori terprovisioning (§10.1 di
  MULTI-CUSTOMER-INSTALASI-PROVISIONING.md) + sudoers minimal deploy
  (§10.4 dokumen yang sama): hanya `sudo -u postgres pg_dump
  transcendence`. Preflight izin dilakukan TANPA sudo dan gagal jelas
  bila owner/mode menyimpang.

## 2. Backup pra-deploy (otomatis + TERNVALIDASI)

Workflow `.github/workflows/backend-deploy.yml` menjalankan
`backend/deploy/backup_db.sh` (dikirim via `bash -s`) sebelum tiap
deployment. Skrip memakai `set -euo pipefail` sehingga kegagalan
`pg_dump` maupun `gzip` menghentikan deployment — tidak ada lagi dump
kosong/terpotong yang lolos. Hasil ditulis ke file sementara `.part`,
divalidasi berlapis (`gzip -t` + tidak kosong + header dump PostgreSQL),
baru dipublikasi atomik ke
`/var/backups/transcendence/pre-deploy-<STAMP>.sql.gz`.

**Stamp backup unik per run (audit round-5 #2).** STAMP bukan lagi
timestamp detik (dua run/re-run pada detik sama bisa identik — file
saling menimpa). Sekarang stamp = identifier unik yang memuat
`github.run_id` dan `github.run_attempt`:

```
STAMP="run-<run_id>-attempt-<run_attempt>"
# contoh: pre-deploy-run-1892374561-attempt-1.sql.gz
```

Dibuat `backend/deploy/make_backup_stamp.sh` (validasi numerik +
filename-safe `[0-9A-Za-z_-]`, sama dengan gate backup_db.sh dan
rollback.sh). `run_id` unik per workflow run; `run_attempt` naik pada
tiap re-run → stamp selalu unik dan re-run tidak pernah menimpa backup
run sebelumnya.

`LAST_PRE_DEPLOY` hanya diperbarui SETELAH backup tervalidasi — isinya
selalu stamp backup valid terakhir (informasi operasi, bukan dasar
keputusan rollback; lihat §5).

Backup manual (psikologis sama dengan langkah skrip — dump, validasi,
baru dipercaya):

```bash
sudo -u postgres pg_dump transcendence | gzip > /var/backups/transcendence/manual-$(date +%Y%m%d-%H%M%S).sql.gz
gzip -t /var/backups/transcendence/manual-*.sql.gz   # validasi sebelum dipercaya
```

## 3. Restore / rollback database (manual, sadar stamp)

Identifikasi dump yang SATU PASANGAN dengan kondisi yang ingin
dikembalikan — JANGAN asal pakai dump terbaru bila dump itu bukan
pasangan pra-deploy run yang relevan (audit round-4: me-restore dump
lama bisa MENGHAPUS data yang lebih baru):

```bash
# stamp backup valid terakhir (informasi):
cat /var/backups/transcendence/LAST_PRE_DEPLOY
# dump milik run deploy tertentu:
DUMP=/var/backups/transcendence/pre-deploy-<STAMP>.sql.gz
gzip -t "$DUMP"                                        # validasi dulu
sudo -u postgres dropdb --if-exists transcendence_restore
sudo -u postgres createdb transcendence_restore
gunzip -c "$DUMP" | sudo -u postgres psql transcendence_restore
# cek dulu, lalu:
sudo systemctl stop transcendence-backend
sudo -u postgres dropdb transcendence && sudo -u postgres renamedb transcendence_restore transcendence
sudo systemctl start transcendence-backend
```

## 4. Rollback kode

```bash
cd /opt/transcendence-hlwon-
git fetch origin
git checkout --detach "$PREVIOUS_COMMIT"   # commit ter-deploy sebelumnya
source backend/.venv/bin/activate && pip install -r backend/requirements.txt
sudo systemctl restart transcendence-backend
curl -s http://127.0.0.1:8000/api/v1/health
```

TIDAK PERNAH `git reset --hard origin/main` — origin bisa bergerak
setelah approval (audit round-3 #2). `$PREVIOUS_COMMIT` tercatat di
`/var/backups/transcendence/PREVIOUS_COMMIT` oleh workflow.

Skema migrasi bersifat ADDITIVE (001 PG: CREATE TABLE IF NOT EXISTS) —
kode lama tetap bisa berjalan di atas database baru; rollback kode tidak
wajib rollback DB.

## 5. Rollback otomatis post-deploy (stamp run ini)

Workflow deploy: step mana pun gagal → step rollback (`if: failure()`)
menjalankan `backend/deploy/rollback.sh` di VPS. Kontrak (audit
round-4 #2, stamp baru round-5 #2):

- Runner mengekspor `PRE_DEPLOY_STAMP` = stamp backup run INI
  (`run-<run_id>-attempt-<run_attempt>`) dan meneruskannya ke
  rollback.sh — diekspor SEBELUM backup dijalankan, jadi walau step
  backup gagal, rollback tetap tahu stamp run ini dan TIDAK akan
  menebak dari backup lain.
- Rollback HANYA me-restore dump `pre-deploy-$PRE_DEPLOY_STAMP.sql.gz`
  yang ada dan lolos validasi ulang (gzip -t + header dump).
- Backup run ini GAGAL (dump tidak ada / korup) → rollback MENOLAK
  memakai backup lama — kondisi sistem saat itu masih pra-deploy
  (migrasi belum jalan, kode belum di-checkout), jadi tidak ada yang
  diubah; intervensi manual wajib. Dump lama bukan pasangan run ini.
- First deploy (`PREVIOUS_COMMIT=NONE`): service DIHENTIKAN, database
  utama dikembalikan ke pra-deploy, `.env` tidak diubah, retry manual
  (runbook provisioning §10) — tidak ada "kode baru + skema lama".

### 5.1 Serialisasi deployment production (audit round-5 #1 + round-6)

Workflow memakai concurrency group workflow-level dengan antrean
pending maksimal:

```yaml
concurrency:
  group: backend-deploy-production
  cancel-in-progress: false
  queue: max
```

- Tepat SATU deployment production berjalan pada satu waktu — run
  berikutnya MENUNGGU (queued); VPS/database tidak pernah dimodifikasi
  dua deployment sekaligus.
- `cancel-in-progress: false`: deployment berjalan TIDAK PERNAH
  dibatalkan run baru (membatalkan di tengah jalan = backup/migrasi
  terpotong).
- `queue: max` (audit round-6): default GitHub Actions hanya
  mempertahankan SATU run berjalan dan SATU run pending per concurrency
  group — run pending LAMA dapat DIBATALKAN dan digantikan run pending
  terbaru (run baru menggusur yang sudah mengantre; deployment yang
  sudah di-approve reviewer bisa hilang dari antrian tanpa jejak).
  Dengan `queue: max`, hingga **100 run pending** dapat mengantre dan
  diproses SEQUENTIAL sesuai urutan masuk antrean; run tambahan yang
  datang saat antrean penuh (ke-101) **ditolak/dibatalkan** — tidak ada
  run di dalam antrean yang digusur. Konsekuensi operasional: antrean
  produksi tidak pernah "lupa" deploy ber-approval, tetapi pemantauan
  tetap wajib — bila run ke-101 ditolak, jalankan ulang dispatch
  setelah antrean menyusut.
- `queue: max` hanya valid bersama `cancel-in-progress: false` —
  GitHub Actions melarang kombinasi dengan `cancel-in-progress: true`
  (dua opsi menggambarkan perilaku bertentangan untuk run yang sedang
  berjalan); kombinasi salah dites statik di pytest.
- Group statis dan monopoli workflow ini (dites statik: workflow lain
  tidak boleh memakai nama group sama — group GitHub Actions berlaku
  global lintas workflow), dan TIDAK ada concurrency job-level —
  antrian produksi dikelola tepat satu di level workflow.
- Lapis kedua: stamp backup unik per run (§2) membuat backup antar-run
  mustahil bertabrakan; dan kontrak rollback tidak berubah oleh
  panjangnya antrean — rollback (§5) tetap HANYA memakai
  `PRE_DEPLOY_STAMP` milik run yang sama, jadi urutan antrean mana pun
  yang berjalan, pasangan kode+database yang di-restorasi selalu
  pasangan run itu sendiri.

## 6. Rotasi kredensial (MT5_CRED_KEY)

1. Set `MT5_CRED_KEY=<kunci baru>` dan `MT5_CRED_KEY_OLD=<kunci lama>`,
2. Restart backend — dekripsi lama tetap jalan,
3. Kredensial tersimpan ulang dgn kunci baru saat akun disinkron/diubah
   (`key_version` tercatat), lalu kosongkan `MT5_CRED_KEY_OLD`.
