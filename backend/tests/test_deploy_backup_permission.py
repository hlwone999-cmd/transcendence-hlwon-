"""TEMUAN AUDIT ROUND-3 #1 — izin direktori backup deployment.

Masalah: `sudo mkdir -p /var/backups/transcendence` membuat direktori
MILIK ROOT, sedangkan `gzip > $BACKUP_DIR/...` (dan `chmod`) dijalankan
sebagai user deploy — penulisan GAGAL (Permission denied) atau, bila
admin kebetulan meng-chown manual, kebijakan tidak terjaga.

Perbaikan (backend-deploy.yml): `sudo install -d -o deploy -g deploy
-m 750 $BACKUP_DIR` — idempoten, owner/mode dijaga SEBELUM penulisan;
dump ditulis user deploy (umask 077 + chmod 600), tanpa sudo tee.

Pengujian 3 lapis (jujur soal batas sandbox):
1. STATIK    — workflow memakai install -d -o deploy + urutan benar
               (izin dipasang SEBELUM penulisan dump), tanpa sudo tee,
               dump chmod 600. Selalu jalan.
2. SIMULASI  — perilaku: direktori tanpa izin tulis -> penulisan gagal;
               setelah langkah perbaikan (pasang izin yang benar) ->
               penulisan berhasil, file 600, direktori 750. Selalu jalan
               (meniru langkah workflow pada direktori milik sendiri).
3. LIVE root — bila passwordless sudo tersedia (CI ubuntu-latest):
               buat direktori root-owned sungguhan -> tulis sebagai user
               biasa GAGAL -> `sudo install -d -o <user>` -> BERHASIL.
               Di sandbox tanpa sudo: SKIP dengan alasan eksplisit.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY_WF = REPO / ".github" / "workflows" / "backend-deploy.yml"


# ---------------------------------------------------------------- 1. statik
def test_workflow_pakai_install_d_owner_deploy():
    raw = DEPLOY_WF.read_text(encoding="utf-8")
    # langkah perbaikan izin wajib ada dan menarget owner deploy
    m = re.findall(r"install -d -o \$BACKUP_OWNER -g \$BACKUP_OWNER"
                   r" -m 750 \$BACKUP_DIR", raw)
    assert len(m) >= 2, \
        "workflow wajib memasang izin backup (owner deploy, 750) di " \
        "step pencatatan commit DAN step backup"
    assert "BACKUP_OWNER: deploy" in raw, \
        "BACKUP_OWNER harus user SSH deploy"


def test_workflow_tidak_ada_sudo_mkdir_backup_dir():
    raw = DEPLOY_WF.read_text(encoding="utf-8")
    assert not re.search(r"sudo\s+mkdir\s+-p\s+\$BACKUP_DIR", raw), \
        "`sudo mkdir -p $BACKUP_DIR` (root-owned) masih ada — sumber masalah"


def test_backup_dump_ditulis_tanpa_sudo_tee_dan_chmod_600():
    raw = DEPLOY_WF.read_text(encoding="utf-8")
    assert re.search(r"gzip >\s*\\\n?\s*\$BACKUP_DIR/pre-deploy-", raw) or \
        re.search(r"gzip > \$BACKUP_DIR/pre-deploy-", raw), \
        "dump harus ditulis langsung oleh user deploy (gzip > file)"
    assert not re.search(r"sudo tee \$BACKUP_DIR", raw), \
        "sudo tee ke $BACKUP_DIR dilarang — file backup milik deploy"
    assert "umask 077" in raw, "umask 077 wajib sebelum menulis dump"
    assert re.search(r"chmod 600 \$BACKUP_DIR/pre-deploy-", raw), \
        "dump berisi data transaksi — wajib 600"


def test_izin_dipasang_sebelum_penulisan_dump():
    """Urutan wajib: install -d (izin) harus muncul SEBELUM baris
    penulisan dump pada step backup."""
    raw = DEPLOY_WF.read_text(encoding="utf-8")
    lines = raw.splitlines()
    idx_install = [i for i, ln in enumerate(lines)
                   if "install -d -o" in ln]
    idx_dump = [i for i, ln in enumerate(lines)
                if re.search(r"gzip >", ln)]
    assert idx_install and idx_dump, "step izin/dump tidak ditemukan"
    assert min(idx_install) < min(idx_dump), \
        "izin direktori harus dipasang SEBELUM penulisan dump"


# --------------------------------------------------------------- 2. simulasi
def _simulasi_langkah_workflow(tmp_path: Path) -> None:
    """Meniru persis semantik langkah workflow pada satu direktori:
    (a) `install -d -o <owner> -g <owner> -m 750` = mkdir -p + chown
        owner + chmod 750 — idempoten (kita terapkan manual, sandbox
        tanpa sudo tidak bisa chown, jadi owner = user sekarang);
    (b) umask 077 + tulis dump via redirect + chmod 600;
    (c) tulis stamp file + chmod 600.
    """
    d = tmp_path / "backups"
    # (a) ekuivalen install -d (mode penuh diterapkan eksplisit)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o750)
    # (b) tulis dump: directory owner-writable, file 600 via umask
    old_umask = os.umask(0o077)
    try:
        dump = d / "pre-deploy-20261104-120000.sql.gz"
        with open(dump, "wb") as f:          # ekuivalen `gzip > $file`
            f.write(b"PGDMP\x01\x02simulasi-dump")
        os.chmod(dump, 0o600)                # ekuivalen `chmod 600`
        stamp = d / "LAST_PRE_DEPLOY"
        stamp.write_text("20261104-120000\n")
        os.chmod(stamp, 0o600)
    finally:
        os.umask(old_umask)
    # hasil: mode benar, isi terbaca oleh user deploy
    assert stat.S_IMODE(d.stat().st_mode) == 0o750
    assert stat.S_IMODE(dump.stat().st_mode) == 0o600
    assert dump.read_bytes().startswith(b"PGDMP")


def test_simulasi_langkah_izin_backup_berhasil(tmp_path):
    _simulasi_langkah_workflow(tmp_path)


def test_simulasi_directori_tanpa_izin_tulis_gagal(tmp_path):
    """Bukti bahwa izin yang salah MENYEBABKAN kegagalan penulisan —
    alasan perbaikan ini ada (PermissionError, bukan diam-diam OK)."""
    d = tmp_path / "backups-root-owned"
    d.mkdir()
    os.chmod(d, 0o555)            # r-x: TIDAK bisa menulis (dan owner
    #                                bukan kita pada kasus nyata root:root)
    try:
        with pytest.raises(PermissionError):
            (d / "pre-deploy-x.sql.gz").write_bytes(b"x")
    finally:
        os.chmod(d, 0o750)        # bereskan agar tmp_path bisa dihapus
    # setelah langkah perbaikan workflow (izin dipasang ulang):
    os.chmod(d, 0o750)
    (d / "pre-deploy-x.sql.gz").write_bytes(b"x")
    assert (d / "pre-deploy-x.sql.gz").exists()


# ---------------------------------------------------------------- 3. live root
def test_live_root_owned_dir_diperbaiki_install_d(tmp_path):
    """Hanya di lingkungan dengan passwordless sudo (CI ubuntu-latest):
    direktori ROOT-OWNED sungguhan -> tulis GAGAL -> `sudo install -d
    -o <user> -g <group> -m 750` -> tulis BERHASIL (persis perbaikan
    workflow). Sandbox tanpa sudo: skip jujur (bukan lulus palsu)."""
    sudo_ok = True
    try:
        subprocess.run(["sudo", "-n", "true"], check=True,
                       capture_output=True)
    except Exception:
        sudo_ok = False
    if not sudo_ok:
        pytest.skip("passwordless sudo tidak tersedia di sandbox — "
                    "simulasi tanpa hak root sudah membuktikan semantik "
                    "izin; jalankan test ini di CI (ubuntu-latest)")
    d = tmp_path / "rootdir"
    subprocess.run(["sudo", "mkdir", "-p", str(d)], check=True)
    subprocess.run(["sudo", "chmod", "750", str(d)], check=True)
    # kondisi temuan: direktori root-owned -> user deploy (kita) GAGAL
    with pytest.raises(PermissionError):
        (d / "pre-deploy.sql.gz").write_bytes(b"x")
    # perbaikan workflow: install -d -o deploy ... (di sini: user aktif)
    subprocess.run(["sudo", "install", "-d",
                    "-o", str(os.getuid()), "-g", str(os.getgid()),
                    "-m", "750", str(d)], check=True)
    (d / "pre-deploy.sql.gz").write_bytes(b"PGDMP")
    assert stat.S_IMODE(d.stat().st_mode) == 0o750
    assert (d / "pre-deploy.sql.gz").read_bytes() == b"PGDMP"
