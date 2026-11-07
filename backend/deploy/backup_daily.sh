#!/usr/bin/env bash
# Backup harian cron — TERNORMALISASI + TERNVALIDASI (temuan review N7).
#
# Masalah (N7): runbook cron lama menjalankan backup harian sebagai user
# `postgres` yang MENULIS langsung ke /var/backups/transcendence — padahal
# direktori itu MILIK deploy:deploy 750 (provisioning §10.1) — sehingga
# penulisan cron GAGAL SENYAP. Pipeline `pg_dump | gzip > final` lama juga
# tanpa pipefail, tanpa validasi isi, tanpa penanganan gagal — persis pola
# yang audit round-4 #2 tutup untuk jalur pra-deploy.
#
# Model baru — SATU USER, SATU DIREKTORI, SATU POLA VALIDASI:
#   * cron berjalan sebagai user deploy (sama dengan jalur pra-deploy);
#   * direktori backup tetap deploy:deploy 750 — postgres TIDAK PERNAH
#     menulis ke sini; dump dibaca via peer-auth `sudo -u postgres pg_dump`
#     (tanpa kredensial apa pun di file cron/skrip);
#   * validasi identik backup_db.sh: set -euo pipefail, dump ke file
#     sementara .part (umask 077 -> 600), gzip -t + tidak-kosong + marker
#     "PostgreSQL database dump", publikasi atomik mv, chmod 600;
#   * LAST_DAILY (informasi operasi) HANYA ditulis setelah tervalidasi,
#     dan ditulis ATOMIK: printf ke file sementara .LAST_DAILY.part
#     (direktori sama, filesystem sama) lalu mv — rename POSIX atomik;
#     kill/crash di tengah penulisan tidak pernah meninggalkan pointer
#     kosong/terpotong, pembaca selalu melihat isi lama ATAU baru utuh;
#   * retensi HANYA menyentuh daily-*.sql.gz — artefak kontrak stamp
#     pra-deploy (pre-deploy-*/LAST_*/APPROVED_COMMIT/PREVIOUS_COMMIT)
#     dan manual-* tidak pernah dihapus;
#   * tanpa izin 777; file 600 di dalam dir 750 — hanya user deploy yang
#     berwenang membaca.
#
# Kontrak cron (/etc/cron.d, lihat docs/MULTI-CUSTOMER-BACKUP-ROLLBACK §1):
#   DB_NAME         wajib — nama database PostgreSQL produksi
#   BACKUP_DIR      opsional, default /var/backups/transcendence
#   RETENTION_DAYS  opsional, default 30 (wajib numerik)
#
# Prasyarat: direktori sudah diprovisioning (provisioning §10.1) dan
# sudoers minimal deploy terpasang (provisioning §10.4). Skrip ini TIDAK
# memakai sudo selain `sudo -u postgres pg_dump` — bila owner/mode
# direktori menyimpang, skrip GAGAL dengan pesan perbaikan (tidak
# mengoreksi sendiri, agar cron tidak butuh sudo tambahan).
set -euo pipefail
umask 077

: "${BACKUP_DIR:=/var/backups/transcendence}"
: "${RETENTION_DAYS:=30}"
: "${DB_NAME:?DB_NAME wajib (mis. DB_NAME=transcendence)}"

log() { printf '[backup-daily %s] %s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$*"; }
fail() { log "GAGAL: $*"; exit 1; }

# RETENTION_DAYS hanya numerik (masuk argumen find -mtime).
case "$RETENTION_DAYS" in
  *[!0-9]*) fail "RETENTION_DAYS tidak valid: '$RETENTION_DAYS' (wajib angka)" ;;
esac

# --- preflight izin (TANPA sudo): dir milik user cron, mode 750 ---
[ -d "$BACKUP_DIR" ] || fail "$BACKUP_DIR tidak ada — provisioning §10.1: sudo install -d -o deploy -g deploy -m 750 $BACKUP_DIR"
_dir_owner="$(stat -c '%U' "$BACKUP_DIR")"
_dir_mode="$(stat -c '%a' "$BACKUP_DIR")"
[ "$_dir_owner" = "$(id -un)" ] || fail "$BACKUP_DIR owner=$_dir_owner, bukan $(id -un) — cron wajib berjalan sebagai deploy dan dir wajib deploy:deploy (provisioning §10.1)"
[ "$_dir_mode" = "750" ] || fail "$BACKUP_DIR mode=$_dir_mode, wajib 750 — perbaiki dengan: sudo install -d -o deploy -g deploy -m 750 $BACKUP_DIR"

# Stamp harian: daily-YYYYmmdd-HHMMSS — filename-safe; cadence harian
# membuat tabrakan praktis mustahil (hanya dua panggilan pada detik sama,
# dan itu hanya menimpa artefak harian miliknya sendiri).
STAMP="daily-$(date +%Y%m%d-%H%M%S)"
case "$STAMP" in
  *[!0-9A-Za-z_-]*) fail "stamp tidak filename-safe: '$STAMP'" ;;
esac

TARGET="$BACKUP_DIR/$STAMP.sql.gz"
PART="$BACKUP_DIR/.$STAMP.sql.gz.part"
PART_POINTER="$BACKUP_DIR/.LAST_DAILY.part"
trap 'rm -f "$PART" "$PART_POINTER"' EXIT

# 1) dump | gzip -> FILE SEMENTARA. pipefail: pg_dump yang gagal membuat
#    step gagal WALAU gzip "sukses" mengompres stdin kosong/terpotong.
log "mulai: sudo -u postgres pg_dump $DB_NAME | gzip -> $PART"
sudo -u postgres pg_dump "$DB_NAME" | gzip > "$PART"

# 2) validasi tiga lapis (identik backup_db.sh) SEBELUM publikasi.
gzip -t "$PART" || fail "hasil gzip tidak utuh ($PART)"
if [ "$(gzip -dc "$PART" | wc -c)" -eq 0 ]; then
  fail "dump KOSONG (pg_dump tidak menghasilkan data?)"
fi
if ! gzip -dc "$PART" | grep "PostgreSQL database dump" > /dev/null; then
  fail "isi file bukan dump PostgreSQL"
fi

# 3) publikasi atomik + 600; pointer LAST_DAILY hanya SETELAH tervalidasi.
#    Penulisan pointer juga atomik — tulis ke .LAST_DAILY.part di
#    DIREKTORI YANG SAMA (syarat rename atomik: satu filesystem), lalu
#    mv ke tempat final. Redirect langsung (truncate-then-write) pada
#    pointer: kill/crash/habisnya disk di antara truncate dan tulis
#    meninggalkan pointer kosong yang bisa disalahbaca operasional
#    sebagai "belum pernah backup". mv (rename) mengganti isi pointer
#    dalam satu operasi atomik — isi lama utuh atau isi baru utuh.
mv "$PART" "$TARGET"
chmod 600 "$TARGET"
printf '%s\n' "$STAMP" > "$PART_POINTER"
chmod 600 "$PART_POINTER"
mv "$PART_POINTER" "$BACKUP_DIR/LAST_DAILY"
log "OK: $TARGET tervalidasi (stamp $STAMP)"

# 4) retensi: HANYA daily-*.sql.gz milik skrip ini yang boleh dihapus.
#    Tanpa mode hapus langsung dari find — hapus eksplisit satu per satu
#    setelah pencocokan pola sempit (maxdepth 1, nama daily-*.sql.gz).
_deleted=0
while IFS= read -r -d '' _f; do
  rm -f -- "$_f"
  _deleted=$((_deleted + 1))
done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'daily-*.sql.gz' -mtime +"$RETENTION_DAYS" -print0)
log "retensi: $_deleted file daily-*.sql.gz > ${RETENTION_DAYS} hari dihapus"
