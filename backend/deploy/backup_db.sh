#!/usr/bin/env bash
# Backup database pra-deploy — TERNORMALISASI + TERNVALIDASI.
# Audit round-4 (temuan #2): pipeline `pg_dump | gzip` lama hanya pakai
# `set -e` — kegagalan pg_dump TERSEMBUNYI karena exit status pipeline =
# exit status TERAKHIR (gzip), sehingga dump KOSONG/TERPOTONG terbit
# dengan nama final dan LAST_PRE_DEPLOY ikut diperbarui — rollback
# berikutnya me-restore dump rusak seolah sah.
#
# Perbaikan (skrip ini dipanggil workflow via `bash -s`, sama pola dgn
# rollback.sh — jadi bisa DITES sungguhan, bukan cuma dicek statik):
#   1. `set -euo pipefail` — kegagalan di posisi MANA PUN dalam pipeline
#      (pg_dump maupun gzip) menghentikan seluruh step deployment.
#   2. Dump ditulis ke FILE SEMENTARA (.part, diawali titik, umask 077
#      -> 600): nama final TIDAK PERNAH berisi file yang belum tervalidasi.
#   3. Validasi berlapis SEBELUM publikasi:
#        a. `gzip -t`      — container gzip utuh (CRC) ;
#        b. ukuran dekompresi > 0 (gzip -t saja tidak menolak dump kosong
#           — .gz dari stdin kosong adalah gzip VALID);
#        c. marker `PostgreSQL database dump` (header pg_dump) — isi
#           benar-benar dump PG, bukan keluaran error/wrapper lain.
#      Pembaca validasi (wc/grep) menghabiskan SELURUH stdin sehingga
#      aman terhadap SIGPIPE di bawah pipefail.
#   4. Publikasi atomik `mv .part -> final` HANYA setelah validasi lulus.
#   5. `LAST_PRE_DEPLOY` diperbarui HANYA setelah backup tervalidasi —
#      bila run ini gagal, stamp tetap menunjuk backup VALID run lama,
#      dan file stamp TIDAK PERNAH menunjuk dump rusak.
#   6. `trap rm -f` membersihkan file .part pada kegagalan apa pun.
#
# Kontrak (dari workflow, semua wajib):
#   STAMP        stamp run deploy ini (YYYYmmdd-HHMMSS, dari runner agar
#                bisa diteruskan ke rollback sebagai PRE_DEPLOY_STAMP)
#   BACKUP_DIR   /var/backups/transcendence
#   BACKUP_OWNER user pemilik direktori backup (deploy)
#   DB_NAME      nama database PostgreSQL produksi
#
# Konsumsi disk sementara = dump + salinan dekompresi? TIDAK — validasi
# mem-stream dekompresi (tanpa file mentah); hanya .part (setara dump
# terkompresi) yang ada di disk.
set -euo pipefail
umask 077

: "${STAMP:?STAMP wajib (YYYYmmdd-HHMMSS dari runner)}"
: "${BACKUP_DIR:?BACKUP_DIR wajib}"
: "${BACKUP_OWNER:?BACKUP_OWNER wajib}"
: "${DB_NAME:?DB_NAME wajib}"

# STAMP masuk ke nama file — hanya karakter aman yang diterima.
case "$STAMP" in
  *[!0-9A-Za-z_-]*)
    echo "::error::STAMP tidak valid: '$STAMP' (hanya [0-9A-Za-z_-])"
    exit 1 ;;
esac

# AUDIT ROUND-3 #1 (tetap): direktori backup MILIK user deploy (750),
# idempoten — owner/mode dikoreksi SEBELUM penulisan apa pun.
sudo install -d -o "$BACKUP_OWNER" -g "$BACKUP_OWNER" -m 750 "$BACKUP_DIR"

TARGET="$BACKUP_DIR/pre-deploy-$STAMP.sql.gz"
PART="$BACKUP_DIR/.pre-deploy-$STAMP.sql.gz.part"
trap 'rm -f "$PART"' EXIT

# 1) dump | gzip -> FILE SEMENTARA. Dengan pipefail, pg_dump yang gagal
#    membuat pipeline gagal WALAU gzip "sukses" mengompres stdin kosong/
#    terpotong — celah utama yang ditutup temuan ini.
sudo -u postgres pg_dump "$DB_NAME" | gzip > "$PART"

# 2a) container gzip utuh
gzip -t "$PART" || {
  echo "::error::hasil gzip tidak utuh ($PART) — backup GAGAL"
  exit 1
}
# 2b) isi tidak kosong (dump kosong = .gz valid menurut gzip -t!)
if [ "$(gzip -dc "$PART" | wc -c)" -eq 0 ]; then
  echo "::error::dump KOSONG (pg_dump tidak menghasilkan data?) — backup GAGAL"
  exit 1
fi
# 2c) isi benar-benar dump PostgreSQL (header pg_dump). grep membaca
#     seluruh stream (tanpa -q/-m1) -> aman SIGPIPE di bawah pipefail.
if ! gzip -dc "$PART" | grep "PostgreSQL database dump" > /dev/null; then
  echo "::error::isi file bukan dump PostgreSQL — backup GAGAL"
  exit 1
fi

# 3) publikasi atomik: nama final hanya muncul SETELAH tervalidasi.
mv "$PART" "$TARGET"
chmod 600 "$TARGET"

# 4) LAST_PRE_DEPLOY hanya SETELAH backup tervalidasi (round-4 #2).
printf '%s\n' "$STAMP" > "$BACKUP_DIR/LAST_PRE_DEPLOY"
chmod 600 "$BACKUP_DIR/LAST_PRE_DEPLOY"

echo "backup pra-deploy TERNVALIDASI: $TARGET (stamp $STAMP)"
