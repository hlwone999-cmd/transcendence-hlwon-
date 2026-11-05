#!/usr/bin/env bash
# Rollback deployment backend — audit round-3 (temuan #3) + round-4
# (temuan #2: rollback tidak boleh memakai backup lama saat run gagal).
#
# Dijalankan DI VPS oleh workflow Backend Deploy (step `if: failure()`)
# via `ssh ... 'APP_DIR=... PRE_DEPLOY_STAMP=... bash -s' < backend/
# deploy/rollback.sh`. Semua parameter masuk sebagai environment
# variable (wajib ada):
#   APP_DIR, VENV_DIR, SERVICE, ENV_FILE, BACKUP_DIR, DB_NAME,
#   RESTORE_DB_NAME, HEALTH_URL, PRE_DEPLOY_STAMP (round-4)
#
# DUA jalur rollback (logika ini DITES — backend/tests/test_rollback_
# script.py memakai stub systemctl/dropdb/createdb/psql/curl):
#
# 1. NORMAL (PREVIOUS_COMMIT tercatat):
#      kode -> `git checkout --detach $PREVIOUS_COMMIT` (commit ter-
#      deploy sebelumnya, BUKAN origin/<branch> yang bisa bergerak),
#      database pra-deploy -> RESTORE_DB_NAME, .env -> RESTORE_DB_NAME,
#      restart, health check. Pasangan (kode lama, DB pra-deploy)
#      kompatibel karena dump diambil SEBELUM migrasi kode baru.
#
# 2. FIRST DEPLOY (PREVIOUS_COMMIT=NONE) — temuan audit round-3 #3:
#      TIDAK ada kode lama untuk dikembalikan. DILARANG keras:
#      me-restore database lama lalu membiarkan KODE BARU terus
#      berjalan di atas skema lama. Prosedur yang benar (dan diuji):
#        a. HENTIKAN service — kode baru berhenti melayani;
#        b. kembalikan DATABASE UTAMA ke kondisi pra-deploy dari dump;
#        c. .env TIDAK diubah (tetap menunjuk database utama);
#        d. service dibiarkan STOP — first-deploy di-retry manual
#           sesuai runbook (provisioning §10).
#
# AUDIT ROUND-4 #2 — HANYA backup MILIK run deploy ini:
#   Workflow mengekspor PRE_DEPLOY_STAMP (stamp backup run ini, lihat
#   backend/deploy/backup_db.sh) lalu meneruskannya ke sini. Rollback
#   HANYA me-restore dump `pre-deploy-$PRE_DEPLOY_STAMP.sql.gz` yang
#   ADA dan lolos validasi (gzip -t + marker dump PG). Konsekuensi:
#   - backup run ini GAGAL (file tidak ada / korup) => rollback
#     MENOLAK me-restore dump LAMA (LAST_PRE_DEPLOY sengaja TIDAK
#     dipakai lagi: dump lama bukan pasangan pra-deploy run ini dan
#     bisa jauh lebih tua dari state DB sekarang — me-restore-nya
#     justru MENGHAPUS data). Kondisi sistem saat backup gagal masih
#     pra-deploy (migration belum jalan, kode belum di-checkout), jadi
#     tidak ada yang perlu dikembalikan — intervensi manual wajib.
#   - LAST_PRE_DEPLOY tetap ditulis backup_db.sh sebagai info operasi
#     "backup valid terakhir", bukan lagi dasar keputusan rollback.
set -euo pipefail

: "${APP_DIR:?APP_DIR wajib}" "${VENV_DIR:?VENV_DIR wajib}" \
  "${SERVICE:?SERVICE wajib}" "${ENV_FILE:?ENV_FILE wajib}" \
  "${BACKUP_DIR:?BACKUP_DIR wajib}" "${DB_NAME:?DB_NAME wajib}" \
  "${RESTORE_DB_NAME:?RESTORE_DB_NAME wajib}" \
  "${HEALTH_URL:?HEALTH_URL wajib}"

health_ok() {
  local i code
  for i in 1 2 3 4 5 6; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)
    [ "$code" = "200" ] && return 0
    echo "rollback health percobaan $i: HTTP $code"
    sleep 5
  done
  return 1
}

PREV=$(cat "$BACKUP_DIR/PREVIOUS_COMMIT" 2>/dev/null || echo NONE)

# AUDIT ROUND-4 #2: stamp backup MILIK run deploy ini (dikirim
# workflow). TANPA stamp ini rollback tidak bisa memastikan backup
# mana yang satu pasangan dengan run ini — dan DILARANG menebak dari
# LAST_PRE_DEPLOY (bisa menunjuk backup run LAMA).
RUN_STAMP="${PRE_DEPLOY_STAMP:-}"
if [ -z "$RUN_STAMP" ]; then
  echo "::error::PRE_DEPLOY_STAMP tidak diberikan — rollback tidak bisa memastikan backup milik run ini; backup LAMA DILARANG dipakai. Tidak ada perubahan dilakukan; intervensi manual wajib"
  exit 1
fi
case "$RUN_STAMP" in
  *[!0-9A-Za-z_-]*)
    echo "::error::PRE_DEPLOY_STAMP tidak valid: '$RUN_STAMP'"
    exit 1 ;;
esac
DUMP="$BACKUP_DIR/pre-deploy-$RUN_STAMP.sql.gz"

# Jaga-jaga round-4: backup run INI harus ada + valid SEBELUM menyentuh
# apa pun. Bila run ini gagal backup (pg_dump/gzip/validasi), kondisi
# sistem masih pra-deploy (migration belum jalan) — rollback TIDAK
# boleh menggantinya dengan dump lama.
if [ ! -f "$DUMP" ]; then
  echo "::error::backup run ini tidak ada ($DUMP) — backup run saat ini GAGAL sebelum tervalidasi; backup LAMA tidak boleh dipakai (bukan pasangan run ini). DB & kode TIDAK diubah (kondisi masih pra-deploy); intervensi manual wajib"
  exit 1
fi
if ! gzip -t "$DUMP"; then
  echo "::error::backup run ini korup ($DUMP) — ditolak; backup LAMA tidak boleh dipakai. Tidak ada perubahan dilakukan; intervensi manual wajib"
  exit 1
fi
if ! gzip -dc "$DUMP" | grep "PostgreSQL database dump" > /dev/null; then
  echo "::error::isi $DUMP bukan dump PostgreSQL — ditolak; intervensi manual wajib"
  exit 1
fi
echo "backup pra-deploy run ini TERNVALIDASI: $DUMP"

if [ -n "$PREV" ] && [ "$PREV" != "NONE" ]; then
  # ============================================================ NORMAL
  cd "$APP_DIR"
  git fetch origin
  git checkout --detach "$PREV"
  echo "kode dikembalikan ke commit ter-deploy sebelumnya: $PREV"
  source "$VENV_DIR/bin/activate"
  pip install -r backend/requirements.txt
  # Database: dump pra-deploy (skema SEBELUM migrasi — cocok dengan
  # kode lama) di-restore ke RESTORE_DB_NAME.
  sudo -u postgres dropdb --if-exists "$RESTORE_DB_NAME"
  sudo -u postgres createdb "$RESTORE_DB_NAME"
  gunzip -c "$DUMP" | sudo -u postgres psql -q "$RESTORE_DB_NAME"
  # Service diarahkan ke database restore via EnvironmentFile systemd.
  sudo sed -i -E "s#(postgresql://[^/]+)/$DB_NAME([?[:space:]]|$)#\1/$RESTORE_DB_NAME\2#" "$ENV_FILE"
  sudo systemctl restart "$SERVICE"
  if health_ok; then
    echo "ROLLBACK OK (kode $PREV + DB $RESTORE_DB_NAME dari $DUMP)"
    exit 0
  fi
  echo "::error::ROLLBACK health check GAGAL — intervensi manual wajib"
  exit 1
fi

# ====================================================== FIRST DEPLOY
# PREVIOUS_COMMIT=NONE: tanpa kode lama. Kembalikan sistem ke kondisi
# pra-deploy, BUKAN "kode baru + skema lama".
echo "FIRST-DEPLOY ROLLBACK: menghentikan service + memulihkan DB pra-deploy"
sudo systemctl stop "$SERVICE"
sudo -u postgres dropdb --if-exists "$DB_NAME"
sudo -u postgres createdb "$DB_NAME"
gunzip -c "$DUMP" | sudo -u postgres psql -q "$DB_NAME"
# .env tidak diubah (tetap database utama); service TIDAK di-restart.
echo "DB utama dikembalikan ke pra-deploy ($DUMP); service DIHENTIKAN"
echo "Pemulihan selesai — first-deploy harus di-retry MANUAL (runbook provisioning §10)"
exit 0
