#!/usr/bin/env bash
# Stamp backup pra-deploy UNIK per run deploy — audit round-5.
#
# Masalah skema lama `date +%Y%m%d-%H%M%S` (timestamp resolusi DETIK):
# dua workflow run — atau re-run run yang sama — yang jatuh pada detik
# yang sama menghasilkan STAMP identik. Akibatnya backup_db.sh menulis
# file final yang SAMA (`pre-deploy-<stamp>.sql.gz`), isi backup saling
# menimpa, dan backup run LAMA bisa terkecoh dianggap backup run INI —
# persis risiko yang round-4 tutup untuk kasus backup gagal.
#
# Solusi (temuan audit round-5 #2): stamp = identifier unik yang MEMUAT
# github.run_id dan github.run_attempt:
#
#     run-<RUN_ID>-attempt-<RUN_ATTEMPT>
#
#   - RUN_ID       unik per workflow run      (= github.run_id)
#   - RUN_ATTEMPT  naik pada tiap re-run run yang sama
#                                             (= github.run_attempt)
#   -> dua run berbeda TIDAK mungkin menghasilkan stamp sama; re-run
#      run yang sama pun mendapat stamp baru (dan backup barunya sendiri).
#   - Deterministik: (run_id, attempt) sama -> stamp sama — stamp bisa
#     diputuskan ulang tanpa risiko bergeser.
#   - filename-safe: hanya [0-9A-Za-z_-] — sama dengan gate charset
#     backup_db.sh dan rollback.sh (anti injeksi path). Tanpa '/' tanpa
#     '.' -> tidak bisa keluar dari BACKUP_DIR.
#
# Kontrak (workflow Backend Deploy, step backup):
#   RUN_ID      = ${{ github.run_id }}
#   RUN_ATTEMPT = ${{ github.run_attempt }}
# Output: stamp satu baris pada stdout.
set -euo pipefail

: "${RUN_ID:?RUN_ID wajib (github.run_id)}"
: "${RUN_ATTEMPT:?RUN_ATTEMPT wajib (github.run_attempt)}"

# github.run_id / github.run_attempt selalu numerik di runner — tolak
# yang lain agar karakter liar tidak mungkin masuk nama file walau
# pemanggil salah mengirim nilai.
if ! printf '%s' "$RUN_ID" | grep -Eq '^[0-9]+$'; then
  echo "::error::RUN_ID wajib numerik (github.run_id; diterima: '$RUN_ID')"
  exit 1
fi
if ! printf '%s' "$RUN_ATTEMPT" | grep -Eq '^[0-9]+$'; then
  echo "::error::RUN_ATTEMPT wajib numerik (github.run_attempt; diterima: '$RUN_ATTEMPT')"
  exit 1
fi

STAMP="run-${RUN_ID}-attempt-${RUN_ATTEMPT}"

# Gate filename-safe terakhir (paranoid): charset identik dengan
# validasi backup_db.sh — bila format di atas berubah keluar charset,
# skrip ini menolak sebelum sempat sampai ke VPS.
case "$STAMP" in
  *[!0-9A-Za-z_-]*)
    echo "::error::stamp tidak filename-safe: '$STAMP' (hanya [0-9A-Za-z_-])"
    exit 1 ;;
esac

printf '%s\n' "$STAMP"
