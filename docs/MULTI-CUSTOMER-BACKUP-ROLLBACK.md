# Backup & Rollback — Transcendence Multi-Customer

## 1. Backup harian (cron di VPS)

```bash
# /etc/cron.d/transcendence-backup
15 2 * * * postgres pg_dump transcendence | gzip > /var/backups/transcendence/daily-$(date +\%Y\%m\%d).sql.gz
30 2 * * * find /var/backups/transcendence -name 'daily-*.sql.gz' -mtime +30 -delete
```

## 2. Backup sebelum migrasi (otomatis)

Workflow `.github/workflows/backend-deploy.yml` menjalankan `pg_dump`
sebelum tiap deployment (job "Backup database sebelum migration") dan
menyimpan ke `/var/backups/transcendence/pre-deploy-*.sql.gz`.

Backup manual sebelum migrasi:

```bash
sudo -u postgres pg_dump transcendence | gzip > /var/backups/transcendence/manual-$(date +%Y%m%d-%H%M%S).sql.gz
```

## 3. Restore / rollback database

```bash
LATEST=$(ls -1t /var/backups/transcendence/pre-deploy-*.sql.gz | head -1)
sudo -u postgres dropdb --if-exists transcendence_restore
sudo -u postgres createdb transcendence_restore
gunzip -c "$LATEST" | sudo -u postgres psql transcendence_restore
# cek dulu, lalu:
sudo systemctl stop transcendence-backend
sudo -u postgres dropdb transcendence && sudo -u postgres renamedb transcendence_restore transcendence
sudo systemctl start transcendence-backend
```

## 4. Rollback kode

```bash
cd /opt/transcendence
git reset --hard origin/main          # atau commit sebelumnya
sudo systemctl restart transcendence-backend
curl -s http://127.0.0.1:8000/api/v1/health
```

Skema migrasi bersifat ADDITIVE (001 PG: CREATE TABLE IF NOT EXISTS) —
kode lama tetap bisa berjalan di atas database baru; rollback kode tidak
wajib rollback DB.

## 5. Rollback otomatis post-deploy

Workflow deploy: health check gagal 5× → job rollback otomatis
(reset kode ke origin/main + restore backup terbaru) — lihat
`.github/workflows/backend-deploy.yml`.

## 6. Rotasi kredensial (MT5_CRED_KEY)

1. Set `MT5_CRED_KEY=<kunci baru>` dan `MT5_CRED_KEY_OLD=<kunci lama>`,
2. Restart backend — dekripsi lama tetap jalan,
3. Kredensial tersimpan ulang dgn kunci baru saat akun disinkron/diubah
   (`key_version` tercatat), lalu kosongkan `MT5_CRED_KEY_OLD`.
