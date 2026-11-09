#!/usr/bin/env python3
"""Helper provisioning terminal MT5 — SATU-SATUNYA komponen ber-hak root.

Pemisahan hak (Tahap 3B): API backend berjalan sebagai user tanpa hak
root dan TIDAK punya akses systemd bebas. API hanya boleh menjalankan
helper ini lewat sudo dengan aturan tunggal:

    transcendence ALL=(root) NOPASSWD: /usr/local/sbin/transcendence-mt5-provision

Helper dipasang ROOT-OWNED (0755) di luar direktori kode API sehingga
API tidak dapat mengubahnya. Seluruh parameter divalidasi ketat:

- operasi tetap: install | start | stop | remove | status
- --code         : ^C[0-9]{3,9}$  (dibangkitkan server dari id customer)
- --port         : integer di jendela [port_base, port_base+max_slots)
- --provision-id : ^[0-9a-f]{32}$ (uuid4 hex dibangkitkan server)
- path, nama unit, user layanan, binary Wine, direktori template:
  DITURUNKAN dari file konfigurasi root-only CONFIG_PATH — tidak pernah
  dari argumen/ENV pemanggil. Tidak ada eksekusi shell; subprocess hanya
  memakai argv list dengan path absolut dari konfigurasi.
- remove HANYA bila penanda kepemilikan (.provision_id) di direktori
  customer sama persis dengan --provision-id. Tanpa bukti kepemilikan,
  tidak ada yang dihapus.
- install menolak port yang sudah dipakai unit customer LAIN.

Keluaran: satu baris JSON di stdout; exit 0 sukses, 2 argumen tidak sah,
3 kepemilikan tidak terbukti / konflik, 4 kegagalan operasi.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = "/etc/transcendence/provisioner.json"
UNIT_PREFIX = "transcendence-mt5-"
MARKER = ".provision_id"

CODE_RE = re.compile(r"^C[0-9]{3,9}$")
PID_RE = re.compile(r"^[0-9a-f]{32}$")
OPS = ("install", "start", "stop", "remove", "status")

EXIT_OK, EXIT_ARGS, EXIT_OWNERSHIP, EXIT_FAIL = 0, 2, 3, 4

# ------------------------------------------------------------- templates
# Server RPyC (mt5server) HANYA bind 127.0.0.1 — lihat docs/RPYC-SECURITY.md
# untuk pembatasan tambahan (firewall owner-match, layanan read-only).
UNIT_TEMPLATE = """# Dibangkitkan helper provisioning — JANGAN diedit manual.
[Unit]
Description=Transcendence MT5 Terminal + RPyC ({code}, localhost:{port})
After=network.target

[Service]
Type=simple
User={user}
Environment=WINEPREFIX={wine_prefix}
Environment=WINEDEBUG=-all
WorkingDirectory={terminal_dir}
ExecStart=/bin/sh {terminal_dir}/run-terminal.sh
ExecStop=/bin/sh {terminal_dir}/stop-terminal.sh
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={base}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost
IPAddressAllow={broker_allow}

[Install]
WantedBy=multi-user.target
"""

RUN_TEMPLATE = """#!/bin/sh
set -eu
export WINEPREFIX="{wine_prefix}"
export WINEDEBUG=-all
if ! pgrep -f "terminal64.exe.*{terminal_dir}" >/dev/null 2>&1; then
    "{wine_bin}" "{terminal_dir}/terminal64.exe" /portable &
fi
exec "{wine_bin}" "{terminal_dir}/mt5server.exe" --host 127.0.0.1 --port {port}
"""

# Tahap 4: varian layanan RPC READ-ONLY bertoken (mt5client/ro_service.py
# disalin ke template_dir) — menggantikan mt5server RPyC classic.
RUN_TEMPLATE_RO = """#!/bin/sh
set -eu
export WINEPREFIX="{wine_prefix}"
export WINEDEBUG=-all
if ! pgrep -f "terminal64.exe.*{terminal_dir}" >/dev/null 2>&1; then
    "{wine_bin}" "{terminal_dir}/terminal64.exe" /portable &
fi
exec "{wine_bin}" "{wine_python}" "{terminal_dir}/ro_service.py" --port {port} --token-file "{terminal_dir}/rpc.token"
"""

STOP_TEMPLATE = """#!/bin/sh
pkill -TERM -f "terminal64.exe.*{terminal_dir}" 2>/dev/null || true
pkill -TERM -f "mt5server.*--port {port}" 2>/dev/null || true
sleep 2
pkill -KILL -f "terminal64.exe.*{terminal_dir}" 2>/dev/null || true
pkill -KILL -f "mt5server.*--port {port}" 2>/dev/null || true
exit 0
"""


class HelperError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


@dataclass(frozen=True)
class HelperConfig:
    base_dir: Path            # mis. /var/lib/transcendence/terminals
    template_dir: Path        # instalasi MT5 "emas" (terminal64.exe dst.)
    unit_dir: Path            # /etc/systemd/system
    service_user: str         # user non-root layanan terminal
    port_base: int
    max_slots: int
    systemctl: str = "/usr/bin/systemctl"
    wine_bin: str = "/usr/bin/wine"
    broker_allow: str = "any"  # opsional: daftar CIDR server broker
    proc_root: Path = Path("/proc")
    ro_service: bool = False   # True: izinkan --rpc-mode ro
    # "per_customer" (disarankan): user sistem mt5-<kode> + grup privat per
    # customer -> proses terminal satu customer TIDAK dapat membaca
    # direktori/token customer lain. "shared": satu service_user (lama).
    service_user_mode: str = "shared"
    useradd: str = "/usr/sbin/useradd"
    userdel: str = "/usr/sbin/userdel"
    wine_python: str = ""      # path python.exe (Windows) di prefix Wine
    rpc_secret: str = ""       # secret HMAC token (sama dgn MT5_RPC_SECRET)

    @staticmethod
    def load(path: str = CONFIG_PATH) -> "HelperConfig":
        p = Path(path)
        st = p.stat()
        # konfigurasi WAJIB root-owned & tidak dapat ditulis grup/lain
        if st.st_uid != 0 or (st.st_mode & 0o022):
            raise HelperError(EXIT_FAIL, "konfigurasi helper tidak aman")
        raw = json.loads(p.read_text(encoding="utf-8"))
        return HelperConfig(
            base_dir=Path(raw["base_dir"]),
            template_dir=Path(raw["template_dir"]),
            unit_dir=Path(raw.get("unit_dir", "/etc/systemd/system")),
            service_user=str(raw["service_user"]),
            port_base=int(raw["port_base"]),
            max_slots=int(raw["max_slots"]),
            systemctl=str(raw.get("systemctl", "/usr/bin/systemctl")),
            wine_bin=str(raw.get("wine_bin", "/usr/bin/wine")),
            broker_allow=str(raw.get("broker_allow", "any")),
            ro_service=bool(raw.get("ro_service", False)),
            service_user_mode=str(raw.get("service_user_mode", "shared")),
            useradd=str(raw.get("useradd", "/usr/sbin/useradd")),
            userdel=str(raw.get("userdel", "/usr/sbin/userdel")),
            wine_python=str(raw.get("wine_python", "")),
            rpc_secret=str(raw.get("rpc_secret", "")))


# ------------------------------------------------------------- validasi
def validate(cfg: HelperConfig, op: str, code: str, port: int | None,
             provision_id: str | None) -> None:
    if op not in OPS:
        raise HelperError(EXIT_ARGS, "operasi tidak sah")
    if not isinstance(code, str) or not CODE_RE.fullmatch(code):
        raise HelperError(EXIT_ARGS, "kode customer tidak sah")
    if op in ("install", "status", "start"):
        if (not isinstance(port, int) or isinstance(port, bool) or
                not cfg.port_base <= port < cfg.port_base + cfg.max_slots):
            raise HelperError(EXIT_ARGS, "port di luar jendela alokasi")
    if op in ("install", "remove"):
        if not isinstance(provision_id, str) or \
                not PID_RE.fullmatch(provision_id):
            raise HelperError(EXIT_ARGS, "provision-id tidak sah")
    for p in (cfg.base_dir, cfg.template_dir, cfg.unit_dir):
        if not p.is_absolute():
            raise HelperError(EXIT_FAIL, "path konfigurasi wajib absolut")
    if cfg.service_user_mode not in ("shared", "per_customer"):
        raise HelperError(EXIT_FAIL, "service_user_mode tidak sah")
    for exe in (cfg.systemctl, cfg.wine_bin, cfg.useradd, cfg.userdel):
        if not os.path.isabs(exe):
            raise HelperError(EXIT_FAIL, "binary konfigurasi wajib absolut")


def paths(cfg: HelperConfig, code: str) -> dict:
    base = cfg.base_dir / code
    # tahan symlink/traversal: hasil resolve WAJIB anak langsung base_dir
    if base.exists() and (base.is_symlink() or
                          base.resolve().parent != cfg.base_dir.resolve()):
        raise HelperError(EXIT_OWNERSHIP, "direktori customer tidak sah")
    unit = f"{UNIT_PREFIX}{code.lower()}"
    return {"base": base, "terminal": base / "terminal",
            "wine_prefix": base / "wine-prefix", "data": base / "data",
            "marker": base / MARKER, "unit": unit,
            "unit_file": cfg.unit_dir / f"{unit}.service"}


def service_user_for(cfg: HelperConfig, code: str) -> str:
    """User layanan terminal customer (DITURUNKAN, bukan dari argumen)."""
    if cfg.service_user_mode == "per_customer":
        return f"mt5-{code.lower()}"
    return cfg.service_user


def ensure_user(cfg: HelperConfig, user: str, home: Path, runner) -> None:
    import pwd
    try:
        pwd.getpwnam(user)
        return
    except KeyError:
        pass
    runner.run([cfg.useradd, "--system", "--user-group", "--no-create-home",
                "--home-dir", str(home), "--shell", "/usr/sbin/nologin",
                user])


def read_marker(p: dict) -> str | None:
    m = p["marker"]
    if not m.exists() or m.is_symlink():
        return None
    return m.read_text(encoding="utf-8").strip() or None


def port_owner(cfg: HelperConfig, port: int) -> str | None:
    """Kode customer pemilik unit yang memakai [port], bila ada."""
    if not cfg.unit_dir.is_dir():
        return None
    needle = f"localhost:{port})"
    for f in cfg.unit_dir.glob(f"{UNIT_PREFIX}*.service"):
        try:
            if needle in f.read_text(encoding="utf-8"):
                return f.name[len(UNIT_PREFIX):-len(".service")].upper()
        except OSError:
            continue
    return None


# ------------------------------------------------------------- operasi
class Runner:
    """Eksekusi perintah TANPA shell, argv list, env minimal."""

    def run(self, argv: list[str], check: bool = True) -> int:
        if not argv or not os.path.isabs(argv[0]):
            raise HelperError(EXIT_FAIL, "perintah wajib path absolut")
        r = subprocess.run(argv, shell=False, check=False,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
                           timeout=60)
        if check and r.returncode != 0:
            raise HelperError(EXIT_FAIL, f"perintah gagal ({r.returncode})")
        return r.returncode


def op_install(cfg, code, port, provision_id, runner, chown=True,
               rpc_mode: str = "classic") -> dict:
    if rpc_mode not in ("classic", "ro"):
        raise HelperError(EXIT_ARGS, "rpc-mode tidak sah")
    if rpc_mode == "ro" and not cfg.ro_service:
        raise HelperError(EXIT_FAIL, "mode ro tidak diizinkan konfigurasi")
    p = paths(cfg, code)
    user = service_user_for(cfg, code)
    owner = read_marker(p)
    if p["base"].exists() and owner not in (None, provision_id):
        raise HelperError(EXIT_OWNERSHIP,
                          "direktori dimiliki provisioning lain")
    if p["base"].exists() and owner is None and any(p["base"].iterdir()):
        raise HelperError(EXIT_OWNERSHIP,
                          "direktori tanpa bukti kepemilikan")
    other = port_owner(cfg, port)
    if other not in (None, code):
        raise HelperError(EXIT_OWNERSHIP, "port dipakai customer lain")
    if owner == provision_id and p["unit_file"].exists():
        return {"ok": True, "idempotent": True}
    p["base"].mkdir(mode=0o750, parents=False, exist_ok=True)
    p["marker"].write_text(provision_id, encoding="utf-8")
    os.chmod(p["marker"], 0o600)
    if not p["terminal"].exists():
        shutil.copytree(cfg.template_dir, p["terminal"], symlinks=False)
    p["wine_prefix"].mkdir(exist_ok=True)
    p["data"].mkdir(exist_ok=True)
    fmt = {"code": code, "port": port, "user": user,
           "base": p["base"], "terminal_dir": p["terminal"],
           "wine_prefix": p["wine_prefix"], "wine_bin": cfg.wine_bin,
           "broker_allow": cfg.broker_allow}
    if rpc_mode == "ro":
        import hashlib
        import hmac
        if len(cfg.rpc_secret) < 32 or not cfg.wine_python:
            raise HelperError(EXIT_FAIL, "konfigurasi ro_service tidak lengkap")
        token = hmac.new(cfg.rpc_secret.encode("utf-8"),
                         f"{code}:{port}:{provision_id}".encode("utf-8"),
                         hashlib.sha256).hexdigest()
        tok = p["terminal"] / "rpc.token"
        fd = os.open(tok, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        fmt["wine_python"] = cfg.wine_python
        (p["terminal"] / "run-terminal.sh").write_text(
            RUN_TEMPLATE_RO.format(**fmt), encoding="utf-8")
    else:
        (p["terminal"] / "run-terminal.sh").write_text(
            RUN_TEMPLATE.format(**fmt), encoding="utf-8")
    (p["terminal"] / "stop-terminal.sh").write_text(
        STOP_TEMPLATE.format(**fmt), encoding="utf-8")
    if chown:
        import pwd
        if cfg.service_user_mode == "per_customer":
            ensure_user(cfg, user, p["base"], runner)
        pw = pwd.getpwnam(user)
        for root, dirs, files in os.walk(p["base"]):
            for n in dirs + files:
                fp = os.path.join(root, n)
                if not os.path.islink(fp) and n != MARKER:
                    os.chown(fp, pw.pw_uid, pw.pw_gid)
                    if os.path.isdir(fp):
                        os.chmod(fp, 0o700)
        # base: root:<grup customer> 0750 — customer lain tanpa akses
        os.chown(p["base"], 0, pw.pw_gid)
        os.chmod(p["base"], 0o750)
    p["unit_file"].write_text(UNIT_TEMPLATE.format(**fmt), encoding="utf-8")
    os.chmod(p["unit_file"], 0o644)
    runner.run([cfg.systemctl, "daemon-reload"])
    return {"ok": True, "idempotent": False}


def op_start(cfg, code, port, runner) -> dict:
    p = paths(cfg, code)
    if read_marker(p) is None or not p["unit_file"].exists():
        raise HelperError(EXIT_OWNERSHIP, "terminal belum di-install")
    runner.run([cfg.systemctl, "enable", "--now", p["unit"] + ".service"])
    return {"ok": True}


def op_stop(cfg, code, runner) -> dict:
    p = paths(cfg, code)
    if p["unit_file"].exists():
        runner.run([cfg.systemctl, "disable", "--now",
                    p["unit"] + ".service"], check=False)
    return {"ok": True}


def op_remove(cfg, code, provision_id, runner) -> dict:
    p = paths(cfg, code)
    owner = read_marker(p)
    if not p["base"].exists() and not p["unit_file"].exists():
        return {"ok": True, "already_absent": True}
    if owner != provision_id:
        # Tahap 3E: jangan hapus resource yang kepemilikannya tak terbukti
        raise HelperError(EXIT_OWNERSHIP, "kepemilikan tidak terbukti")
    port = _unit_port(p)
    op_stop(cfg, code, runner)
    # Paritas PR #4 (S5): resource/port TIDAK dilepas selama terminal lama
    # mungkin masih hidup. Setelah stop, unit wajib tidak aktif DAN tidak
    # ada listener port milik cgroup unit ini; bila masih ada -> gagal,
    # tidak ada yang dihapus (slot tetap ditahan, admin menangani).
    if p["unit_file"].exists() and runner.run(
            [cfg.systemctl, "is-active", "--quiet", p["unit"] + ".service"],
            check=False) == 0:
        raise HelperError(EXIT_FAIL, "terminal masih aktif — tidak dihapus")
    if port is not None and socket_owned_by_unit(cfg, p["unit"], port):
        raise HelperError(EXIT_FAIL, "listener terminal masih hidup — "
                                     "tidak dihapus")
    if p["unit_file"].exists():
        p["unit_file"].unlink()
        runner.run([cfg.systemctl, "daemon-reload"], check=False)
    shutil.rmtree(p["base"])
    if cfg.service_user_mode == "per_customer":
        import pwd
        try:
            pwd.getpwnam(service_user_for(cfg, code))
            runner.run([cfg.userdel, service_user_for(cfg, code)],
                       check=False)
        except KeyError:
            pass
    return {"ok": True, "already_absent": False}


def _unit_port(p: dict) -> int | None:
    """Port terminal dari unit milik helper sendiri (bukan argumen)."""
    try:
        txt = p["unit_file"].read_text(encoding="utf-8")
        return int(txt.split("localhost:", 1)[1].split(")", 1)[0])
    except (OSError, IndexError, ValueError):
        return None


def _listening_inode(proc_root: Path, port: int) -> str | None:
    """Inode socket LISTEN 127.0.0.1:port (IPv4) dari /proc/net/tcp."""
    want = f"0100007F:{port:04X}"
    try:
        lines = (proc_root / "net" / "tcp").read_text().splitlines()[1:]
    except OSError:
        return None
    for ln in lines:
        f = ln.split()
        if len(f) > 9 and f[1] == want and f[3] == "0A":
            return f[9]
    return None


def socket_owned_by_unit(cfg: HelperConfig, unit: str, port: int) -> bool:
    """True bila listener 127.0.0.1:port dimiliki proses dalam cgroup unit
    customer ini — mencegah verifikasi berbicara dengan terminal milik
    customer LAIN yang kebetulan memakai port (Tahap 3D)."""
    inode = _listening_inode(cfg.proc_root, port)
    if inode is None:
        return False
    target = f"socket:[{inode}]"
    for pid_dir in cfg.proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cg = (pid_dir / "cgroup").read_text()
            if f"/{unit}.service" not in cg:
                continue
            for fd in (pid_dir / "fd").iterdir():
                if os.readlink(fd) == target:
                    return True
        except OSError:
            continue
    return False


def op_status(cfg, code, port, runner) -> dict:
    p = paths(cfg, code)
    installed = read_marker(p) is not None and p["unit_file"].exists()
    active = installed and runner.run(
        [cfg.systemctl, "is-active", "--quiet", p["unit"] + ".service"],
        check=False) == 0
    owned = active and socket_owned_by_unit(cfg, p["unit"], port)
    return {"ok": True, "installed": installed, "active": active,
            "listener_owned": owned, "running": bool(active and owned)}


def dispatch(cfg: HelperConfig, argv: list[str], runner=None) -> dict:
    ap = argparse.ArgumentParser(prog="transcendence-mt5-provision",
                                 add_help=False, allow_abbrev=False)
    ap.add_argument("op", choices=OPS)
    ap.add_argument("--code", required=True)
    ap.add_argument("--port", type=int)
    ap.add_argument("--provision-id")
    ap.add_argument("--rpc-mode", choices=("classic", "ro"),
                    default="classic")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        raise HelperError(EXIT_ARGS, "argumen tidak sah") from e
    validate(cfg, a.op, a.code, a.port, a.provision_id)
    runner = runner or Runner()
    if a.op == "install":
        return op_install(cfg, a.code, a.port, a.provision_id, runner,
                          rpc_mode=a.rpc_mode)
    if a.op == "start":
        return op_start(cfg, a.code, a.port, runner)
    if a.op == "stop":
        return op_stop(cfg, a.code, runner)
    if a.op == "remove":
        return op_remove(cfg, a.code, a.provision_id, runner)
    return op_status(cfg, a.code, a.port, runner)


def main(argv: list[str] | None = None) -> int:
    try:
        if os.geteuid() != 0:
            raise HelperError(EXIT_FAIL, "helper wajib dijalankan via sudo")
        cfg = HelperConfig.load()
        out = dispatch(cfg, list(sys.argv[1:] if argv is None else argv))
        print(json.dumps(out))
        return EXIT_OK
    except HelperError as e:
        print(json.dumps({"ok": False, "error": e.msg, "code": e.code}))
        return e.code
    except Exception:  # noqa: BLE001 — tanpa traceback (tanpa info sensitif)
        print(json.dumps({"ok": False, "error": "kegagalan internal",
                          "code": EXIT_FAIL}))
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
