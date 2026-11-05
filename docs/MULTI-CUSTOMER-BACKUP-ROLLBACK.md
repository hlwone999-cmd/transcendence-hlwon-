# Backup & Rollback — Transcendence Multi-Customer

## 1. Backup harian (cron di VPS)

```bash
# /etc/cron.d/transcendence-backup
15 2 * * * postgres pg_dump transcendence | gzip > /var/backups/transcendence/daily-$(date +\%Y\%m\%d).sql.gz
30 2 * * * find /var/backups/transcendence -name 'daily-*.sql.gz' -mtime +30 -delete
```

## 2. Backup pra-deploy (otomatis + TERNVALIDASI)

Workflow `.github/workflows/backend-deploy.yml` menjalankan
`backend/deploy/backup_db.sh` (dikirim via `bash -s`) sebelum tiap
deployment. Skrip memakai `set -euo pipefail` sehingga kegagalan
`pg_dump` maupun `gzip` menghentikan deployment — tidak ada lagi dump
kosong/terpotong yang lolos. Hasil ditulis ke file sementara `.part`,
divalidasi berlapis (`gzip -t` + tidak kosong + header dump PostgreSQL),
baru dipublikasi atomik ke
`/var/backups/transcendence/pre-deploy-<STAMP>.sql.gz`.

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
round-4 #2):

- Runner mengekspor `PRE_DEPLOY_STAMP` = stamp backup run INI dan
  meneruskannya ke rollback.sh.
- Rollback HANYA me-restore dump `pre-deploy-$PRE_DEPLOY_STAMP.sql.gz`
  yang ada dan lolos validasi ulang (gzip -t + header dump).
- Backup run ini GAGAL (dump tidak ada / korup) → rollback MENOLAK
  memakai backup lama — kondisi sistem saat itu masih pra-deploy
  (migrasi belum jalan, kode belum di-checkout), jadi tidak ada yang
  diubah; intervensi manual wajib. Dump lama bukan pasangan run ini.
- First deploy (`PREVIOUS_COMMIT=NONE`): service DIHENTIKAN, database
  utama dikembalikan ke pra-deploy, `.env` tidak diubah, retry manual
  (runbook provisioning §10) — tidak ada "kode baru + skema lama".

## 6. Rotasi kredensial (MT5_CRED_KEY)

1. Set `MT5_CRED_KEY=<kunci baru>` dan `MT5_CRED_KEY_OLD=<kunci lama>`,
2. Restart backend — dekripsi lama tetap jalan,
3. Kredensial tersimpan ulang dgn kunci baru saat akun disinkron/diubah
   (`key_version` tercatat), lalu kosongkan `MT5_CRED_KEY_OLD`.
