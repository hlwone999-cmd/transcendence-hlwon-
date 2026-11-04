"""TEMUAN AUDIT ROUND-3 #3 — logika rollback deployment benar-benar diuji.

backend/deploy/rollback.sh dijalankan DI VPS oleh workflow saat deploy
gagal. Skrip ini diuji di sini dengan STUB perintah berbahaya
(systemctl, dropdb, createdb, psql, git, pip, curl) pada PATH depan:

- jalur NORMAL (PREVIOUS_COMMIT tercatat): checkout commit lama ->
  restore dump pra-deploy ke RESTORE_DB_NAME -> .env diarahkan ke
  restore -> restart -> health OK.
- jalur FIRST-DEPLOY (PREVIOUS_COMMIT=NONE): TEMUAN #3 — dilarang
  "restore DB lalu biarkan kode baru jalan dengan skema lama".
  Wajib: systemctl STOP, restore ke DATABASE UTAMA, .env TIDAK diubah,
  TIDAK restart (service tetap berhenti, retry manual).
- dump pra-deploy tidak ada -> rollback MENGUNDUR DIRI tanpa perubahan
  apa pun (exit 1) — tidak menyentuh DB/service.
- health gagal setelah rollback normal -> exit 1 (intervensi manual).
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


def _siapkan(tmp_path: Path, prev: str):
    """Bangun dunia palsu: backup dir + dump gzip + stub bin + repo git."""
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

    # dump pra-deploy sungguhan (gzip)
    stamp = "20261104-120000"
    (bk / f"pre-deploy-{stamp}.sql.gz").write_bytes(
        gzip.compress(b"-- dump pra-deploy simulasi"))
    (bk / "LAST_PRE_DEPLOY").write_text(stamp + "\n")
    (bk / "PREVIOUS_COMMIT").write_text(prev + "\n")

    # stub perintah: catat pemanggilan, lalu perilaku minimal
    # cat == /bin/cat asli; cat.log == logging stub
    stubs = {
        "systemctl": "",
        "dropdb": "",
        "createdb": "",
        "psql": "",
        "git": "",
        "pip": "",
        "curl": "echo 200",          # health selalu OK (diubah test lain)
        "gunzip": None,              # pakai /bin/gunzip asli
    }
    for name, body in stubs.items():
        if body is None:
            continue
        p = bin_dir / name
        p.write_text(f'#!/bin/bash\necho "{name} $*" >> {log}\n{body}\n')
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
    return app, bk, bin_dir, log, stamp


def _env_for(tmp_path, app, bk, extra=None):
    e = {k: v.format(app=app, env=tmp_path / "backend.env", bk=bk)
         for k, v in ENV.items()}
    e["ENV_FILE"] = str(tmp_path / "backend.env")
    (tmp_path / "backend.env").write_text(
        "DATABASE_URL=postgresql://app:pw@127.0.0.1/transcendence\n")
    e.update(extra or {})
    return e


def _run(bin_dir, env, tmp_path):
    path = f"{bin_dir}:/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(ROLLBACK)], env={**os.environ, "PATH": path, **env},
        capture_output=True, text=True, cwd=str(tmp_path))


def test_rollback_normal_kode_lama_plus_db_restore(tmp_path):
    app, bk, bin_dir, log, stamp = _siapkan(tmp_path, prev="abc1234")
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    # kode kembali ke commit ter-deploy sebelumnya (BUKAN origin/main)
    assert "git checkout --detach abc1234" in calls
    assert "reset --hard" not in calls
    # dump pra-deploy di-restore ke RESTORE_DB_NAME
    assert f"psql -q transcendence_restore" in calls
    assert "createdb transcendence_restore" in calls
    # service di-restart + health dicek
    assert "systemctl restart transcendence-backend" in calls
    assert "ROLLBACK OK" in r.stdout
    # .env diarahkan ke database restore
    env_txt = (tmp_path / "backend.env").read_text()
    assert "/transcendence_restore" in env_txt


def test_rollback_first_deploy_hentikan_service_bukan_kode_baru(tmp_path):
    """INTI TEMUAN #3: PREVIOUS_COMMIT=NONE — TIDAK boleh ada restart
    kode baru di atas DB lama. Wajib: stop service, restore ke DATABASE
    UTAMA, .env TIDAK diubah, TIDAK restart."""
    app, bk, bin_dir, log, stamp = _siapkan(tmp_path, prev="NONE")
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    # service DIHENTIKAN dan TIDAK di-restart
    assert "systemctl stop transcendence-backend" in calls
    assert "systemctl restart" not in calls
    # TIDAK ada checkout kode lama (tidak ada kode lama!)
    assert "git checkout" not in calls
    # database utama dikembalikan ke pra-deploy
    assert "createdb transcendence" in calls
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
    app, bk, bin_dir, log, stamp = _siapkan(tmp_path, prev="NONE")
    # buang semua dump — rollback tidak boleh menyentuh apa pun
    for f in bk.glob("pre-deploy-*.sql.gz"):
        f.unlink()
    r = _run(bin_dir, _env_for(tmp_path, app, bk), tmp_path)
    assert r.returncode == 1
    assert not log.exists() or log.read_text() == "", \
        "tanpa dump: tidak boleh ada systemctl/dropdb/createdb/psql"
    env_txt = (tmp_path / "backend.env").read_text()
    assert "/transcendence\n" in env_txt, ".env tidak boleh berubah"
    assert "intervensi manual" in r.stdout.lower()


def test_rollback_normal_health_gagal_exit_1(tmp_path):
    app, bk, bin_dir, log, stamp = _siapkan(tmp_path, prev="def5678")
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
                "RESTORE_DB_NAME=", "HEALTH_URL="):
        assert var in raw, f"env {var} wajib diteruskan ke rollback.sh"
    # rollback hanya jalan saat deployment gagal
    assert re.search(r"if:\s*failure\(\)", raw), \
        "step rollback wajib if: failure()"
