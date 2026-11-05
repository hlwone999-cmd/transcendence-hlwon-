"""TEMUAN AUDIT ROUND-3 #3 + ROUND-4 #2 — rollback deployment diuji.

backend/deploy/rollback.sh dijalankan DI VPS oleh workflow saat deploy
gagal. Skrip ini diuji di sini dengan STUB perintah berbahaya
(systemctl, dropdb, createdb, psql, git, pip, curl) pada PATH depan:

ROUND-3 (tetap):
- jalur NORMAL (PREVIOUS_COMMIT tercatat): checkout commit lama ->
  restore dump pra-deploy ke RESTORE_DB_NAME -> .env diarahkan ke
  restore -> restart -> health OK.
- jalur FIRST-DEPLOY (PREVIOUS_COMMIT=NONE): dilarang "restore DB lalu
  biarkan kode baru jalan dengan skema lama". Wajib: systemctl STOP,
  restore ke DATABASE UTAMA, .env TIDAK diubah, TIDAK restart.
- dump tidak ada -> rollback MENGUNDUR DIRI tanpa perubahan (exit 1).
- health gagal setelah rollback normal -> exit 1.

ROUND-4 #2 (baru) — rollback TIDAK BOLEH memakai backup lama saat run
gagal:
- TANPA PRE_DEPLOY_STAMP -> ditolak (tidak ada perubahan, exit 1) —
  rollback tidak boleh menebak backup dari LAST_PRE_DEPLOY.
- stamp run ini tapi dump TIDAK ADA (simulasi backup run gagal) —
  dump LAMA + LAST_PRE_DEPLOY sengaja disediakan, dan rollback WAJIB
  MENOLAK semuanya (tidak satu pun perintah restore dipanggil).
- dump run ini KORUP (bukan gzip valid) -> ditolak.
- jalur normal terbukti memakai dump MILIK RUN INI (bukan dump lama):
  stub gunzip mencatat path file yang benar-benar dibaca.
"""
import gzip
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROLLBACK = REPO / "backend" / "deploy" / "rollback.sh"

ENV = {
    "APP_DIR": "{app}",
    "VENV_DIR": "{app}/backend/.venv",
    "SERVICE": "transcendence-backend",
    "ENV_FILE": "{env}",
    "BACKUP_DIR": "{bk}",
    "DB_NAME": "transcendence",
    "RESTORE_DB_NAME": "transcendence_restore",
    "HEALTH_URL": "http://127.0.0.1:8000/api/v1/health",
}

# stamp run LAMA (backup valid sebelumnya) vs run INI (yang sedang deploy)
OLD_STAMP = "20261104-120000"
CUR_STAMP = "20261105-090000"

DUMP_PG = b"--\n-- PostgreSQL database dump\n--\n-- data simulasi\n"


def _siapkan(tmp_path: Path, prev: str,
             dump_cur: bytes | None = gzip.compress(DUMP_PG)):
    """Bangun dunia palsu: backup dir (dump lama + run ini) + stub bin +
    repo git + venv. `dump_cur` = byte PERSIS yang ditulis ke file dump
    run ini (default: dump PG valid ter-gzip; None = TIDAK dibuat,
    simulasi backup run ini GAGAL; byte non-gzip = simulasi korup)."""
    app = tmp_path / "opt" / "transcendence-hlwon-"
    bk = tmp_path / "var" / "backups" / "transcendence"
    bin_dir = tmp_path / "stubbin"
    log = tmp_path / "calls.log"
    for p in (app, bk, bin_dir):
        p.mkdir(parents=True, exist_ok=True)
    (app / "backend").mkdir(parents=True, exist_ok=True)
    (app / "backend" / "requirements.txt").write_text("flask\n")
    # venv palsu: rollback.sh melakukan `source .../activate`
    venv_bin = app / "backend" / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "activate").write_text("# venv stub\n")

    # dump run LAMA (valid) + stamp "terakhir valid" menunjuk dump lama —
    # PERSIS kondisi berbahaya yang wajib ditolak rollback saat run ini
    # gagal backup (round-4 #2).
    (bk / f"pre-deploy-{OLD_STAMP}.sql.gz").write_bytes(
        gzip.compress(DUMP_PG))
    (bk / "LAST_PRE_DEPLOY").write_text(OLD_STAMP + "\n")
    (bk / "PREVIOUS_COMMIT").write_text(prev + "\n")
    if dump_cur is not None:
        (bk / f"pre-deploy-{CUR_STAMP}.sql.gz").write_bytes(dump_cur)

    # stub perintah: catat pemanggilan, lalu perilaku minimal
    stubs = {
        "systemctl": "",
        "dropdb": "",
        "createdb": "",
        # psql nyata MENGHABISKAN stdin (seluruh dump) — stub wajib sama,
        # kalau tidak gunzip mendapat SIGPIPE (141) di bawah pipefail
        "psql": "cat > /dev/null",
        "git": "",
        "pip": "",
        "curl": "echo 200",          # health selalu OK (diubah test lain)
    }
    for name, body in stubs.items():
        p = bin_dir / name
        p.write_text(f'#!/bin/bash\necho "{name} $*" >> {log}\n{body}\n')
        p.chmod(0o755)
    # stub gunzip: catat path dump yang dibaca, lalu jalankan gunzip
    # asli (agar isi dump benar-benar didekompresi)
    p = bin_dir / "gunzip"
    p.write_text(f'#!/bin/bash\necho "gunzip $*" >> {log}\n'
                 'exec /bin/gunzip "$@"\n')
    p.chmod(0o755)
    # stub sudo: lewati -u <user> lalu eksekusi stub perintah aslinya —
    # tanpa ini, `sudo systemctl ...` memanggil sudo NYATA (butuh tty)
    p = bin_dir / "sudo"
    p.write_text('#!/bin/bash\n'
                 f'echo "sudo $*" >> {log}\n'
                 'if [ "$1" = "-u" ]; then shift 2; fi\n'
                 'exec "$@"\n')
    p.chmod(0o755)

    # repo git palsu di APP_DIR (untuk git fetch/checkout stub)
    return app, bk, bin_dir, log


def _env_for(tmp_path, app, bk, extra=None):
    e = {k: v.format(app=app, env=tmp_path / "backend.env", bk=bk)
         for k, v in ENV.items()}
    e["ENV_FILE"] = str(tmp_path / "backend.env")
    (tmp_path / "backend.env").write_text(
        "DATABASE_URL=postgresql://app:pw@127.0.0.1/transcendence\n")
    e["PRE_DEPLOY_STAMP"] = CUR_STAMP     # kontrak round-4 (workflow kirim)
    e.update(extra or {})
    return e


def _run(bin_dir, env, tmp_path):
    path = f"{bin_dir}:/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(ROLLBACK)], env={**os.environ, "PATH": path, **env},
        capture_output=True, text=True, cwd=str(tmp_path))


# ============================================== ROUND-3 (kontrak lama)
def test_rollback_normal_kode_lama_plus_db_restore(tmp_path):
    app, bk, bin_dir, log = _siapkan(tmp_path, prev="abc1234")
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    # kode kembali ke commit ter-deploy sebelumnya (BUKAN origin/main)
    assert "git checkout --detach abc1234" in calls
    assert "reset --hard" not in calls
    # dump pra-deploy di-restore ke RESTORE_DB_NAME
    assert "psql -q transcendence_restore" in calls
    assert "createdb transcendence_restore" in calls
    # service di-restart + health dicek
    assert "systemctl restart transcendence-backend" in calls
    assert "ROLLBACK OK" in r.stdout
    # .env diarahkan ke database restore
    env_txt = (tmp_path / "backend.env").read_text()
    assert "/transcendence_restore" in env_txt


def test_rollback_first_deploy_hentikan_service_bukan_kode_baru(tmp_path):
    """INTI ROUND-3 #3: PREVIOUS_COMMIT=NONE — TIDAK boleh ada restart
    kode baru di atas DB lama. Wajib: stop service, restore ke DATABASE
    UTAMA, .env TIDAK diubah, TIDAK restart."""
    app, bk, bin_dir, log = _siapkan(tmp_path, prev="NONE")
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    # service DIHENTIKAN dan TIDAK di-restart
    assert "systemctl stop transcendence-backend" in calls
    assert "systemctl restart" not in calls
    # TIDAK ada checkout kode lama (tidak ada kode lama!)
    assert "git checkout" not in calls
    # database utama dikembalikan ke pra-deploy
    assert "createdb transcendence\n" in calls or \
        "createdb transcendence " in calls
    assert "psql -q transcendence\n" in calls or \
        "psql -q transcendence " in calls
    assert "transcendence_restore" not in calls.replace(
        "RESTORE_DB_NAME", ""), "first-deploy tidak boleh pakai DB restore"
    # .env TIDAK diubah (tetap database utama)
    env_txt = (tmp_path / "backend.env").read_text()
    assert "/transcendence\n" in env_txt and \
        "transcendence_restore" not in env_txt
    assert "retry" in r.stdout.lower() or "MANUAL" in r.stdout


def test_rollback_tanpa_dump_batalkan_tanpa_perubahan(tmp_path):
    """Stamp run ini ada, tetapi dump-nya TIDAK ada — tidak boleh
    menyentuh apa pun (exit 1)."""
    app, bk, bin_dir, log = _siapkan(tmp_path, prev="NONE", dump_cur=None)
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 1
    assert not log.exists() or log.read_text() == "", \
        "tanpa dump: tidak boleh ada systemctl/dropdb/createdb/psql"
    env_txt = (tmp_path / "backend.env").read_text()
    assert "/transcendence\n" in env_txt, ".env tidak boleh berubah"
    assert "intervensi manual" in r.stdout.lower()


def test_rollback_normal_health_gagal_exit_1(tmp_path):
    app, bk, bin_dir, log = _siapkan(tmp_path, prev="def5678")
    # stub curl: health selalu 503
    (bin_dir / "curl").write_text(
        f'#!/bin/bash\necho "curl $*" >> {log}\necho 503\n')
    (bin_dir / "curl").chmod(0o755)
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 1
    assert "GAGAL" in r.stdout


def test_rollback_script_terpasang_di_workflow():
    raw = (REPO / ".github" / "workflows" / "backend-deploy.yml"
           ).read_text(encoding="utf-8")
    assert "backend/deploy/rollback.sh" in raw
    assert "bash -s" in raw, "skrip dikirim ke VPS via stdin (bash -s)"
    for var in ("APP_DIR=", "BACKUP_DIR=", "DB_NAME=",
                "RESTORE_DB_NAME=", "HEALTH_URL=", "PRE_DEPLOY_STAMP="):
        assert var in raw, f"env {var} wajib diteruskan ke rollback.sh"
    # rollback hanya jalan saat deployment gagal
    assert re.search(r"if:\s*failure\(\)", raw), \
        "step rollback wajib if: failure()"


# ============================================== ROUND-4 #2 (kontrak baru)
def test_rollback_tanpa_stamp_ditolak_backup_lama_tidak_dipakai(tmp_path):
    """TANPA PRE_DEPLOY_STAMP: rollback tidak boleh menebak backup dari
    LAST_PRE_DEPLOY — ditolak total, tidak ada perubahan."""
    app, bk, bin_dir, log = _siapkan(tmp_path, prev="abc1234")
    r = _run(bin_dir, _env_for(tmp_path, app, bk,
                               extra={"PRE_DEPLOY_STAMP": ""}), tmp_path)
    assert r.returncode == 1
    assert not log.exists() or log.read_text() == "", \
        "tanpa stamp: tidak boleh ada restore/service/git sama sekali"
    env_txt = (tmp_path / "backend.env").read_text()
    assert "/transcendence\n" in env_txt, ".env tidak boleh berubah"
    assert "PRE_DEPLOY_STAMP" in r.stdout
    assert "intervensi manual" in r.stdout.lower()


def test_backup_run_gagal_rollback_tidak_pakai_backup_lama(tmp_path):
    """INTI ROUND-4 #2: backup run INI gagal (dump tidak ada), tetapi
    dump LAMA + LAST_PRE_DEPLOY masih menunjuknya — rollback WAJIB
    menolak dump lama (bukan pasangan pra-deploy run ini)."""
    app, bk, bin_dir, log = _siapkan(tmp_path, prev="abc1234",
                                     dump_cur=None)
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 1
    calls = log.read_text() if log.exists() else ""
    # TIDAK ada satu pun aksi restore/service/git
    assert "gunzip" not in calls, "dump lama tidak boleh dibaca"
    assert "psql" not in calls and "createdb" not in calls, \
        "restore DB tidak boleh terjadi dengan backup run yang gagal"
    assert "systemctl" not in calls, "service tidak boleh disentuh"
    assert "git checkout" not in calls
    # .env tetap menunjuk database utama
    env_txt = (tmp_path / "backend.env").read_text()
    assert "/transcendence\n" in env_txt
    # pesan menjelaskan penolakan backup lama
    assert "backup LAMA tidak boleh dipakai" in r.stdout
    assert "intervensi manual" in r.stdout.lower()


def test_dump_korup_ditolak_tanpa_perubahan(tmp_path):
    """Dump run ini ADA tetapi korup (bukan gzip valid) — rollback
    menolak dan tidak menyentuh apa pun (backup lama juga dilarang)."""
    app, bk, bin_dir, log = _siapkan(tmp_path, prev="abc1234",
                                     dump_cur=b"inilah-bukan-gzip\x00\x01")
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 1
    calls = log.read_text() if log.exists() else ""
    assert "gunzip" not in calls and "psql" not in calls
    assert "systemctl" not in calls
    env_txt = (tmp_path / "backend.env").read_text()
    assert "/transcendence\n" in env_txt


def test_dump_bukan_pg_ditolak_tanpa_perubahan(tmp_path):
    """Dump run ini gzip VALID tetapi isinya bukan dump PostgreSQL
    (mis. keluaran error) — marker check menolak."""
    app, bk, bin_dir, log = _siapkan(
        tmp_path, prev="abc1234",
        dump_cur=gzip.compress(b"pg_dump: error: connection refused\n"))
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 1
    calls = log.read_text() if log.exists() else ""
    assert "psql" not in calls and "createdb" not in calls
    assert "systemctl" not in calls


def test_rollback_normal_memakai_dump_run_ini_bukan_lama(tmp_path):
    """Terbukti via stub gunzip: dump yang dibaca = pre-deploy-<STAMP
    RUN INI>.sql.gz — BUKAN dump lama yang ditunjuk LAST_PRE_DEPLOY."""
    app, bk, bin_dir, log = _siapkan(tmp_path, prev="abc1234")
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    assert f"gunzip -c {bk}/pre-deploy-{CUR_STAMP}.sql.gz" in calls, \
        "rollback wajib memakai dump milik run ini"
    assert f"pre-deploy-{OLD_STAMP}.sql.gz" not in calls, \
        "dump lama (LAST_PRE_DEPLOY) tidak boleh dipakai rollback"
    assert r.returncode == 0
