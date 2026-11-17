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

Remediasi F2 (timeout / race):
- SERIALISASI: install/start/stop/remove memegang flock eksklusif per
  customer (<lock_dir>/<KODE>.lock). Lock dilepas kernel saat proses
  (termasuk anaknya, yang mewarisi fd lock) mati — aman setelah crash/
  SIGKILL. Lock tidak didapat dalam lock_wait_seconds -> exit 6 (sibuk).
- BATAS WAKTU SENDIRI: helper menghentikan dirinya pada op_timeout_seconds
  (dari konfigurasi root) dan pada SIGTERM/SIGINT/SIGHUP (sudo meneruskan
  SIGTERM dari API). Anak (systemctl/useradd) ikut dihentikan. Hasilnya
  exit 5: operasi TIDAK lengkap; tidak ada yang dinyatakan selesai.
- PENANDA vs SELESAI: .provision_id hanya KLAIM kepemilikan (agar resource
  setengah jadi tetap bisa dibersihkan pemiliknya). Bukti instalasi selesai
  adalah .install_complete, ditulis PALING AKHIR setelah salinan template
  terverifikasi, skrip & unit ditulis atomik, dan daemon-reload sukses.
  start/status hanya menerima instalasi dengan .install_complete.
- IDEMPOTEN: install ulang oleh pemilik yang sama atas instalasi tidak
  lengkap membuang salinan lama (tidak dipercaya) lalu menyalin ulang ke
  terminal.partial -> verifikasi -> rename. Tidak ada direktori/unit ganda
  (path & nama unit diturunkan dari kode customer).
- inspect: laporan read-only (busy, phase) untuk pemulihan setelah timeout.
- TOMBSTONE (T2): remove TIDAK pernah rmtree direktori customer di tempat.
  Urutan: validasi penanda -> stop -> tulis manifest identitas
  (<base_dir>/.tombstones/<KODE>.<provision_id>.json, root 0700) -> rename
  ATOMIK direktori customer -> .tombstones/<KODE>.<provision_id> -> hapus
  unit -> rmtree tombstone -> userdel -> hapus manifest PALING AKHIR.
  Terputus di titik mana pun (SIGTERM/SIGKILL/crash): manifest + nama
  tombstone tetap menjadi bukti kepemilikan; remove berikutnya dengan
  provision_id sama MELANJUTKAN. Tombstone tanpa manifest yang cocok,
  atau penanda di dalamnya berbeda, TIDAK disentuh. install ditolak selama
  ada tombstone untuk kode itu.

Keluaran: satu baris JSON di stdout; exit 0 sukses, 2 argumen tidak sah,
3 kepemilikan tidak terbukti / konflik, 4 kegagalan operasi,
5 dihentikan (batas waktu/sinyal) — instalasi TIDAK lengkap,
6 sibuk (operasi lain atas customer yang sama sedang berjalan),
7 TIDAK BERSIH — proses turunan operasi ini mungkin MASIH HIDUP (tidak
  dapat dimatikan/dituai dalam batas waktu). Tidak pernah sukses; identitas
  proses dicatat di <lock_dir>/<KODE>.unclean sehingga customer itu tetap
  "sibuk" (fail-closed) selama proses tersebut masih ada.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = "/etc/transcendence/provisioner.json"
UNIT_PREFIX = "transcendence-mt5-"
MARKER = ".provision_id"          # klaim kepemilikan (BUKAN bukti selesai)
COMPLETE = ".install_complete"    # bukti instalasi selesai & terverifikasi
PARTIAL = "terminal.partial"      # target salin sementara
TOMB_DIR = ".tombstones"          # di bawah base_dir (satu filesystem), root 0700

CODE_RE = re.compile(r"^C[0-9]{3,9}$")
PID_RE = re.compile(r"^[0-9a-f]{32}$")
OPS = ("install", "start", "stop", "remove", "status", "inspect",
       "fence-advance", "fence-release", "selfcheck", "audit-all")
LOCKED_OPS = ("install", "start", "stop", "remove", "fence-advance",
              "fence-release")
# Operasi mengubah keadaan terminal yang tunduk pada fence migrasi.
FENCED_OPS = ("install", "start", "stop", "remove")
# Operasi tanpa kode customer (global, read-only).
GLOBAL_OPS = ("selfcheck", "audit-all")
# Operasi read-only yang tetap diizinkan walau konfigurasi produksi
# tidak lolos (agar operator dapat mendiagnosis).
DIAG_OPS = ("status", "inspect", "selfcheck", "audit-all")

EXIT_OK, EXIT_ARGS, EXIT_OWNERSHIP, EXIT_FAIL = 0, 2, 3, 4
EXIT_INTERRUPTED, EXIT_BUSY, EXIT_UNCLEAN = 5, 6, 7
# MT5 RPC Hardening: fence migrasi ditolak (worker generasi lama / migrasi
# lain memegang terminal) dan kebijakan produksi dilanggar. Keduanya
# dikembalikan SEBELUM perubahan apa pun.
EXIT_FENCE, EXIT_POLICY = 8, 9

# ---- PARITAS backend/security/rpc_policy.py (diuji
# tests/test_rpc_policy_parity.py) — helper stdlib-only, tidak mengimpor
# modul backend.
HELPER_PRODUCTION_RPC_MODES = ("ro",)
HELPER_KNOWN_RPC_MODES = ("classic", "ro")
SELFCHECK_DOMAIN = "transcendence-selfcheck-v2|"
SECRET_MIN_BYTES = 32
SECRET_PLACEHOLDERS = frozenset({
    "", "changeme", "change-me", "secret", "<openssl rand -hex 32>",
    "<random>", "xxx", "todo",
})
FENCE_DIR = ".fence"              # di bawah base_dir (persisten), root 0700
FENCE_VERSION = 1
MID_RE = re.compile(r"^[0-9a-f]{32}$")
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")

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
    # F2: lock per customer + batas waktu helper SENDIRI. Wajib:
    # op_timeout_seconds + lock_wait_seconds < PROVISIONER_TIMEOUT_SECONDS
    # (API), agar helper berhenti sendiri sebelum API menyerah.
    lock_dir: Path = Path("/run/transcendence-mt5-provision")
    op_timeout_seconds: float = 45.0
    lock_wait_seconds: float = 5.0
    # B8/S5: mode "shared" (satu UID untuk semua terminal) TIDAK mengisolasi
    # customer — proses terminal customer lain ber-UID sama. Proses helper
    # (main) MENOLAK install dengan mode shared kecuali kunci ini true
    # (hanya untuk lingkungan uji; JANGAN di staging/produksi).
    allow_shared_service_user: bool = False
    # MT5 RPC Hardening: true = mode produksi. Operasi yang mengubah
    # keadaan DITOLAK (exit 9) kecuali per_customer + ro_service + secret
    # kuat + wine_python + broker_allow CIDR; classic tidak pernah boleh
    # di-install/di-start; allow_shared_service_user diabaikan.
    production: bool = False

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
            rpc_secret=str(raw.get("rpc_secret", "")),
            lock_dir=Path(raw.get("lock_dir",
                                  "/run/transcendence-mt5-provision")),
            op_timeout_seconds=float(raw.get("op_timeout_seconds", 45.0)),
            lock_wait_seconds=float(raw.get("lock_wait_seconds", 5.0)),
            allow_shared_service_user=raw.get(
                "allow_shared_service_user", False) is True,
            production=raw.get("production", False) is True)


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
    for p in (cfg.base_dir, cfg.template_dir, cfg.unit_dir, cfg.lock_dir):
        if not p.is_absolute():
            raise HelperError(EXIT_FAIL, "path konfigurasi wajib absolut")
    if not (1.0 <= float(cfg.op_timeout_seconds) <= 3600.0 and
            0.0 <= float(cfg.lock_wait_seconds) <= 60.0):
        raise HelperError(EXIT_FAIL, "batas waktu konfigurasi tidak sah")
    if cfg.service_user_mode not in ("shared", "per_customer"):
        raise HelperError(EXIT_FAIL, "service_user_mode tidak sah")
    for exe in (cfg.systemctl, cfg.wine_bin, cfg.useradd, cfg.userdel):
        if not os.path.isabs(exe):
            raise HelperError(EXIT_FAIL, "binary konfigurasi wajib absolut")
    validate_global(cfg, op)


def validate_global(cfg: HelperConfig, op: str) -> None:
    """Validasi konfigurasi yang tidak bergantung pada kode customer.

    - broker_allow SELALU divalidasi (disisipkan ke unit systemd):
      karakter kontrol/baris baru/hostname -> ditolak (exit 4).
    - production=true: operasi yang mengubah keadaan ditolak (exit 9)
      bila production_problems() tidak kosong. Operasi diagnostik
      (status/inspect/selfcheck/audit-all) tetap diizinkan."""
    if op not in OPS:
        raise HelperError(EXIT_ARGS, "operasi tidak sah")
    try:
        parse_broker_allow(cfg.broker_allow, production=False)
    except ValueError as e:
        raise HelperError(EXIT_FAIL, f"broker_allow tidak sah: {e}") from None
    if cfg.production and op not in DIAG_OPS:
        probs = production_problems(cfg)
        if probs:
            raise HelperError(EXIT_POLICY, "konfigurasi produksi ditolak: "
                                           + ", ".join(probs))


def paths(cfg: HelperConfig, code: str) -> dict:
    base = cfg.base_dir / code
    # tahan symlink/traversal: hasil resolve WAJIB anak langsung base_dir
    if base.exists() and (base.is_symlink() or
                          base.resolve().parent != cfg.base_dir.resolve()):
        raise HelperError(EXIT_OWNERSHIP, "direktori customer tidak sah")
    unit = f"{UNIT_PREFIX}{code.lower()}"
    return {"base": base, "terminal": base / "terminal",
            "partial": base / PARTIAL,
            "wine_prefix": base / "wine-prefix", "data": base / "data",
            "marker": base / MARKER, "complete": base / COMPLETE,
            "unit": unit,
            "unit_file": cfg.unit_dir / f"{unit}.service",
            "unit_tmp": cfg.unit_dir / f".{unit}.service.tmp"}


# ------------------------------------------------------------- F2: lock
def _lock_dir(cfg: HelperConfig) -> Path:
    d = cfg.lock_dir
    try:
        d.mkdir(mode=0o700, parents=False)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & 0o077):
        raise HelperError(EXIT_FAIL, "direktori lock tidak aman")
    return d


def _open_lock(cfg: HelperConfig, code: str) -> int:
    path = _lock_dir(cfg) / f"{code}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        os.close(fd)
        raise HelperError(EXIT_FAIL, "berkas lock tidak aman")
    return fd


def acquire_lock(cfg: HelperConfig, code: str,
                 wait: float | None = None) -> int:
    """flock eksklusif per customer. Dilepas otomatis oleh kernel saat
    SEMUA pemegang fd (helper + anak yang mewarisinya) mati."""
    fd = _open_lock(cfg, code)
    end = time.monotonic() + (cfg.lock_wait_seconds if wait is None
                              else wait)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if unclean_alive(cfg, code):
                # M2/M3: turunan operasi sebelumnya masih hidup walau lock
                # bebas (mis. menutup fd warisan) -> tetap SIBUK
                os.close(fd)
                raise HelperError(EXIT_BUSY, "proses operasi sebelumnya "
                                             "atas customer ini masih hidup")
            return fd
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                os.close(fd)
                raise
        if time.monotonic() >= end:
            os.close(fd)
            raise HelperError(EXIT_BUSY, "operasi lain atas customer ini "
                                         "sedang berjalan")
        time.sleep(0.1)


PROC_ALIVE, PROC_DEAD, PROC_ZOMBIE, PROC_UNKNOWN = (
    "alive", "dead", "zombie", "unknown")


def _read_proc_stat(pid: int) -> str:
    with open(f"/proc/{int(pid)}/stat") as fh:
        return fh.read()


def _proc_probe(pid: int) -> tuple:
    """(status, starttime|None) proses [pid] dari /proc/<pid>/stat.

    DEAD    : /proc/<pid> benar-benar tidak ada (ENOENT/ESRCH) atau state
              'X' (mati).
    ZOMBIE  : state 'Z' — tidak dapat lagi menjalankan apa pun.
    UNKNOWN : TIDAK dapat dipastikan — izin ditolak, galat I/O, format
              /proc tidak sah. BUKAN bukti proses mati.
    ALIVE   : terbaca & bukan zombie (starttime dikembalikan)."""
    try:
        raw = _read_proc_stat(pid)
    except (FileNotFoundError, ProcessLookupError):
        return PROC_DEAD, None
    except (OSError, ValueError):
        return PROC_UNKNOWN, None
    try:
        f = raw.rsplit(")", 1)[1].split()
        state, start = f[0], f[19]
        if not start.isdigit() or len(state) != 1:
            return PROC_UNKNOWN, None
    except (IndexError, AttributeError):
        return PROC_UNKNOWN, None
    if state == "Z":
        return PROC_ZOMBIE, start
    if state in ("X", "x"):
        return PROC_DEAD, start
    return PROC_ALIVE, start


def proc_state(pid: int, start: str) -> str:
    """Status proses DENGAN identitas (pid, starttime). PID yang sudah
    dipakai ulang (starttime berbeda) = proses asli DEAD."""
    st, cur = _proc_probe(pid)
    if st == PROC_ALIVE and cur != str(start):
        return PROC_DEAD                 # PID didaur ulang: bukan proses itu
    return st


def _proc_start(pid: int) -> str | None:
    """starttime proses HIDUP (bukan zombie); None bila mati, zombie, ATAU
    tidak dapat dipastikan. Hanya untuk MENCATAT identitas — keputusan
    hidup/mati WAJIB memakai proc_state() (None di sini bukan bukti mati)."""
    st, cur = _proc_probe(pid)
    return cur if st == PROC_ALIVE else None


def _unclean_path(cfg: HelperConfig, code: str) -> Path:
    return _lock_dir(cfg) / f"{code}.unclean"


UNKNOWN_IDENT = (0, "?")      # identitas proses tersisa TIDAK diketahui


def _ident_unknown(pid, start) -> bool:
    """Entri tanpa identitas pasti (pid tidak sah / starttime '?')."""
    try:
        return int(pid) <= 0 or str(start) == "?"
    except (TypeError, ValueError):
        return True


def record_unclean(cfg: HelperConfig, code: str, survivors: list) -> None:
    """Catat identitas (pid, starttime) proses operasi yang MASIH HIDUP.
    Digabung dengan catatan lama yang prosesnya masih ada / tidak pasti.
    Catatan TIDAK PERNAH kosong: tanpa identitas -> UNKNOWN_IDENT (tetap
    sibuk sampai dibersihkan admin / reboot)."""
    path = _unclean_path(cfg, code)
    keep = {(int(p), str(st)) for p, st in survivors}
    try:
        for p, st in json.loads(path.read_text(encoding="utf-8")):
            if _ident_unknown(p, st) or proc_state(int(p), st) in (
                    PROC_ALIVE, PROC_UNKNOWN):
                keep.add((int(p), str(st)))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        keep.add(UNKNOWN_IDENT)           # catatan lama rusak: tetap tak pasti
    if not keep:
        keep.add(UNKNOWN_IDENT)
    _atomic_write(path, json.dumps(sorted(keep)), 0o600)


def _survivors_pending(survivors: list) -> bool:
    """True bila ada yang masih hidup, identitasnya tidak diketahui, ATAU
    statusnya tidak dapat dipastikan (UNKNOWN). Hanya DEAD/ZOMBIE yang
    dianggap selesai."""
    return any(_ident_unknown(p, st) or proc_state(int(p), st) in (
        PROC_ALIVE, PROC_UNKNOWN) for p, st in survivors)


def _guard(survivors: list) -> None:
    """Pengaman TERAKHIR bila catatan .unclean tidak dapat ditulis: helper
    TIDAK keluar (lock flock tetap dipegang proses ini) selama proses
    tersisa masih hidup / identitasnya tidak diketahui. API melihat helper
    masih berjalan (helper_running) -> fail-closed; pulih sendiri saat
    proses itu mati, atau lewat admin (kill helper) / reboot."""
    signal.pthread_sigmask(signal.SIG_BLOCK, set(_OP_SIGNALS))
    signal.setitimer(signal.ITIMER_REAL, 0)
    while _survivors_pending(survivors or [UNKNOWN_IDENT]):
        time.sleep(0.5)


def persist_unclean(cfg: HelperConfig, code: str, survivors: list,
                    lock_held: bool) -> None:
    """Catat .unclean dan VERIFIKASI terbaca sebagai sibuk. Gagal mencatat
    TIDAK PERNAH dianggap berhasil: pegang lock (ambil dulu bila belum
    dipegang) lalu _guard()."""
    survivors = list(survivors) or [UNKNOWN_IDENT]
    try:
        record_unclean(cfg, code, survivors)
        if unclean_alive(cfg, code):
            return
    except Exception:  # noqa: BLE001
        pass
    if not lock_held:
        try:
            fd = _open_lock(cfg, code)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    time.sleep(0.1)       # pemegang lain = tetap sibuk
        except Exception:  # noqa: BLE001
            pass                          # tanpa lock: tetap menahan proses
    _guard(survivors)


def unclean_alive(cfg: HelperConfig, code: str) -> bool:
    """True bila ada proses tercatat yang masih hidup DENGAN starttime sama
    (PID yang didaur ulang tidak dihitung). Catatan dihapus bila semua mati.
    Catatan rusak/tidak terbaca -> True (fail-closed)."""
    path = _unclean_path(cfg, code)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        return True
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
        # kosong / identitas tidak diketahui = TIDAK PASTI -> sibuk dan
        # catatan TIDAK dihapus otomatis
        if not items or _survivors_pending(items):
            return True
    except (OSError, ValueError, TypeError):
        return True
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    return False


def lock_busy(cfg: HelperConfig, code: str) -> bool:
    """True bila operasi lain (proses hidup) memegang lock customer."""
    try:
        fd = acquire_lock(cfg, code, wait=0.0)
    except HelperError as e:
        if e.code == EXIT_BUSY:
            return True
        raise
    os.close(fd)
    return False


# ------------------------------------------------------------- F2: berkas
def _atomic_write(path: Path, data: str, mode: int) -> None:
    """Tulis tmp di direktori yang sama -> fsync -> rename -> fsync dir.
    Pembaca tidak pernah melihat isi setengah tertulis."""
    tmp = path.parent / f".{path.name}.tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


# ------------------------------------------------------------- B8/S5: kepemilikan
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _same_inode(a, b) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def chown_tree_fd(dfd: int, uid: int, gid: int) -> int:
    """Ubah kepemilikan isi direktori [dfd] TANPA pernah mengikuti symlink.

    - Semua operasi relatif terhadap fd direktori yang sudah dibuka (bukan
      path) -> pertukaran komponen path di tengah jalan tidak berpengaruh.
    - Tiap entri: lstat -> buka dengan O_NOFOLLOW -> fstat HARUS inode &
      tipe yang sama -> fchown/fchmod pada fd itu (bukan chown(path)).
    - Urutan DAUN -> AKAR: direktori baru dimiliki user setelah seluruh
      isinya selesai diproses.
    - Symlink, hardlink (nlink > 1), dan berkas khusus (fifo/device/socket)
      DITOLAK (EXIT_OWNERSHIP) — tidak diubah, tidak diikuti.
    Return: jumlah entri yang diubah."""
    n = 0
    for name in os.listdir(dfd):
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            try:
                cfd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
            except OSError as e:
                raise HelperError(EXIT_OWNERSHIP, "entri berubah saat "
                                  "kepemilikan diproses") from e
            try:
                if not _same_inode(os.fstat(cfd), st):
                    raise HelperError(EXIT_OWNERSHIP, "entri berubah saat "
                                      "kepemilikan diproses")
                n += chown_tree_fd(cfd, uid, gid)
                os.fchown(cfd, uid, gid)
                os.fchmod(cfd, 0o700)
                n += 1
            finally:
                os.close(cfd)
        elif stat.S_ISREG(st.st_mode):
            try:
                fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
            except OSError as e:
                raise HelperError(EXIT_OWNERSHIP, "entri berubah saat "
                                  "kepemilikan diproses") from e
            try:
                st2 = os.fstat(fd)
                if not stat.S_ISREG(st2.st_mode) or \
                        not _same_inode(st2, st) or st2.st_nlink != 1:
                    raise HelperError(EXIT_OWNERSHIP, "berkas tidak sah "
                                      "(berubah / hardlink) — tidak diubah")
                os.fchown(fd, uid, gid)
                n += 1
            finally:
                os.close(fd)
        else:
            raise HelperError(EXIT_OWNERSHIP, "entri bukan berkas/direktori "
                              "biasa (symlink/khusus) — tidak diubah")
    return n


def _rmtree_at(dfd: int, name: str) -> None:
    """Hapus subpohon [name] di dalam direktori [dfd] (tanpa mengikuti
    symlink; entri berupa symlink hanya di-unlink)."""
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=dfd)
    else:
        os.unlink(name, dir_fd=dfd)


def verify_copy(src: Path, dst: Path) -> int:
    """Setiap entri template ada di salinan dengan tipe & ukuran sama."""
    n = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        droot = dst if rel == "." else dst / rel
        if not droot.is_dir() or droot.is_symlink():
            raise HelperError(EXIT_FAIL, "salinan template tidak lengkap")
        for f in files:
            s_st = os.stat(os.path.join(root, f))
            d = droot / f
            if d.is_symlink() or not d.is_file() or \
                    d.stat().st_size != s_st.st_size:
                raise HelperError(EXIT_FAIL, "salinan template tidak lengkap")
            n += 1
    return n


def read_complete(p: dict, provision_id: str | None) -> dict | None:
    """Bukti selesai yang SAH: berkas biasa, JSON, provision_id = penanda."""
    c = p["complete"]
    if provision_id is None or not c.is_file() or c.is_symlink():
        return None
    try:
        data = json.loads(c.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("provision_id") != provision_id:
        return None
    return data


# ------------------------------------------------------------- F2/T2: tombstone
def _tomb_root(cfg: HelperConfig) -> Path:
    d = cfg.base_dir / TOMB_DIR
    try:
        d.mkdir(mode=0o700, parents=False)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & 0o077):
        raise HelperError(EXIT_FAIL, "direktori tombstone tidak aman")
    return d


def tomb_paths(cfg: HelperConfig, code: str, provision_id: str) -> tuple:
    """(direktori tombstone, manifest) — nama membawa kode + provision_id."""
    if not CODE_RE.fullmatch(code) or not PID_RE.fullmatch(provision_id):
        raise HelperError(EXIT_ARGS, "identitas tombstone tidak sah")
    root = _tomb_root(cfg)
    name = f"{code}.{provision_id}"
    return root / name, root / f"{name}.json"


def tombs_for(cfg: HelperConfig, code: str) -> list[str]:
    """Nama entri tombstone/manifest untuk kode ini (penghapusan belum
    selesai). Tidak membuat apa pun bila direktori tombstone belum ada."""
    d = cfg.base_dir / TOMB_DIR
    if not d.is_dir() or d.is_symlink():
        return []
    return sorted(n for n in os.listdir(d) if n.startswith(f"{code}."))


def read_manifest(path: Path, code: str, provision_id: str) -> bool:
    """Manifest SAH: berkas biasa (bukan symlink) milik root, JSON dengan
    kode & provision_id persis sama."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict) and data.get("code") == code and
            data.get("provision_id") == provision_id)


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install_complete(p: dict, provision_id: str | None) -> dict | None:
    """B8.1: instalasi SELESAI hanya bila SEMUA syarat terpenuhi:
    - penanda .provision_id = provision_id;
    - .install_complete sah untuk provision_id itu DAN mencatat keadaan
      direktori customer yang diharapkan (base_uid/base_gid/base_mode);
    - unit ada;
    - direktori customer (lstat, bukan symlink) persis berpemilik, bergrup,
      dan bermode seperti yang dicatat (mis. root:<grup> 0750 setelah
      dibuka). Masih terkunci / diubah -> BUKAN selesai.
    Marker format lama (tanpa base_*) -> BUKAN selesai (fail-closed)."""
    if provision_id is None or read_marker(p) != provision_id:
        return None
    data = read_complete(p, provision_id)
    if data is None or not p["unit_file"].exists():
        return None
    try:
        want = (int(data["base_uid"]), int(data["base_gid"]),
                int(data["base_mode"]))
    except (KeyError, TypeError, ValueError):
        return None
    try:
        st = os.lstat(p["base"])
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    if (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != want:
        return None
    return data


def phase_of(p: dict, tombs: list[str] | None = None) -> str:
    owner = read_marker(p)
    if tombs:
        return "removing"           # penghapusan belum selesai (tombstone)
    if not p["base"].exists():
        return "orphan_unit" if p["unit_file"].exists() else "absent"
    if owner is None:
        return "unclaimed"
    if install_complete(p, owner) is not None:
        return "complete"
    return "incomplete"


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
    v = m.read_text(encoding="utf-8").strip()
    return v if PID_RE.fullmatch(v) else None


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
_OP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGALRM)
# Proses helper KHUSUS (diaktifkan main()). Hanya di sana sapuan turunan
# (kill_descendants) berjalan: bila modul dipakai in-process (uji/pustaka),
# anak proses pemanggil yang tidak terkait TIDAK boleh ikut dimatikan.
# Pemutusan process group per perintah SELALU berlaku (tidak bergantung
# pada subreaper).
_HELPER_PROCESS = False
# PR_SET_CHILD_SUBREAPER berhasil: turunan yang keluar dari process group
# (setsid/daemonize) tetap dapat ditemukan. Bila GAGAL di proses helper,
# operasi yang mengubah state DITOLAK sebelum menyentuh apa pun (M1).
_SUBREAPER_OK = False
KILL_WAIT_SECONDS = 5.0       # batas menuai pemimpin setelah SIGKILL (M3)
SWEEP_SECONDS = 3.0           # batas sapuan turunan (M2)


def _set_subreaper() -> bool:
    """PR_SET_CHILD_SUBREAPER: turunan yatim (termasuk yang setsid/daemonize)
    di-reparent ke helper ini, bukan ke PID 1 -> tetap dapat ditemukan dan
    dihentikan."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(36, 1, 0, 0, 0) == 0
    except Exception:  # noqa: BLE001
        return False


def _proc_parent(pid: int) -> tuple:
    """(status, ppid|None, starttime|None) dari /proc/<pid>/stat.
    status: PROC_ALIVE (terbaca, bukan zombie) | PROC_ZOMBIE | PROC_DEAD
    (benar-benar tidak ada) | PROC_UNKNOWN (tidak terbaca / format tidak
    sah — BUKAN bukti 'bukan turunan')."""
    try:
        raw = _read_proc_stat(pid)
    except (FileNotFoundError, ProcessLookupError):
        return PROC_DEAD, None, None
    except (OSError, ValueError):
        return PROC_UNKNOWN, None, None
    try:
        f = raw.rsplit(")", 1)[1].split()
        state, ppid, start = f[0], int(f[1]), f[19]
        if not start.isdigit() or len(state) != 1:
            return PROC_UNKNOWN, None, None
    except (IndexError, ValueError, AttributeError):
        return PROC_UNKNOWN, None, None
    if state == "Z":
        return PROC_ZOMBIE, ppid, start
    if state in ("X", "x"):
        return PROC_DEAD, ppid, start
    return PROC_ALIVE, ppid, start


def _is_my_child(pidfd: int) -> str:
    """Verifikasi TANPA /proc: kernel hanya menerima waitid() atas pidfd
    untuk ANAK proses ini (termasuk yatim yang diadopsi subreaper).
    'child' = anak & masih berjalan | 'exited' = anak, sudah keluar (zombie)
    | 'no' = bukan anak (ECHILD) | 'unknown' = tidak dapat dipastikan."""
    try:
        si = os.waitid(os.P_PIDFD, pidfd,
                       os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return "no"
    except OSError:
        return "unknown"
    return "child" if si is None else "exited"


def _kill_pidfd(fd: int) -> bool:
    """SIGKILL lewat pidfd. False bila sinyal TIDAK terkirim padahal proses
    mungkin masih ada."""
    try:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return True                      # sudah tidak ada
    except OSError:
        return False


def kill_descendants(max_seconds: float | None = None) -> list:
    """Matikan SEMUA proses yang induknya helper ini (anak langsung + yatim
    yang diadopsi lewat subreaper). Sinyal SELALU lewat pidfd (PID yang
    didaur ulang tidak pernah terkena). Unit systemd customer mana pun
    bukan anak helper (di-fork PID 1) -> tidak pernah tersentuh.

    Per PID di /proc (B7 — tidak ada galat yang diabaikan diam-diam):
    - stat terbaca, PPid != helper        -> BUKAN turunan: dilewati.
    - tidak ada / zombie                  -> dilewati (zombie dituai).
    - stat terbaca, PPid == helper        -> pidfd dibuka, identitas dibaca
      ULANG: sama -> SIGKILL; berbeda (PID dipakai ulang) -> dilewati;
      tidak terbaca -> diputuskan lewat waitid(pidfd).
    - stat TIDAK terbaca / format rusak   -> BUKAN bukti 'bukan turunan':
      diputuskan lewat waitid(pidfd): anak -> SIGKILL (identitas dicatat
      (pid, '?')); bukan anak -> dilewati (TIDAK dimatikan); tidak dapat
      dipastikan -> UNKNOWN_IDENT.
    - pidfd tidak dapat dibuka / sinyal gagal terkirim -> tetap dicatat.

    Return: daftar (pid, starttime|'?') / UNKNOWN_IDENT yang MASIH HIDUP atau
    TIDAK TERVERIFIKASI setelah batas waktu ([] = bersih). Pemanggil WAJIB
    memperlakukan daftar tak kosong sebagai operasi tidak bersih."""
    me = os.getpid()
    end = time.monotonic() + (SWEEP_SECONDS if max_seconds is None
                              else max_seconds)
    while True:
        left = []
        for name in os.listdir("/proc"):
            if not name.isdigit() or int(name) == me:
                continue
            pid = int(name)
            st, ppid, start = _proc_parent(pid)
            if st in (PROC_DEAD, PROC_ZOMBIE):
                continue
            if st == PROC_ALIVE and ppid != me:
                continue                                 # bukan turunan
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue                                 # sudah tidak ada
            except OSError:
                # tidak dapat membuka pidfd: turunan yang diketahui tetap
                # dicatat; yang tak terverifikasi -> tidak diketahui
                left.append((pid, start) if st == PROC_ALIVE
                            else UNKNOWN_IDENT)
                continue
            try:
                if st == PROC_ALIVE:
                    st2, ppid2, start2 = _proc_parent(pid)
                    if st2 == PROC_ALIVE:
                        if (ppid2, start2) != (ppid, start):
                            continue                     # PID dipakai ulang
                        verdict = "child"
                    elif st2 in (PROC_DEAD, PROC_ZOMBIE):
                        continue
                    else:
                        verdict = _is_my_child(fd)       # kini tak terbaca
                else:
                    verdict = _is_my_child(fd)           # stat tak terbaca
                    start = "?"
                if verdict in ("no", "exited"):
                    continue
                if verdict == "unknown":
                    left.append(UNKNOWN_IDENT)           # TIDAK dimatikan
                    continue
                _kill_pidfd(fd)
                left.append((pid, start))                # hidup sampai terbukti mati
            finally:
                os.close(fd)
        try:                                             # tuai yang sudah mati
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        if not left or time.monotonic() >= end:
            return left
        time.sleep(0.05)


class Runner:
    """Eksekusi perintah TANPA shell, argv list, env minimal.

    PENGHENTIAN TURUNAN: setiap perintah berjalan di sesi & process group
    SENDIRI. Selesai normal, batas waktu, atau terpotong sinyal -> SELURUH
    grup di-SIGKILL SEBELUM pemimpinnya dituai, lalu (di proses helper)
    turunan yang keluar dari grup dimatikan lewat kill_descendants().

    JAMINAN IDENTITAS (H1): killpg HANYA dipanggil selama pemimpin terbukti
    belum dituai (masih berjalan atau zombie milik kita) — saat itu kernel
    tidak dapat memakai ulang PID/PGID-nya. Itu mensyaratkan SIGCHLD TIDAK
    diabaikan (kernel menuai otomatis bila SIG_IGN): run() MENOLAK berjalan
    bila SIGCHLD = SIG_IGN, dan bila waitid() tetap melaporkan anak hilang
    (ChildProcessError) killpg TIDAK dipanggil sama sekali.

    TIDAK BERSIH (M2/M3): pemimpin tidak dapat dituai dalam
    KILL_WAIT_SECONDS, atau masih ada turunan setelah sapuan -> identitasnya
    dilaporkan ke on_unclean (dicatat di direktori lock) dan run() melempar
    EXIT_UNCLEAN — tidak pernah sukses."""

    # F2: fd lock diwariskan ke anak -> lock tetap terpegang selama anak
    # hidup (termasuk bila helper di-SIGKILL); pemeriksaan "busy" jujur.
    pass_fds: tuple = ()
    timeout: float = 60.0
    on_unclean = None             # callable(list[(pid, starttime)])

    def _unclean(self, survivors: list, why: str):
        if self.on_unclean is not None:
            try:
                self.on_unclean(survivors)
            except Exception:  # noqa: BLE001
                # pencatatan gagal TIDAK ditelan: tahan proses (lock operasi
                # masih dipegang pemanggil) sampai proses tersisa berhenti
                _guard(survivors)
        raise HelperError(EXIT_UNCLEAN, why)

    def run(self, argv: list[str], check: bool = True) -> int:
        if not argv or not os.path.isabs(argv[0]):
            raise HelperError(EXIT_FAIL, "perintah wajib path absolut")
        if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            # H1: dengan SIG_IGN kernel menuai anak otomatis -> identitas
            # PID/PGID tidak dapat dijamin. Tolak SEBELUM membuat anak.
            raise HelperError(EXIT_FAIL, "SIGCHLD diabaikan — penghentian "
                                         "turunan tidak dapat dijamin")
        p = subprocess.Popen(argv, shell=False, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
                             pass_fds=tuple(self.pass_fds),
                             start_new_session=True)
        timed_out = False
        identity_lost = False
        survivors: list = []
        try:
            end = time.monotonic() + self.timeout
            while True:
                # tunggu TANPA menuai (WNOWAIT): PID/PGID tetap terkunci
                try:
                    si = os.waitid(os.P_PID, p.pid,
                                   os.WEXITED | os.WNOWAIT | os.WNOHANG)
                except ChildProcessError:
                    identity_lost = True     # dituai pihak lain: PID bebas
                    break
                if si is not None:
                    break
                if time.monotonic() >= end:
                    timed_out = True
                    break
                time.sleep(0.02)
        finally:
            # tidak boleh terpotong sinyal kedua: blokir selama pembersihan
            # (dibatasi: KILL_WAIT_SECONDS + SWEEP_SECONDS)
            old = signal.pthread_sigmask(signal.SIG_BLOCK, set(_OP_SIGNALS))
            try:
                if not identity_lost:
                    lead_start = _proc_start(p.pid)
                    try:
                        os.killpg(p.pid, signal.SIGKILL)     # seluruh grup
                    except (ProcessLookupError, PermissionError):
                        pass
                    try:
                        p.wait(timeout=KILL_WAIT_SECONDS)    # tuai pemimpin
                    except subprocess.TimeoutExpired:
                        # M3: pemimpin tidak dapat dituai (mis. macet di
                        # kernel) — masih mungkin aktif
                        survivors.append((p.pid, lead_start or "?"))
                    except ChildProcessError:
                        identity_lost = True
                if _HELPER_PROCESS:
                    try:
                        survivors.extend(kill_descendants())
                    except Exception:  # noqa: BLE001
                        survivors.append(UNKNOWN_IDENT)   # sapuan gagal
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old)
        if survivors:
            self._unclean(survivors, "proses turunan masih hidup setelah "
                                     "batas pembersihan — operasi TIDAK "
                                     "bersih")
        if identity_lost:
            raise HelperError(EXIT_FAIL, "status anak tidak dapat dipastikan "
                                         "(dituai pihak lain) — tidak "
                                         "dianggap sukses")
        if timed_out:
            raise HelperError(EXIT_FAIL, "perintah melewati batas waktu")
        if check and p.returncode != 0:
            raise HelperError(EXIT_FAIL, f"perintah gagal ({p.returncode})")
        return p.returncode


def op_install(cfg, code, port, provision_id, runner, chown=True,
               rpc_mode: str = "classic") -> dict:
    if rpc_mode not in ("classic", "ro"):
        raise HelperError(EXIT_ARGS, "rpc-mode tidak sah")
    if cfg.production and rpc_mode not in HELPER_PRODUCTION_RPC_MODES:
        # Produksi: classic (RPyC tanpa autentikasi) TIDAK PERNAH di-install
        raise HelperError(EXIT_POLICY, "mode classic dilarang di produksi")
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
    if tombs_for(cfg, code):
        raise HelperError(EXIT_OWNERSHIP, "penghapusan sebelumnya untuk "
                                          "customer ini belum selesai")
    other = port_owner(cfg, port)
    if other not in (None, code):
        raise HelperError(EXIT_OWNERSHIP, "port dipakai customer lain")
    if owner == provision_id and \
            install_complete(p, provision_id) is not None:
        return {"ok": True, "idempotent": True}
    if chown and cfg.service_user_mode != "per_customer" and \
            (cfg.production or (_HELPER_PROCESS and
                                not cfg.allow_shared_service_user)):
        # B8/S5: tanpa UID per customer, proses terminal customer lain
        # ber-UID sama -> tidak ada isolasi. Ditolak SEBELUM perubahan.
        raise HelperError(EXIT_FAIL, "service_user_mode 'shared' ditolak — "
                                     "wajib 'per_customer'")
    p["base"].mkdir(mode=0o700, parents=False, exist_ok=True)
    base_fd = os.open(p["base"], _DIR_FLAGS)
    try:
        return _install_locked_base(cfg, code, port, provision_id, runner,
                                    chown, rpc_mode, p, user, owner, base_fd)
    finally:
        os.close(base_fd)


def _install_locked_base(cfg, code, port, provision_id, runner, chown,
                         rpc_mode, p, user, owner, base_fd) -> dict:
    """Isi instalasi dengan direktori customer TERKUNCI (B8/S5).

    Selama provisioning direktori customer root:root 0700 -> user layanan
    (dan proses customer lain) TIDAK dapat masuk; pencarian lewat cwd/fd
    lama ke direktori itu pun ditolak kernel. Seluruh isi dibuat BARU oleh
    root (data/ & wine-prefix lama DIBUANG — isi yang pernah dikuasai user
    tidak pernah di-chown ulang), kepemilikan diubah berbasis fd dari daun
    ke akar, dan direktori customer baru dibuka (root:<grup> 0750) PALING
    AKHIR."""
    if chown:
        os.fchown(base_fd, 0, 0)
        os.fchmod(base_fd, 0o700)                 # kunci SEBELUM apa pun
    if owner is None:
        _atomic_write(p["marker"], provision_id, 0o600)   # KLAIM, bukan selesai
    # Dari sini direktori milik provision_id ini, tetapi BELUM lengkap.
    # Sisa instalasi sebelumnya yang tidak lengkap tidak dipercaya.
    for f in (p["complete"], p["unit_tmp"]):
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass
    if p["unit_file"].exists():
        p["unit_file"].unlink()           # unit sisa instalasi tak lengkap
    for name in (PARTIAL, "terminal", "wine-prefix", "data"):
        _rmtree_at(base_fd, name)                 # isi lama tidak dipercaya
    _copy_template(cfg, p)
    os.mkdir("wine-prefix", 0o700, dir_fd=base_fd)
    os.mkdir("data", 0o700, dir_fd=base_fd)
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
        _atomic_write(p["terminal"] / "rpc.token", token, 0o600)
        fmt["wine_python"] = cfg.wine_python
        _atomic_write(p["terminal"] / "run-terminal.sh",
                      RUN_TEMPLATE_RO.format(**fmt), 0o644)
    else:
        _atomic_write(p["terminal"] / "run-terminal.sh",
                      RUN_TEMPLATE.format(**fmt), 0o644)
    _atomic_write(p["terminal"] / "stop-terminal.sh",
                  STOP_TEMPLATE.format(**fmt), 0o644)
    pw = None
    if chown:
        import pwd
        if cfg.service_user_mode == "per_customer":
            ensure_user(cfg, user, p["base"], runner)
        pw = pwd.getpwnam(user)
        # direktori customer masih root:root 0700: tidak ada proses lain
        # yang dapat menyentuh pohon ini selama kepemilikan diubah
        for name in ("terminal", "wine-prefix", "data"):
            sub = os.open(name, _DIR_FLAGS, dir_fd=base_fd)
            try:
                chown_tree_fd(sub, pw.pw_uid, pw.pw_gid)
                os.fchown(sub, pw.pw_uid, pw.pw_gid)
                os.fchmod(sub, 0o700)
            finally:
                os.close(sub)
    _write_unit(p, UNIT_TEMPLATE.format(**fmt))
    runner.run([cfg.systemctl, "daemon-reload"])
    if pw is not None:
        # Dibuka: root:<grup customer> 0750 — customer lain tetap tanpa
        # akses; tidak ada lagi operasi root di DALAM pohon ini.
        os.fchown(base_fd, 0, pw.pw_gid)
        os.fchmod(base_fd, 0o750)
    # B8.1: keadaan akhir direktori DIVERIFIKASI (fstat fd yang sama) lalu
    # dicatat; bukti selesai ditulis PALING AKHIR — setelah salinan
    # terverifikasi, unit, reload, DAN pembukaan direktori berhasil.
    bst = os.fstat(base_fd)
    want_gid = pw.pw_gid if pw is not None else bst.st_gid
    want_mode = 0o750 if pw is not None else stat.S_IMODE(bst.st_mode)
    if pw is not None and (bst.st_uid, bst.st_gid,
                           stat.S_IMODE(bst.st_mode)) != (0, want_gid,
                                                          want_mode):
        raise HelperError(EXIT_FAIL, "keadaan akhir direktori customer tidak"
                                     " sesuai — instalasi tidak selesai")
    _atomic_write(p["complete"], json.dumps(
        {"provision_id": provision_id, "port": port, "rpc_mode": rpc_mode,
         "base_uid": bst.st_uid, "base_gid": want_gid,
         "base_mode": want_mode}), 0o600)
    return {"ok": True, "idempotent": False}


def _copy_template(cfg: HelperConfig, p: dict) -> None:
    """Salin ke terminal.partial -> verifikasi -> rename atomik."""
    shutil.copytree(cfg.template_dir, p["partial"], symlinks=False)
    verify_copy(cfg.template_dir, p["partial"])
    os.replace(p["partial"], p["terminal"])


def _write_unit(p: dict, text: str) -> None:
    """Unit ditulis atomik (tmp tersembunyi .*.tmp tidak dimuat systemd)."""
    try:
        os.unlink(p["unit_tmp"])
    except FileNotFoundError:
        pass
    fd = os.open(p["unit_tmp"], os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 os.O_NOFOLLOW, 0o644)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fchmod(fd, 0o644)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(p["unit_tmp"], p["unit_file"])


def op_start(cfg, code, port, runner, expect_pid: str | None = None,
             expect_mode: str | None = None) -> dict:
    """Start HANYA instalasi lengkap. Hardening: marker WAJIB sama dengan
    expect_pid (bila diberikan; wajib di produksi), mode yang tercatat di
    .install_complete WAJIB sama dengan expect_mode, dan produksi menolak
    instalasi yang bukan 'ro' (termasuk unit classic sisa lama)."""
    p = paths(cfg, code)
    owner = read_marker(p)
    if cfg.production and expect_pid is None:
        raise HelperError(EXIT_ARGS, "--expect-pid wajib di produksi")
    if expect_pid is not None and owner != expect_pid:
        raise HelperError(EXIT_OWNERSHIP, "marker instalasi tidak cocok "
                                          "dengan --expect-pid")
    data = install_complete(p, owner) if owner is not None else None
    if data is None:
        raise HelperError(EXIT_OWNERSHIP, "terminal belum di-install lengkap")
    mode = data.get("rpc_mode")
    if mode not in HELPER_KNOWN_RPC_MODES:
        mode = None                       # format lama: mode TIDAK diketahui
    if expect_mode is not None and mode != expect_mode:
        raise HelperError(EXIT_OWNERSHIP, "mode instalasi tidak cocok "
                                          "dengan --expect-mode")
    if cfg.production and mode not in HELPER_PRODUCTION_RPC_MODES:
        raise HelperError(EXIT_POLICY, "instalasi bukan mode ro — start "
                                       "ditolak di produksi")
    runner.run([cfg.systemctl, "enable", "--now", p["unit"] + ".service"])
    return {"ok": True}


def op_stop(cfg, code, runner, expect_pid: str | None = None) -> dict:
    """Stop unit. Hardening: bila expect_pid diberikan (wajib di produksi)
    marker WAJIB sama; instalasi yang sudah tidak ada (phase absent) =
    idempoten ok."""
    p = paths(cfg, code)
    if cfg.production and expect_pid is None:
        raise HelperError(EXIT_ARGS, "--expect-pid wajib di produksi")
    if expect_pid is not None:
        owner = read_marker(p)
        absent = not p["base"].exists() and not p["unit_file"].exists()
        if not absent and owner != expect_pid:
            raise HelperError(EXIT_OWNERSHIP, "marker instalasi tidak cocok "
                                              "dengan --expect-pid")
    out = {"ok": True}
    if p["unit_file"].exists():
        if expect_pid is not None:
            # hanya jalur baru (berfence/berpid) yang melaporkan keadaan
            # sebelumnya — urutan panggilan jalur lama tidak berubah
            out["was_active"] = runner.run(
                [cfg.systemctl, "is-active", "--quiet",
                 p["unit"] + ".service"], check=False) == 0
        runner.run([cfg.systemctl, "disable", "--now",
                    p["unit"] + ".service"], check=False)
    return out


def _stop_and_check(cfg, code, p, runner, port) -> None:
    """Paritas PR #4 (S5): resource TIDAK dilepas selama terminal lama
    mungkin masih hidup — unit wajib tidak aktif DAN tidak ada listener
    port milik cgroup unit ini; bila masih ada -> gagal, tidak dihapus."""
    op_stop(cfg, code, runner)
    if p["unit_file"].exists() and runner.run(
            [cfg.systemctl, "is-active", "--quiet", p["unit"] + ".service"],
            check=False) == 0:
        raise HelperError(EXIT_FAIL, "terminal masih aktif — tidak dihapus")
    if port is not None and socket_owned_by_unit(cfg, p["unit"], port):
        raise HelperError(EXIT_FAIL, "listener terminal masih hidup — "
                                     "tidak dihapus")


def op_remove(cfg, code, provision_id, runner) -> dict:
    p = paths(cfg, code)
    tomb, manifest = tomb_paths(cfg, code, provision_id)
    have_manifest = manifest.exists() or manifest.is_symlink()
    manifest_ok = have_manifest and read_manifest(manifest, code,
                                                  provision_id)
    if have_manifest and not manifest_ok:
        raise HelperError(EXIT_OWNERSHIP, "manifest tombstone tidak sah")
    if tomb.exists() or tomb.is_symlink():
        if not manifest_ok:
            raise HelperError(EXIT_OWNERSHIP, "tombstone tanpa bukti "
                                              "kepemilikan")
        if tomb.is_symlink() or not tomb.is_dir():
            raise HelperError(EXIT_OWNERSHIP, "tombstone tidak sah")
        inner = tomb / MARKER
        if inner.exists() and not inner.is_symlink() and \
                inner.read_text(encoding="utf-8").strip() != provision_id:
            raise HelperError(EXIT_OWNERSHIP, "penanda tombstone tidak cocok")
    base_exists = p["base"].exists()
    if not base_exists and not p["unit_file"].exists() and not manifest_ok:
        # Idempoten (mis. respons remove sebelumnya hilang). TIDAK ada yang
        # dihapus di sini: tanpa direktori & penanda, kepemilikan tidak dapat
        # dibuktikan. User layanan per-customer yang tersisa (remove terputus
        # antara rmtree dan userdel) hanya DILAPORKAN untuk admin.
        out = {"ok": True, "already_absent": True}
        if cfg.service_user_mode == "per_customer":
            import pwd
            try:
                pwd.getpwnam(service_user_for(cfg, code))
                out["service_user_left"] = True
            except KeyError:
                pass
        return out
    if base_exists:
        if read_marker(p) != provision_id:
            # Tahap 3E: jangan hapus resource yang kepemilikannya tak terbukti
            raise HelperError(EXIT_OWNERSHIP, "kepemilikan tidak terbukti")
        if tomb.exists():
            raise HelperError(EXIT_OWNERSHIP, "direktori & tombstone sama-sama"
                                              " ada — tidak disentuh")
    elif not manifest_ok:
        # tanpa direktori: hanya unit tersisa tanpa bukti -> tidak disentuh
        raise HelperError(EXIT_OWNERSHIP, "kepemilikan tidak terbukti")
    _stop_and_check(cfg, code, p, runner, _unit_port(p))
    resumed = manifest_ok
    if base_exists:
        # Bukti selesai dicabut DULU: sisa tidak dapat di-start lagi.
        for f in (p["complete"], p["unit_tmp"]):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        if not manifest_ok:
            _atomic_write(manifest, json.dumps(
                {"code": code, "provision_id": provision_id,
                 "unit": p["unit"]}), 0o600)
        os.rename(p["base"], tomb)          # ATOMIK, satu filesystem
        _fsync_dir(cfg.base_dir)
        _fsync_dir(tomb.parent)
    if p["unit_file"].exists():
        p["unit_file"].unlink()
        runner.run([cfg.systemctl, "daemon-reload"], check=False)
    try:
        os.unlink(p["unit_tmp"])
    except FileNotFoundError:
        pass
    if tomb.exists():
        shutil.rmtree(tomb)
    if cfg.service_user_mode == "per_customer":
        import pwd
        try:
            pwd.getpwnam(service_user_for(cfg, code))
            runner.run([cfg.userdel, service_user_for(cfg, code)],
                       check=False)
        except KeyError:
            pass
    os.unlink(manifest)                     # PALING AKHIR: bukti dilepas
    _fsync_dir(tomb.parent)
    return {"ok": True, "already_absent": False, "resumed": resumed}


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


# ::1 di /proc/net/tcp6 (urutan byte per kata 32-bit, little-endian host)
_TCP6_LOOPBACK = "00000000000000000000000001000000"


def _listening_inodes(proc_root: Path, port: int) -> list[str]:
    """Inode LISTEN pada 127.0.0.1:port (tcp) DAN [::1]:port (tcp6).
    Hardening: listener IPv6 loopback ikut diperhitungkan."""
    out: list[str] = []
    v4 = _listening_inode(proc_root, port)
    if v4 is not None:
        out.append(v4)
    want = f"{_TCP6_LOOPBACK}:{port:04X}"
    try:
        lines = (proc_root / "net" / "tcp6").read_text().splitlines()[1:]
    except OSError:
        lines = []
    for ln in lines:
        f = ln.split()
        if len(f) > 9 and f[1] == want and f[3] == "0A":
            out.append(f[9])
    return out


def socket_owned_by_unit(cfg: HelperConfig, unit: str, port: int) -> bool:
    """True bila listener 127.0.0.1:port (atau [::1]:port) dimiliki proses
    dalam cgroup unit customer ini — mencegah verifikasi berbicara dengan
    terminal milik customer LAIN yang kebetulan memakai port (Tahap 3D)."""
    inodes = _listening_inodes(cfg.proc_root, port)
    if not inodes:
        return False
    targets = {f"socket:[{i}]" for i in inodes}
    for pid_dir in cfg.proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            cg = (pid_dir / "cgroup").read_text()
            if f"/{unit}.service" not in cg:
                continue
            for fd in (pid_dir / "fd").iterdir():
                if os.readlink(fd) in targets:
                    return True
        except OSError:
            continue
    return False


def op_status(cfg, code, port, runner,
              expect_pid: str | None = None) -> dict:
    """Status terminal. Hardening: di produksi --expect-pid WAJIB (sama
    seperti start/stop) — status tanpa identitas instalasi tidak boleh
    dipakai sebagai bukti terminal yang benar sedang berjalan."""
    if cfg.production and expect_pid is None:
        raise HelperError(EXIT_ARGS, "--expect-pid wajib di produksi")
    p = paths(cfg, code)
    busy = lock_busy(cfg, code)
    installed = phase_of(p, tombs_for(cfg, code)) == "complete"
    marker = read_marker(p)
    marker_match = None if expect_pid is None else marker == expect_pid
    if expect_pid is not None and not marker_match:
        installed = False                 # bukan instalasi yang diharapkan
    active = installed and not busy and runner.run(
        [cfg.systemctl, "is-active", "--quiet", p["unit"] + ".service"],
        check=False) == 0
    owned = active and socket_owned_by_unit(cfg, p["unit"], port)
    out = {"ok": True, "installed": installed, "active": active,
           "busy": busy, "listener_owned": owned,
           "running": bool(active and owned)}
    if expect_pid is not None:
        out["marker_match"] = bool(marker_match)
        out["rpc_mode"] = _complete_mode(p, marker)
    return out


def _complete_mode(p: dict, marker: str | None) -> str | None:
    """Mode RPC yang tercatat di .install_complete milik marker ini."""
    data = read_complete(p, marker) if marker is not None else None
    mode = data.get("rpc_mode") if data else None
    return mode if mode in HELPER_KNOWN_RPC_MODES else None


def op_inspect(cfg, code, provision_id) -> dict:
    """Read-only (F2): dipakai API setelah timeout untuk membedakan helper
    masih berjalan (busy) / berhenti dengan instalasi lengkap / tidak."""
    p = paths(cfg, code)
    owner = read_marker(p)
    tombs = tombs_for(cfg, code)
    match = None
    if provision_id is not None:
        match = owner == provision_id
        if not p["base"].exists() and tombs:
            # penghapusan berjalan: bukti = manifest tombstone milik pid ini
            match = read_manifest(tomb_paths(cfg, code, provision_id)[1],
                                  code, provision_id)
    marker = read_marker(p)
    return {"ok": True, "busy": lock_busy(cfg, code),
            "phase": phase_of(p, tombs), "owner_match": match,
            "unit_file": p["unit_file"].exists(),
            "partial": p["partial"].exists(), "tombstones": len(tombs),
            # Hardening (tambahan, read-only): identitas & mode instalasi,
            # fence migrasi, dan proses tersisa.
            "marker": marker, "rpc_mode": _complete_mode(p, marker),
            "fence": _fence_public(cfg, code),
            "unclean": unclean_alive(cfg, code)}


# =================================================== MT5 RPC Hardening (G1)
# ---- kebijakan (PARITAS backend/security/rpc_policy.py) ----------------
def parse_broker_allow(raw, *, production: bool) -> list:
    """Salinan PARITAS rpc_policy.parse_broker_allow (lihat docstring di
    sana). Diuji identik oleh tests/test_rpc_policy_parity.py."""
    import ipaddress
    if not isinstance(raw, str):
        raise ValueError("broker_allow harus string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in raw):
        raise ValueError("broker_allow memuat karakter kontrol")
    toks = raw.split()
    if not toks:
        raise ValueError("broker_allow kosong")
    if toks == ["any"]:
        if production:
            raise ValueError("broker_allow 'any' dilarang di produksi")
        return ["any"]
    out = []
    for t in toks:
        if t == "any":
            raise ValueError("'any' tidak boleh dicampur CIDR")
        try:
            net = ipaddress.ip_network(t, strict=False)
        except ValueError:
            raise ValueError(f"broker_allow bukan IP/CIDR: {t[:40]!r}") \
                from None
        if str(net) not in out:
            out.append(str(net))
    return out


def _decode_secret_bytes(secret: str):
    import base64
    import binascii
    s = secret.strip()
    if re.fullmatch(r"[0-9a-fA-F]+", s) and len(s) % 2 == 0:
        try:
            return binascii.unhexlify(s)
        except binascii.Error:
            return None
    if re.fullmatch(r"[A-Za-z0-9_\-]+=*", s):
        try:
            pad = "=" * (-len(s.rstrip("=")) % 4)
            return base64.urlsafe_b64decode(s.rstrip("=") + pad)
        except (binascii.Error, ValueError):
            return None
    return None


def secret_strength_problem(secret) -> str | None:
    """PARITAS rpc_policy.secret_strength_problem (tanpa forbid_equal_to:
    helper tidak mengenal secret API lain)."""
    if not isinstance(secret, str):
        return "secret_missing"
    s = secret.strip()
    if s.lower() in SECRET_PLACEHOLDERS:
        return "secret_placeholder"
    raw = _decode_secret_bytes(s)
    if raw is None:
        return "secret_encoding"
    if len(raw) < SECRET_MIN_BYTES:
        return "secret_too_short"
    if len(set(raw)) < 8:
        return "secret_low_entropy"
    return None


def selfcheck_proof(secret: str, nonce_hex: str) -> str:
    """PARITAS rpc_policy.selfcheck_proof: HMAC-SHA256(secret,
    DOMAIN || nonce). Membuktikan kecocokan secret TANPA mengirimnya."""
    import hashlib
    import hmac
    if not isinstance(nonce_hex, str) or not NONCE_RE.fullmatch(nonce_hex):
        raise ValueError("nonce harus 64 hex huruf kecil")
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret kosong")
    msg = (SELFCHECK_DOMAIN + nonce_hex).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def production_problems(cfg: HelperConfig) -> list:
    """Daftar pelanggaran kebijakan produksi (kosong = siap). TANPA nilai
    rahasia — hanya kode masalah."""
    probs = []
    if cfg.service_user_mode != "per_customer":
        probs.append("service_user_mode_bukan_per_customer")
    if cfg.ro_service is not True:
        probs.append("ro_service_nonaktif")
    sp = secret_strength_problem(cfg.rpc_secret)
    if sp is not None:
        probs.append(sp)
    if not cfg.wine_python or not os.path.isabs(cfg.wine_python):
        probs.append("wine_python_tidak_absolut")
    try:
        parse_broker_allow(cfg.broker_allow, production=True)
    except ValueError:
        probs.append("broker_allow_bukan_cidr")
    return probs


def broker_allow_mode(cfg: HelperConfig) -> str:
    try:
        nets = parse_broker_allow(cfg.broker_allow, production=False)
    except ValueError:
        return "invalid"
    return "any" if nets == ["any"] else "cidr"


# ---- fence migrasi (K1) -------------------------------------------------
def _fence_root(cfg: HelperConfig) -> Path:
    """<base_dir>/.fence — PERSISTEN (bukan /run): fence wajib bertahan
    melewati reboot. Root-only 0700, seperti direktori tombstone."""
    d = cfg.base_dir / FENCE_DIR
    try:
        d.mkdir(mode=0o700, parents=False)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or st.st_mode & 0o077):
        raise HelperError(EXIT_FAIL, "direktori fence tidak aman")
    return d


def _fence_path(cfg: HelperConfig, code: str) -> Path:
    if not CODE_RE.fullmatch(code):
        raise HelperError(EXIT_ARGS, "kode customer tidak sah")
    return cfg.base_dir / FENCE_DIR / code


def read_fence(cfg: HelperConfig, code: str) -> dict | None:
    """Fence tersimpan untuk kode ini, atau None bila belum pernah ada.
    Berkas rusak / bukan milik root / format tak dikenal -> HelperError
    EXIT_FENCE (fail-closed: tidak ada operasi berfence yang dilanjutkan)."""
    path = _fence_path(cfg, code)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or \
            st.st_mode & 0o077:
        raise HelperError(EXIT_FENCE, "berkas fence tidak sah")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HelperError(EXIT_FENCE, "berkas fence rusak") from None
    ok = (isinstance(data, dict) and data.get("v") == FENCE_VERSION and
          isinstance(data.get("migration_id"), str) and
          MID_RE.fullmatch(data["migration_id"]) and
          isinstance(data.get("gen"), int) and
          not isinstance(data.get("gen"), bool) and data["gen"] >= 1 and
          isinstance(data.get("released"), bool))
    if not ok:
        raise HelperError(EXIT_FENCE, "format fence tidak dikenal")
    return data


def _fence_public(cfg: HelperConfig, code: str) -> dict | None:
    """Ringkasan fence untuk inspect/audit (read-only, tidak melempar)."""
    try:
        f = read_fence(cfg, code)
    except HelperError:
        return {"invalid": True}
    if f is None:
        return None
    return {"migration_id": f["migration_id"], "gen": f["gen"],
            "released": f["released"]}


def _write_fence(cfg: HelperConfig, code: str, migration_id: str, gen: int,
                 released: bool) -> None:
    _fence_root(cfg)
    _atomic_write(_fence_path(cfg, code), json.dumps(
        {"v": FENCE_VERSION, "migration_id": migration_id, "gen": gen,
         "released": released}), 0o600)


def check_fence(cfg: HelperConfig, code: str, migration_id: str | None,
                gen: int | None) -> None:
    """Dipanggil DI BAWAH flock, SEBELUM operasi apa pun (FENCED_OPS).

    - Dengan --migration-id/--fence: fence tersimpan WAJIB ada, belum
      dilepas, migration_id sama, dan gen PERSIS sama. gen lebih kecil =
      worker basi; lebih besar = fence-advance belum dijalankan. Selain
      itu exit 8.
    - Tanpa argumen fence (jalur provisioning biasa): ditolak exit 8 bila
      ada migrasi AKTIF (fence tersimpan, belum dilepas) atas kode ini."""
    stored = read_fence(cfg, code)
    if migration_id is None and gen is None:
        if stored is not None and not stored["released"]:
            raise HelperError(EXIT_FENCE, "terminal sedang dipegang migrasi "
                                          "RPC — operasi tanpa fence ditolak")
        return
    if stored is None:
        raise HelperError(EXIT_FENCE, "fence belum di-advance untuk "
                                      "migrasi ini")
    if stored["released"]:
        raise HelperError(EXIT_FENCE, "fence migrasi sudah dilepas")
    if stored["migration_id"] != migration_id:
        raise HelperError(EXIT_FENCE, "fence milik migrasi lain")
    if stored["gen"] != gen:
        raise HelperError(EXIT_FENCE, "generasi fence tidak sama (worker "
                                      "basi atau advance belum dijalankan)")


def op_fence_advance(cfg, code, migration_id: str, gen: int) -> dict:
    """K1: dijalankan DI BAWAH flock — operasi generasi lama yang sedang
    memegang lock PASTI selesai dulu; operasi generasi lama yang mendapat
    lock SESUDAHNYA ditolak check_fence (exit 8). Mengembalikan inspect
    lengkap yang dibaca DI DALAM lock yang sama (dasar keputusan worker
    baru). Idempoten untuk (migration_id, gen) yang sama."""
    stored = read_fence(cfg, code)
    if stored is not None:
        same = stored["migration_id"] == migration_id
        if stored["released"]:
            if same:
                raise HelperError(EXIT_FENCE, "fence migrasi ini sudah "
                                              "dilepas")
        elif not same:
            raise HelperError(EXIT_FENCE, "migrasi lain masih memegang "
                                          "fence")
        elif gen < stored["gen"]:
            raise HelperError(EXIT_FENCE, "generasi lebih kecil dari fence "
                                          "tersimpan")
    if stored is None or stored["released"] or gen > stored["gen"]:
        _write_fence(cfg, code, migration_id, gen, False)
    out = op_inspect_unlocked(cfg, code, None)
    out["fence"] = _fence_public(cfg, code)
    return out


def op_fence_release(cfg, code, migration_id: str, gen: int) -> dict:
    """Lepas fence saat migrasi FINAL (done/cancelled/failed_credentials).
    Hanya pemegang (migration_id, gen) terkini; idempoten."""
    stored = read_fence(cfg, code)
    if stored is None or stored["migration_id"] != migration_id:
        raise HelperError(EXIT_FENCE, "fence bukan milik migrasi ini")
    if stored["gen"] != gen:
        raise HelperError(EXIT_FENCE, "generasi fence tidak sama")
    if not stored["released"]:
        _write_fence(cfg, code, migration_id, gen, True)
    return {"ok": True, "fence": _fence_public(cfg, code)}


def op_inspect_unlocked(cfg, code, provision_id) -> dict:
    """inspect TANPA mencoba lock (dipanggil ketika lock sudah dipegang
    proses ini — flock pada fd baru di proses yang sama akan bentrok)."""
    p = paths(cfg, code)
    tombs = tombs_for(cfg, code)
    marker = read_marker(p)
    return {"ok": True, "busy": False, "phase": phase_of(p, tombs),
            "owner_match": None if provision_id is None
            else marker == provision_id,
            "unit_file": p["unit_file"].exists(),
            "partial": p["partial"].exists(), "tombstones": len(tombs),
            "marker": marker, "rpc_mode": _complete_mode(p, marker),
            "fence": _fence_public(cfg, code)}


# ---- selfcheck (T4) & audit-all (K3) -----------------------------------
def op_selfcheck(cfg: HelperConfig, nonce: str | None) -> dict:
    """Read-only. TIDAK PERNAH mengembalikan secret; kecocokan secret
    dibuktikan lewat secret_proof = HMAC(secret, DOMAIN||nonce)."""
    lock_dir_ok = True
    unclean = 0
    try:
        d = _lock_dir(cfg)
        for n in os.listdir(d):
            if n.endswith(".unclean") and CODE_RE.fullmatch(n[:-8]) and \
                    unclean_alive(cfg, n[:-8]):
                unclean += 1
    except (HelperError, OSError):
        lock_dir_ok = False
    out = {"ok": True, "production": bool(cfg.production),
           "service_user_mode": cfg.service_user_mode,
           "ro_service": bool(cfg.ro_service),
           "broker_allow_mode": broker_allow_mode(cfg),
           "wine_python_ok": bool(cfg.wine_python) and
           os.path.isabs(cfg.wine_python),
           "secret_ok": secret_strength_problem(cfg.rpc_secret) is None,
           "op_timeout_seconds": float(cfg.op_timeout_seconds),
           "lock_wait_seconds": float(cfg.lock_wait_seconds),
           "lock_dir_ok": lock_dir_ok, "unclean_count": unclean,
           "production_problems": production_problems(cfg)}
    if nonce is not None:
        if not NONCE_RE.fullmatch(nonce):
            raise HelperError(EXIT_ARGS, "nonce tidak sah")
        out["secret_proof"] = (selfcheck_proof(cfg.rpc_secret, nonce)
                               if cfg.rpc_secret else None)
    return out


def _render_run_script(cfg: HelperConfig, code: str, port: int,
                       kind: str) -> str:
    p = paths(cfg, code)
    fmt = {"code": code, "port": port, "user": service_user_for(cfg, code),
           "base": p["base"], "terminal_dir": p["terminal"],
           "wine_prefix": p["wine_prefix"], "wine_bin": cfg.wine_bin,
           "broker_allow": cfg.broker_allow,
           "wine_python": cfg.wine_python}
    tpl = RUN_TEMPLATE_RO if kind == "ro" else RUN_TEMPLATE
    return tpl.format(**fmt)


def _run_script_kind(cfg: HelperConfig, code: str, port: int | None) -> str:
    """'ro' / 'classic' bila skrip run SAMA PERSIS dengan template yang
    dirender helper; 'missing' bila tidak ada; selain itu 'unknown'."""
    path = paths(cfg, code)["terminal"] / "run-terminal.sh"
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return "unknown"
        txt = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    if port is None:
        return "unknown"
    for kind in ("ro", "classic"):
        try:
            if txt == _render_run_script(cfg, code, port, kind):
                return kind
        except (KeyError, IndexError, ValueError):
            continue
    return "unknown"


def _code_from_unit_name(name: str) -> str | None:
    if not (name.startswith(UNIT_PREFIX) and name.endswith(".service")):
        return None
    code = name[len(UNIT_PREFIX):-len(".service")].upper()
    return code if CODE_RE.fullmatch(code) else None


class _Inv:
    """Pencatat galat inventaris audit-all (FAIL-CLOSED).

    Setiap sumber yang TIDAK dapat dibaca lengkap (izin ditolak, berkas
    hilang, /proc tidak tersedia, pwd gagal) dicatat sebagai kode galat
    — TIDAK PERNAH ditelan. audit_complete = tidak ada galat. Readiness
    (API) wajib merah bila audit tidak lengkap."""

    def __init__(self):
        self.errors: list = []

    def add(self, source: str, exc: BaseException | str) -> None:
        kind = exc if isinstance(exc, str) else type(exc).__name__
        item = f"{source}:{kind}"
        if item not in self.errors:
            self.errors.append(item)

    def listdir(self, source: str, d: Path, *, required: bool) -> list:
        """Isi direktori terurut. Tidak ada + required -> galat; tidak ada
        + opsional -> []; symlink / bukan direktori / gagal baca -> galat."""
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            if required:
                self.add(source, "tidak_ada")
            return []
        except OSError as e:
            self.add(source, e)
            return []
        if not stat.S_ISDIR(st.st_mode):
            self.add(source, "bukan_direktori")
            return []
        try:
            return sorted(os.listdir(d))
        except OSError as e:
            self.add(source, e)
            return []

    def read_text(self, source: str, path: Path) -> str | None:
        try:
            return path.read_text()
        except FileNotFoundError:
            self.add(source, "tidak_ada")
        except OSError as e:
            self.add(source, e)
        return None


_PROC_NET_SL_RE = re.compile(r"\d+:")
_PROC_NET_ADDR_RE = {"tcp": re.compile(r"[0-9A-Fa-f]{8}:[0-9A-Fa-f]{4}"),
                     "tcp6": re.compile(r"[0-9A-Fa-f]{32}:[0-9A-Fa-f]{4}")}
_PROC_NET_ST_RE = re.compile(r"[0-9A-Fa-f]{2}")
_PROC_NET_MIN_FIELDS = 10          # sl local rem st tx:rx tr:tm rt uid to inode
# Header persis seperti kernel Linux (net/ipv4/tcp_ipv4.c tcp4_seq_show,
# net/ipv6/tcp_ipv6.c tcp6_seq_show); dibandingkan per token sehingga
# padding spasi tidak berpengaruh.
_PROC_NET_HEADER = {
    "tcp": ("sl", "local_address", "rem_address", "st", "tx_queue",
            "rx_queue", "tr", "tm->when", "retrnsmt", "uid", "timeout",
            "inode"),
    "tcp6": ("sl", "local_address", "remote_address", "st", "tx_queue",
             "rx_queue", "tr", "tm->when", "retrnsmt", "uid", "timeout",
             "inode"),
}


def _listening_ports_checked(proc_root: Path, lo: int, hi: int,
                             inv: _Inv) -> dict:
    """{port: [inode,...]} untuk SEMUA alamat (tcp & tcp6) pada rentang
    port [lo, hi). /proc/net/tcp & tcp6 yang hilang atau tidak terbaca
    DICATAT sebagai galat (bukan dilewati).

    FAIL-CLOSED per baris: SETIAP baris non-header (apa pun state-nya)
    divalidasi. Kolom < 10 -> "kolom_tidak_lengkap"; sl/alamat lokal/
    alamat remote/state/inode tidak sesuai format -> "baris_rusak".
    Header baris pertama WAJIB sama per token dengan header kernel;
    berbeda -> "header_rusak", baris pertama berupa data ->
    "header_tidak_ada".
    Baris yang tidak valid tidak pernah dilewati diam-diam: inventaris
    dianggap tidak lengkap (audit_complete=false)."""
    found: dict[int, list[str]] = {}
    for name in ("tcp", "tcp6"):
        src = f"proc_net_{name}"
        txt = inv.read_text(src, proc_root / "net" / name)
        if txt is None:
            continue
        lines = txt.splitlines()
        if not lines:
            inv.add(src, "kosong")
            continue
        addr_re = _PROC_NET_ADDR_RE[name]
        hdr = lines[0].split()
        if tuple(hdr) == _PROC_NET_HEADER[name]:
            rows = lines[1:]
        elif hdr and _PROC_NET_SL_RE.fullmatch(hdr[0]):
            # baris pertama berupa baris data: header HILANG. Tetap
            # dipindai sebagai data (tidak ada listener yang hilang),
            # tetapi inventaris tidak lengkap.
            inv.add(src, "header_tidak_ada")
            rows = lines
        else:
            inv.add(src, "header_rusak")
            rows = lines[1:]
        for ln in rows:
            f = ln.split()
            if len(f) < _PROC_NET_MIN_FIELDS:
                inv.add(src, "kolom_tidak_lengkap")
                continue
            if not (_PROC_NET_SL_RE.fullmatch(f[0])
                    and addr_re.fullmatch(f[1])
                    and addr_re.fullmatch(f[2])
                    and _PROC_NET_ST_RE.fullmatch(f[3])
                    and f[9].isascii() and f[9].isdigit()):
                inv.add(src, "baris_rusak")
                continue
            if f[3].upper() != "0A":
                continue
            port = int(f[1].rsplit(":", 1)[1], 16)
            if lo <= port < hi:
                found.setdefault(port, []).append(f[9])
    return found


def _proc_socket_owners(proc_root: Path, inodes: set, inv: _Inv) -> dict:
    """{inode: set(unit)} untuk socket [inodes] dari /proc/<pid>/fd +
    cgroup. /proc tidak terbaca -> galat. Proses yang KELUAR saat dipindai
    (FileNotFoundError/ProcessLookupError) wajar dan dilewati; izin ditolak
    pada fd/cgroup proses lain -> galat (inventaris tidak lengkap)."""
    owners: dict[str, set] = {i: set() for i in inodes}
    if not inodes:
        return owners
    targets = {f"socket:[{i}]": i for i in inodes}
    names = inv.listdir("proc", proc_root, required=True)
    for name in names:
        if not name.isdigit():
            continue
        pid_dir = proc_root / name
        try:
            fds = [os.readlink(fd) for fd in (pid_dir / "fd").iterdir()]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as e:
            inv.add("proc_pid_fd", e)
            continue
        hit = [targets[x] for x in fds if x in targets]
        if not hit:
            continue
        try:
            cg = (pid_dir / "cgroup").read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as e:
            inv.add("proc_pid_cgroup", e)
            continue
        m = re.search(r"/(" + re.escape(UNIT_PREFIX) + r"[a-z0-9]+)\.service",
                      cg)
        for i in hit:
            owners[i].add(m.group(1) if m else "?")
    return owners


def op_audit_all(cfg: HelperConfig, runner) -> dict:
    """Inventaris READ-ONLY lengkap (K3) untuk rekonsiliasi dua arah di
    API. Tidak mengklasifikasikan sehat/tidak — itu tugas API (dengan DB).
    Setiap entri yang tidak dikenali dilaporkan di unknown_entries.

    FAIL-CLOSED: setiap sumber yang tidak terbaca lengkap masuk
    inventory_errors dan audit_complete=false (readiness wajib merah)."""
    inv = _Inv()
    codes: set = set()
    unknown: list = []
    known_base = {TOMB_DIR, FENCE_DIR}
    for n in inv.listdir("base_dir", cfg.base_dir, required=True):
        if n in known_base:
            continue
        if CODE_RE.fullmatch(n):
            codes.add(n)
        else:
            unknown.append(f"base:{n}")
    for n in inv.listdir("unit_dir", cfg.unit_dir, required=True):
        if not n.startswith(UNIT_PREFIX) and \
                not n.startswith("." + UNIT_PREFIX):
            continue
        c = _code_from_unit_name(n)
        if c is None:
            unknown.append(f"unit:{n}")
        else:
            codes.add(c)
    for n in inv.listdir("tombstone_dir", cfg.base_dir / TOMB_DIR,
                         required=False):
        c = n.split(".", 1)[0]
        if CODE_RE.fullmatch(c):
            codes.add(c)
        else:
            unknown.append(f"tombstone:{n}")
    for n in inv.listdir("fence_dir", cfg.base_dir / FENCE_DIR,
                         required=False):
        if CODE_RE.fullmatch(n):
            codes.add(n)
        elif not n.startswith("."):
            unknown.append(f"fence:{n}")
    # lock_dir (tmpfs /run) boleh belum ada setelah reboot: opsional
    for n in inv.listdir("lock_dir", cfg.lock_dir, required=False):
        stem, _, ext = n.partition(".")
        if ext in ("lock", "unclean") and CODE_RE.fullmatch(stem):
            if ext == "unclean":
                codes.add(stem)
        elif not n.startswith("."):
            unknown.append(f"lock:{n}")
    users_orphan: list = []
    try:
        import pwd
        entries = pwd.getpwall()
    except Exception as e:  # noqa: BLE001 — DICATAT, tidak ditelan
        inv.add("pwd", e)
        entries = []
    for pw in entries:
        if pw.pw_name.startswith("mt5-"):
            c = pw.pw_name[4:].upper()
            if CODE_RE.fullmatch(c):
                if cfg.service_user_mode == "per_customer":
                    codes.add(c)
            else:
                users_orphan.append(pw.pw_name)
    user_names = {pw.pw_name for pw in entries}
    lo, hi = cfg.port_base, cfg.port_base + cfg.max_slots
    ports = _listening_ports_checked(cfg.proc_root, lo, hi, inv)
    all_inodes = {i for v in ports.values() for i in v}
    owners = _proc_socket_owners(cfg.proc_root, all_inodes, inv)
    terminals = []
    for c in sorted(codes):
        try:
            p = paths(cfg, c)
            tombs = tombs_for(cfg, c)
            marker = read_marker(p)
            complete = read_complete(p, marker) if marker else None
            unit_exists = p["unit_file"].exists()
        except (OSError, HelperError) as e:
            inv.add(f"terminal_{c}", e)
            terminals.append({"code": c, "error": True})
            continue
        unit_port = _unit_port(p) if unit_exists else None
        port = (complete or {}).get("port") if complete else None
        port = port if isinstance(port, int) else unit_port
        unit_active = None
        if unit_exists:
            try:
                unit_active = runner.run(
                    [cfg.systemctl, "is-active", "--quiet",
                     p["unit"] + ".service"], check=False) == 0
            except Exception as e:  # noqa: BLE001 — DICATAT
                inv.add(f"systemctl_{c}", e)
        listener_owned = False
        if port is not None and port in ports:
            listener_owned = any(p["unit"] in owners.get(i, set())
                                 for i in ports[port])
        try:
            unclean = unclean_alive(cfg, c)
        except OSError as e:
            inv.add(f"unclean_{c}", e)
            unclean = True
        terminals.append({
            "code": c, "phase": phase_of(p, tombs),
            "base_exists": p["base"].exists(), "marker": marker,
            "complete": None if complete is None else {
                "provision_id": complete.get("provision_id"),
                "port": complete.get("port"),
                "rpc_mode": complete.get("rpc_mode")},
            "partial": p["partial"].exists(), "tombstones": len(tombs),
            "unit_file": unit_exists, "unit_port": unit_port,
            "unit_active": unit_active,
            "run_script_kind": _run_script_kind(cfg, c, port),
            "listener_owned": bool(listener_owned and unit_active),
            "unclean": unclean,
            "fence": _fence_public(cfg, c),
            "service_user_exists": (service_user_for(cfg, c) in user_names)
            if cfg.service_user_mode == "per_customer" and entries
            else None})
    listeners = [{"port": port, "owner_units": sorted(
        {u for i in inodes for u in owners.get(i, set())})}
        for port, inodes in sorted(ports.items())]
    return {"ok": True, "production": bool(cfg.production),
            "port_window": [lo, hi],
            "audit_complete": not inv.errors,
            "inventory_errors": list(inv.errors),
            "terminals": terminals, "unknown_entries": unknown,
            "user_orphans": users_orphan, "listeners": listeners}


def dispatch(cfg: HelperConfig, argv: list[str], runner=None) -> dict:
    ap = argparse.ArgumentParser(prog="transcendence-mt5-provision",
                                 add_help=False, allow_abbrev=False)
    ap.add_argument("op", choices=OPS)
    ap.add_argument("--code")
    ap.add_argument("--port", type=int)
    ap.add_argument("--provision-id")
    # Hardening: TANPA default di parser. Produksi: install WAJIB menyebut
    # mode (dan harus ro). Non-produksi: default lama 'classic' dipertahankan
    # agar perilaku dev/test tidak berubah (API selalu mengirim eksplisit).
    ap.add_argument("--rpc-mode", choices=("classic", "ro"))
    ap.add_argument("--expect-pid")
    ap.add_argument("--expect-mode", choices=("classic", "ro"))
    ap.add_argument("--migration-id")
    ap.add_argument("--fence", type=int)
    ap.add_argument("--gen", type=int)
    ap.add_argument("--nonce")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        raise HelperError(EXIT_ARGS, "argumen tidak sah") from e
    _validate_hardening_args(a)
    if a.op == "install" and a.rpc_mode is None:
        if cfg.production:
            raise HelperError(EXIT_ARGS, "--rpc-mode wajib untuk install "
                                         "di produksi")
        a.rpc_mode = "classic"            # default lama, HANYA non-produksi
    runner = runner or Runner()
    if a.op in GLOBAL_OPS:
        validate_global(cfg, a.op)
        if a.op == "selfcheck":
            return op_selfcheck(cfg, a.nonce)
        return op_audit_all(cfg, runner)
    if a.code is None:
        raise HelperError(EXIT_ARGS, "--code wajib")
    validate(cfg, a.op, a.code, a.port, a.provision_id)
    if a.op == "inspect" and a.provision_id is not None and \
            not PID_RE.fullmatch(a.provision_id):
        raise HelperError(EXIT_ARGS, "provision-id tidak sah")
    global _UNCLEAN_SINK

    held = {"lock": False}        # True selama lock operasi dipegang

    def sink(surv):
        persist_unclean(cfg, a.code, surv, lock_held=held["lock"])
    try:
        runner.on_unclean = sink
    except AttributeError:
        pass
    _UNCLEAN_SINK = sink
    if a.op == "inspect":
        return op_inspect(cfg, a.code, a.provision_id)
    if a.op == "status":
        return op_status(cfg, a.code, a.port, runner,
                         expect_pid=a.expect_pid)
    if _HELPER_PROCESS and not _SUBREAPER_OK:
        # M1: tanpa subreaper, turunan yang keluar dari process group tidak
        # dapat dijamin berhenti -> TOLAK sebelum lock & sebelum perubahan
        raise HelperError(EXIT_FAIL, "subreaper tidak tersedia — penghentian "
                                     "turunan tidak dapat dijamin; operasi "
                                     "ditolak")
    lock_fd = acquire_lock(cfg, a.code)       # F2: serialisasi per customer
    held["lock"] = True
    try:
        runner.pass_fds = (lock_fd,)
        if a.op == "fence-advance":
            return op_fence_advance(cfg, a.code, a.migration_id, a.gen)
        if a.op == "fence-release":
            return op_fence_release(cfg, a.code, a.migration_id, a.gen)
        # K1: fence diperiksa DI BAWAH lock, SEBELUM perubahan apa pun
        check_fence(cfg, a.code, a.migration_id, a.fence)
        if a.op == "install":
            return op_install(cfg, a.code, a.port, a.provision_id, runner,
                              rpc_mode=a.rpc_mode)
        if a.op == "start":
            return op_start(cfg, a.code, a.port, runner,
                            expect_pid=a.expect_pid,
                            expect_mode=a.expect_mode)
        if a.op == "stop":
            return op_stop(cfg, a.code, runner, expect_pid=a.expect_pid)
        return op_remove(cfg, a.code, a.provision_id, runner)
    finally:
        held["lock"] = False
        os.close(lock_fd)


def _validate_hardening_args(a) -> None:
    """Validasi bentuk argumen baru (sebelum lock & sebelum perubahan)."""
    if a.expect_pid is not None:
        if a.op not in ("start", "stop", "status") or \
                not PID_RE.fullmatch(a.expect_pid):
            raise HelperError(EXIT_ARGS, "--expect-pid tidak sah")
    if a.expect_mode is not None and a.op != "start":
        raise HelperError(EXIT_ARGS, "--expect-mode hanya untuk start")
    has_mid = a.migration_id is not None
    if has_mid and not MID_RE.fullmatch(a.migration_id):
        raise HelperError(EXIT_ARGS, "--migration-id tidak sah")
    for name, v in (("--fence", a.fence), ("--gen", a.gen)):
        if v is not None and (isinstance(v, bool) or v < 1 or v > 10**18):
            raise HelperError(EXIT_ARGS, f"{name} tidak sah")
    if a.op in ("fence-advance", "fence-release"):
        if not has_mid or a.gen is None or a.fence is not None:
            raise HelperError(EXIT_ARGS, "--migration-id dan --gen wajib")
    elif a.gen is not None:
        raise HelperError(EXIT_ARGS, "--gen hanya untuk fence-advance/"
                                     "fence-release")
    if a.op in FENCED_OPS:
        if has_mid != (a.fence is not None):
            raise HelperError(EXIT_ARGS, "--migration-id dan --fence wajib "
                                         "berpasangan")
    elif a.op not in ("fence-advance", "fence-release") and \
            (has_mid or a.fence is not None):
        raise HelperError(EXIT_ARGS, "--migration-id/--fence tidak berlaku "
                                     "untuk operasi ini")
    if a.nonce is not None and a.op != "selfcheck":
        raise HelperError(EXIT_ARGS, "--nonce hanya untuk selfcheck")
    if a.op in GLOBAL_OPS and (a.code is not None or a.port is not None or
                               a.provision_id is not None):
        raise HelperError(EXIT_ARGS, "operasi global tanpa --code/--port")


_UNCLEAN_SINK = None      # pencatat identitas proses tersisa (diisi dispatch)


def _interrupt(signum, _frame):
    raise HelperError(EXIT_INTERRUPTED,
                      "dihentikan (batas waktu helper)" if signum ==
                      signal.SIGALRM else f"dihentikan sinyal {signum}"
                      " — operasi tidak lengkap")


def _install_signal_handlers(cfg: HelperConfig) -> None:
    global _HELPER_PROCESS, _SUBREAPER_OK
    # H1: SIGCHLD WAJIB default — disposisi SIG_IGN diwarisi melewati exec
    # (mis. dari sudo/API) dan membuat kernel menuai anak otomatis.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    _HELPER_PROCESS = True
    _SUBREAPER_OK = _set_subreaper()
    for sig in _OP_SIGNALS:
        signal.signal(sig, _interrupt)
    signal.setitimer(signal.ITIMER_REAL, float(cfg.op_timeout_seconds))


def _quiet_signals() -> list:
    """Bisukan sinyal operasi lalu sapu turunan terakhir kali. Return:
    proses yang MASIH HIDUP ([] = bersih) — sudah dicatat sebagai unclean."""
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.pthread_sigmask(signal.SIG_BLOCK, set(_OP_SIGNALS))
    if not _HELPER_PROCESS:
        return []
    try:
        left = kill_descendants()
    except Exception:  # noqa: BLE001
        left = [UNKNOWN_IDENT]            # tidak dapat dipastikan
    if left and _UNCLEAN_SINK is not None:
        try:
            _UNCLEAN_SINK(left)           # termasuk identitas TIDAK diketahui
        except Exception:  # noqa: BLE001
            _guard(left)                  # tidak pernah ditelan
    return left


def _emit(code: int, payload: dict) -> int:
    """Jaring terakhir (M2): bila masih ada turunan hidup, hasil APA PUN
    (termasuk sukses) diganti exit 7 — tidak pernah {"ok": true}."""
    if _quiet_signals():
        code = EXIT_UNCLEAN
        payload = {"ok": False, "code": EXIT_UNCLEAN,
                   "error": "proses turunan masih hidup setelah batas "
                            "pembersihan — operasi TIDAK bersih"}
    print(json.dumps(payload))
    return code


def main(argv: list[str] | None = None,
         config_path: str = CONFIG_PATH) -> int:
    try:
        if os.geteuid() != 0:
            raise HelperError(EXIT_FAIL, "helper wajib dijalankan via sudo")
        cfg = HelperConfig.load(config_path)
        _install_signal_handlers(cfg)
        out = dispatch(cfg, list(sys.argv[1:] if argv is None else argv))
        return _emit(EXIT_OK, out)
    except HelperError as e:
        return _emit(e.code, {"ok": False, "error": e.msg, "code": e.code})
    except Exception:  # noqa: BLE001 — tanpa traceback (tanpa info sensitif)
        return _emit(EXIT_FAIL, {"ok": False, "error": "kegagalan internal",
                                 "code": EXIT_FAIL})


if __name__ == "__main__":
    sys.exit(main())
