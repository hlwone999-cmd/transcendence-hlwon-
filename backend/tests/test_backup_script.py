"""TEMUAN AUDIT ROUND-4 #2 — backup pra-deploy benar-benar diuji.

Masalah: workflow lama menjalankan `pg_dump | gzip > final` hanya dengan
`set -e` — kegagalan pg_dump TERSEMBUNYI (exit pipeline = exit gzip),
dump KOSONG/TERPOTONG terbit dengan nama final, dan LAST_PRE_DEPLOY
tetap diperbarui. Rollback berikutnya me-restore dump rusak.

Perbaikan: logika backup dipindah ke backend/deploy/backup_db.sh
(workflow mengirim via `bash -s`, sama pola dgn rollback.sh) sehingga
bisa DIJALANKAN sungguhan di test — bukan cuma dicek statik. Skrip:
`set -euo pipefail`, dump ke file .part (umask 077), validasi gzip -t +
non-kosong + marker dump PG, mv atomik, LAST_PRE_DEPLOY HANYA setelah
tervalidasi, trap membersihkan .part.

Tiga lapis pengujian:
1. STATIK    — skema wiring workflow + isi skrip (pipefail, .part,
               gzip -t, urutan mv < LAST_PRE_DEPLOY, PRE_DEPLOY_STAMP
               diekspor + diteruskan ke rollback).
2. SIMULASI NYATA — `bash backup_db.sh` dijalankan dengan stub
               pg_dump/gzip pada PATH:
               a. sukses        -> .part -> tervalidasi -> final 600 +
                                   LAST_PRE_DEPLOY = stamp run ini;
               b. pg_dump GAGAL (exit 1 di tengah aliran) -> pipeline
                  gagal (pipefail), TANPA file final, LAST_PRE_DEPLOY
                  TIDAK berubah, .part dibersihkan trap;
               c. gzip GAGAL -> pipeline gagal, tidak ada publikasi;
               d. pg_dump exit 0 tapi OUTPUT KOSONG -> gzip -t lolos
                  (gzip kosong VALID!) tapi validasi isi MENOLAK —
                  bukti cek non-kosong + marker itu wajib;
               e. output bukan dump PG (teks error) -> marker MENOLAK;
               f. gzip menghasilkan file KORUP -> gzip -t MENOLAK;
               g. STAMP dengan karakter aneh -> ditolak (filename-safe).
3. KONTRAK ROLLBACK — LAST_PRE_DEPLOY hanya ditulis pasca-validasi dan
   rollback tidak lagi menjadikannya dasar restore (lihat
   tests/test_rollback_script.py::test_backup_run_gagal_...).
"""
import gzip
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKUP_SH = REPO / "backend" / "deploy" / "backup_db.sh"
DEPLOY_WF = REPO / ".github" / "workflows" / "backend-deploy.yml"
ROLLBACK_SH = REPO / "backend" / "deploy" / "rollback.sh"

STAMP = "20261105-090000"
DUMP_OK = (b"--\n-- PostgreSQL database dump\n--\n"
           b"CREATE TABLE customers (id int);\n")

# ---------------------------------------------------------------- 1. statik
def test_workflow_mengirim_backup_db_sh_dengan_env_lengkap():
    raw = DEPLOY_WF.read_text(encoding="utf-8")
    assert "backend/deploy/backup_db.sh" in raw, \
        "logika backup wajib berada di backup_db.sh (dites pytest)"
    assert "bash -s" in raw, "skrip dikirim ke VPS via stdin (bash -s)"
    for var in ("STAMP=", "BACKUP_DIR=", "BACKUP_OWNER=", "DB_NAME="):
        assert var in raw, f"env {var} wajib diteruskan ke backup_db.sh"
    # stamp run ini diekspor agar rollback tahu backup milik run INI
    assert re.search(r'PRE_DEPLOY_STAMP=\$STAMP.*GITHUB_ENV', raw) or \
        re.search(r'echo\s+"PRE_DEPLOY_STAMP=\$STAMP"\s+>>\s+"\$GITHUB_ENV"',
                  raw), "PRE_DEPLOY_STAMP wajib diekspor ke GITHUB_ENV"


def test_backup_script_pipefail_dan_file_sementara():
    raw = BACKUP_SH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in raw, \
        "wajib pipefail — kegagalan pg_dump tidak boleh tersembunyi"
    assert ".part" in raw, "dump wajib ditulis ke file sementara (.part)"
    assert "gzip -t" in raw, "wajib validasi gzip -t"
    assert "PostgreSQL database dump" in raw, \
        "wajib validasi marker dump PostgreSQL"
    assert re.search(r"if\s+\[\s*\"\$\(gzip -dc \"\$PART\" \| wc -c\)\s*\""
                     r"\s*-eq 0\s*\]", raw) or "wc -c" in raw, \
        "wajib cek dump tidak kosong (gzip -t saja tidak menolak dump kosong)"
    assert "umask 077" in raw, "file dump wajib 600 sejak dibuat"
    assert "mv" in raw, "publikasi atomik: mv .part -> nama final"
    assert "trap" in raw and "rm -f" in raw, \
        "file .part wajib dibersihkan saat gagal"
    # tidak kembali ke pola lama
    assert not re.search(r"ls -1t.*\|\s*head", raw), \
        "ls|head berisiko SIGPIPE di bawah pipefail — dihapus"


def test_last_pre_deploy_ditulis_setelah_validasi():
    """Urutan wajib dalam backup_db.sh: publikasi (mv) HARUS muncul
    SEBELUM penulisan LAST_PRE_DEPLOY."""
    raw = BACKUP_SH.read_text(encoding="utf-8")
    lines = raw.splitlines()
    idx_mv = [i for i, ln in enumerate(lines) if re.search(r"mv .*TARGET", ln)]
    idx_stamp = [i for i, ln in enumerate(lines)
                 if "LAST_PRE_DEPLOY" in ln and ">" in ln]
    assert idx_mv and idx_stamp, "mv/LAST_PRE_DEPLOY tidak ditemukan"
    assert min(idx_mv) < min(idx_stamp), \
        "LAST_PRE_DEPLOY wajib ditulis SETELAH backup tervalidasi (mv)"


def test_rollback_tidak_mengandalkan_last_pre_deploy():
    """Round-4 #2: keputusan restore rollback berbasis stamp run ini,
    BUKAN LAST_PRE_DEPLOY (backup lama)."""
    raw = ROLLBACK_SH.read_text(encoding="utf-8")
    assert "PRE_DEPLOY_STAMP" in raw, \
        "rollback wajib menerima stamp backup run ini"
    # tidak ada lagi pembacaan LAST_PRE_DEPLOY untuk keputusan restore
    assert not re.search(r"LATEST=.*LAST_PRE_DEPLOY", raw), \
        "rollback tidak boleh menebak backup dari LAST_PRE_DEPLOY"
    assert "backup LAMA tidak boleh dipakai" in raw


# --------------------------------------------------------- 2. simulasi nyata
def _dunia(tmp_path: Path):
    """Dunia palsu utk menjalankan backup_db.sh sungguhan."""
    bk = tmp_path / "var" / "backups" / "transcendence"
    bk.mkdir(parents=True)
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    env = {
        "STAMP": STAMP,
        "BACKUP_DIR": str(bk),
        "BACKUP_OWNER": str(os.getuid()),   # install -d -o <uid-sendiri> OK
        "DB_NAME": "transcendence",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    return bk, bin_dir, log, env


def _stub(bin_dir: Path, name: str, log: Path, body: str):
    p = bin_dir / name
    p.write_text(f'#!/bin/bash\necho "{name} $*" >> {log}\n{body}\n')
    p.chmod(0o755)


def _stub_sudo(bin_dir: Path, log: Path):
    """sudo: catat, lewati -u <user>, eksekusi target (stub pg_dump)."""
    p = bin_dir / "sudo"
    p.write_text('#!/bin/bash\n'
                 f'echo "sudo $*" >> {log}\n'
                 'if [ "$1" = "-u" ]; then shift 2; fi\n'
                 'exec "$@"\n')
    p.chmod(0o755)


def _run(bk, env, tmp_path):
    return subprocess.run(
        ["bash", str(BACKUP_SH)], env={**os.environ, **env},
        capture_output=True, text=True, cwd=str(tmp_path))


def _final(bk: Path) -> Path:
    return bk / f"pre-deploy-{STAMP}.sql.gz"


def test_backup_sukses_publikasi_dan_stamp_terupdate(tmp_path):
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub(bin_dir, "pg_dump", log, f"cat << 'EOF'\n{DUMP_OK.decode()}EOF")
    r = _run(bk, env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    final = _final(bk)
    assert final.exists(), "dump tervalidasi wajib terpublikasi"
    # validasi independen (gzip asli di luar skrip)
    assert gzip.decompress(final.read_bytes()).startswith(b"--\n-- PostgreSQL")
    assert (bk / f".pre-deploy-{STAMP}.sql.gz.part").exists() is False, \
        ".part wajib sudah berpindah (mv)"
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == STAMP
    assert oct(os.stat(final).st_mode)[-3:] == "600", \
        "dump berisi data transaksi — wajib 600"
    assert "TERNVALIDASI" in r.stdout


def test_pg_dump_gagal_tidak_ada_publikasi_stamp_tidak_berubah(tmp_path):
    """SIMULASI inti temuan: pg_dump GAGAL di tengah aliran (exit 1).
    pipefail wajib membuat step gagal — TANPA file final, stamp lama
    TIDAK tersentuh, .part dibersihkan."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    (bk / "LAST_PRE_DEPLOY").write_text("20260101-000000\n")
    # pg_dump: tulis sebagian SQL lalu MATI (exit 1) — persis kasus
    # "koneksi DB putus di tengah dump"
    _stub(bin_dir, "pg_dump", log,
          "echo '-- PostgreSQL database dump'; echo 'CREATE ...'; exit 1")
    r = _run(bk, env, tmp_path)
    assert r.returncode != 0, "kegagalan pg_dump wajib menghentikan step"
    assert not _final(bk).exists(), \
        "tidak boleh ada file final dari backup yang gagal"
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == "20260101-000000", \
        "LAST_PRE_DEPLOY tidak boleh berubah saat backup gagal"
    assert not list(bk.glob(".*.part")), \
        "file sementara wajib dibersihkan trap"
    assert not list(bk.glob("*part")), "tidak ada sisa file .part"


def test_gzip_gagal_tidak_ada_publikasi(tmp_path):
    """gzip gagal (mis. disk penuh) -> pipeline gagal -> tanpa publikasi."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    (bk / "LAST_PRE_DEPLOY").write_text("20260101-000000\n")
    # gzip: buang stdin lalu GAGAL
    _stub(bin_dir, "gzip", log, "cat > /dev/null; exit 1")
    _stub(bin_dir, "pg_dump", log, f"cat << 'EOF'\n{DUMP_OK.decode()}EOF")
    r = _run(bk, env, tmp_path)
    assert r.returncode != 0, "kegagalan gzip wajib menghentikan step"
    assert not _final(bk).exists()
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == "20260101-000000"


def test_dump_kosong_ditolak_walau_gzip_valid(tmp_path):
    """pg_dump exit 0 dengan output KOSONG: gzip menghasilkan .gz VALID
    (gzip -t lolos!) — validasi isi (non-kosong) wajib MENOLAK. Inilah
    celah yang tidak terlihat oleh `set -e` maupun `gzip -t` saja."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    (bk / "LAST_PRE_DEPLOY").write_text("20260101-000000\n")
    _stub(bin_dir, "pg_dump", log, "exit 0")     # stdout kosong
    r = _run(bk, env, tmp_path)
    assert r.returncode != 0, "dump kosong wajib ditolak"
    assert not _final(bk).exists()
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == "20260101-000000"
    assert "KOSONG" in r.stdout


def test_dump_bukan_pg_ditolak_marker(tmp_path):
    """pg_dump 'sukses' tapi isinya keluaran error/wrapper (tanpa header
    dump PG) — marker check menolak."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    (bk / "LAST_PRE_DEPLOY").write_text("20260101-000000\n")
    _stub(bin_dir, "pg_dump", log,
          "echo 'pg_dump: error: koneksi ditolak server'; exit 0")
    r = _run(bk, env, tmp_path)
    assert r.returncode != 0
    assert not _final(bk).exists()
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == "20260101-000000"
    assert "bukan dump PostgreSQL" in r.stdout


def test_gzip_korup_ditolak_gzip_t(tmp_path):
    """gzip menghasilkan file KORUP (bukan container gzip valid) —
    `gzip -t` wajib menolak SEBELUM publikasi."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    (bk / "LAST_PRE_DEPLOY").write_text("20260101-000000\n")
    # stub gzip: pada pipeline (tanpa -t) tulis sampah + exit 0;
    # pada gzip -t jalankan gzip ASLI (yang akan menolak sampah).
    _stub(bin_dir, "gzip", log,
          'if [ "$1" = "-t" ]; then exec /bin/gzip "$@"; fi; '
          "cat > /dev/null; "
          'printf "BUKAN-GZIP\\x00\\x01\\x02" > "$0-out"; '
          'cat "$0-out"')
    _stub(bin_dir, "pg_dump", log, f"cat << 'EOF'\n{DUMP_OK.decode()}EOF")
    r = _run(bk, env, tmp_path)
    assert r.returncode != 0, "hasil gzip korup wajib ditolak"
    assert not _final(bk).exists()
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == "20260101-000000"


def test_stamp_karakter_aneh_ditolak(tmp_path):
    """STAMP masuk nama file — karakter di luar [0-9A-Za-z_-] ditolak
    (filename-safe, anti injeksi path)."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub(bin_dir, "pg_dump", log, f"cat << 'EOF'\n{DUMP_OK.decode()}EOF")
    for bad in ("20261105;rm -rf /", "../../etc", "stamp$(x)"):
        env["STAMP"] = bad
        r = _run(bk, env, tmp_path)
        assert r.returncode != 0, f"STAMP '{bad}' wajib ditolak"
        assert "STAMP tidak valid" in r.stdout


def test_backup_run_gagal_rollback_tetap_tolak_backup_lama(tmp_path):
    """Rantai lengkap round-4 #2 (perintah nyata): backup run ini GAGAL
    -> LAST_PRE_DEPLOY tetap menunjuk backup lama -> rollback.sh (dgn
    stamp run ini) WAJIB menolak backup lama dan TIDAK mengubah apa
    pun. Bukti bahwa 'stamp tidak berubah saat gagal' tidak
    menyelamatkan dump lama dari penolakan."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    (bk / "LAST_PRE_DEPLOY").write_text("20260101-000000\n")
    _stub(bin_dir, "pg_dump", log, "exit 0")    # dump kosong -> gagal
    rb = _run(bk, env, tmp_path)
    assert rb.returncode != 0
    # backup lama valid tetap ada di direktori (dari run sebelumnya)
    old = bk / "pre-deploy-20260101-000000.sql.gz"
    old.write_bytes(gzip.compress(DUMP_OK))
    # jalankan rollback sungguhan dengan stamp run yang GAGAL ini
    rlog = tmp_path / "rollback-calls.log"
    stubbin = tmp_path / "stubbin-rb"
    stubbin.mkdir()
    for name, body in {
        "systemctl": "", "dropdb": "", "createdb": "",
        # psql nyata menghabiskan stdin (seluruh dump) — hindari SIGPIPE
        "psql": "cat > /dev/null",
        "git": "", "pip": "", "curl": "echo 200",
    }.items():
        p = stubbin / name
        p.write_text(f'#!/bin/bash\necho "{name} $*" >> {rlog}\n{body}\n')
        p.chmod(0o755)
    p = stubbin / "sudo"
    p.write_text('#!/bin/bash\n'
                 f'echo "sudo $*" >> {rlog}\n'
                 'if [ "$1" = "-u" ]; then shift 2; fi\nexec "$@"\n')
    p.chmod(0o755)
    app = tmp_path / "opt" / "transcendence-hlwon-"
    (app / "backend" / ".venv" / "bin").mkdir(parents=True)
    (app / "backend" / ".venv" / "bin" / "activate").write_text("# stub\n")
    (app / "backend" / "requirements.txt").write_text("flask\n")
    envf = tmp_path / "backend.env"
    envf.write_text("DATABASE_URL=postgresql://app:pw@127.0.0.1/transcendence\n")
    renv = {
        "APP_DIR": str(app),
        "VENV_DIR": str(app / "backend" / ".venv"),
        "SERVICE": "transcendence-backend",
        "ENV_FILE": str(envf),
        "BACKUP_DIR": str(bk),
        "DB_NAME": "transcendence",
        "RESTORE_DB_NAME": "transcendence_restore",
        "HEALTH_URL": "http://127.0.0.1:8000/api/v1/health",
        "PREVIOUS_COMMIT": "abc1234",
        # stamp run yang GAGAL ini — dump-nya TIDAK ADA
        "PRE_DEPLOY_STAMP": STAMP,
        "PATH": f"{stubbin}:/usr/bin:/bin",
    }
    r = subprocess.run(["bash", str(ROLLBACK_SH)],
                       env={**os.environ, **renv},
                       capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 1, "rollback wajib menolak (backup run gagal)"
    calls = rlog.read_text() if rlog.exists() else ""
    assert "gunzip" not in calls and "psql" not in calls, \
        "backup lama TIDAK boleh di-restore saat backup run ini gagal"
    assert "systemctl" not in calls, "service tidak boleh disentuh"
    assert envf.read_text() == \
        "DATABASE_URL=postgresql://app:pw@127.0.0.1/transcendence\n", \
        ".env tidak boleh berubah"
