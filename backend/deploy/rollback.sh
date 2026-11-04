#!/usr/bin/env bash
# Rollback deployment backend — audit round-3 (temuan #3).
#
# Dijalankan DI VPS oleh workflow Backend Deploy (step `if: failure()`)
# via `ssh ... 'APP_DIR=... bash -s' < backend/deploy/rollback.sh`.
# Semua parameter masuk sebagai environment variable (wajib ada):
#   APP_DIR, VENV_DIR, SERVICE, ENV_FILE, BACKUP_DIR, DB_NAME,
#   RESTORE_DB_NAME, HEALTH_URL
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
#      Bila dump pra-deploy tidak ada: TIDAK menyentuh apa pun,
#      exit 1 (intervensi manual wajib).
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
LATEST=$(cat "$BACKUP_DIR/LAST_PRE_DEPLOY" 2>/dev/null || true)
DUMP="$BACKUP_DIR/pre-deploy-$LATEST.sql.gz"

# Jaga-jaga: dump harus benar-benar ada SEBELUM menyentuh apa pun.
if [ -z "$LATEST" ] || [ ! -f "$DUMP" ]; then
  echo "::error::dump pra-deploy tidak ditemukan ($DUMP) — rollback DIBATALKAN tanpa perubahan; intervensi manual wajib"
  exit 1
fi

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
