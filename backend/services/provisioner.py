"""Klien provisioning terminal dari sisi API (TANPA hak root).

- MockProvisioner  : dev/test — direktori + penanda kepemilikan di
                     term_base_dir, status "running" disimulasikan
                     (dapat diberi delay/kegagalan untuk menguji lifecycle).
- HelperProvisioner: produksi — memanggil helper privileged lewat
                     `sudo -n <helper> <op> --code .. --port .. --provision-id ..`
                     (argv list, shell=False, env minimal, timeout). Semua
                     parameter DIBANGKITKAN server (kode dari id customer,
                     port dari alokasi DB, provision_id uuid4) dan divalidasi
                     DI SINI juga sebelum dikirim — input pelanggan (login,
                     server, password) tidak pernah menjadi argumen.

Lihat provisioner/mt5_provision_helper.py untuk validasi sisi helper.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

CODE_RE = re.compile(r"^C[0-9]{3,9}$")
PID_RE = re.compile(r"^[0-9a-f]{32}$")
MARKER = ".provision_id"


class ProvisionError(Exception):
    """Operasi provisioning gagal."""


class OwnershipError(ProvisionError):
    """Kepemilikan resource tidak terbukti — resource TIDAK disentuh."""


@dataclass(frozen=True)
class TerminalSpec:
    code: str
    port: int
    provision_id: str | None
    rpc_mode: str = "classic"


def validate_spec(cfg, spec: TerminalSpec, need_pid: bool) -> None:
    if not CODE_RE.fullmatch(spec.code or ""):
        raise ProvisionError("kode customer tidak sah")
    base = int(cfg.rpyc_port_base)
    if not (isinstance(spec.port, int) and
            base <= spec.port < base + int(cfg.max_customers)):
        raise ProvisionError("port di luar jendela alokasi")
    if need_pid and not PID_RE.fullmatch(spec.provision_id or ""):
        raise OwnershipError("provision_id tidak sah / tidak ada")
    if spec.rpc_mode not in ("classic", "ro"):
        raise ProvisionError("rpc_mode tidak sah")


class MockProvisioner:
    """Provisioner in-process utk dev/test (tanpa Wine/systemd).

    Knob uji: delays{op: detik}, fail{op: True}, running_override
    (False = terminal tidak pernah hidup).
    """

    def __init__(self, cfg):
        self._cfg = cfg
        self.delays: dict[str, float] = {}
        self.fail: dict[str, bool] = {}
        self.running_override: bool | None = None
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def _base(self, code: str) -> Path:
        return Path(self._cfg.term_base_dir) / code

    def _step(self, op: str, spec: TerminalSpec) -> None:
        with self._lock:
            self.calls.append((op, spec.code))
        if self.delays.get(op):
            time.sleep(self.delays[op])
        if self.fail.get(op):
            raise ProvisionError(f"mock: {op} gagal")

    def install(self, spec: TerminalSpec) -> None:
        validate_spec(self._cfg, spec, need_pid=True)
        base = self._base(spec.code)
        marker = base / MARKER
        if marker.exists() and marker.read_text() != spec.provision_id:
            raise OwnershipError("direktori dimiliki provisioning lain")
        if base.exists() and not marker.exists() and any(base.iterdir()):
            raise OwnershipError("direktori tanpa bukti kepemilikan")
        base.mkdir(parents=True, exist_ok=True)
        marker.write_text(spec.provision_id)   # penanda dulu (paritas helper)
        self._step("install", spec)

    def start(self, spec: TerminalSpec) -> None:
        validate_spec(self._cfg, spec, need_pid=False)
        self._step("start", spec)
        if not (self._base(spec.code) / MARKER).exists():
            raise OwnershipError("terminal belum di-install")
        with self._lock:
            self._running.add(spec.code)

    def stop(self, spec: TerminalSpec) -> None:
        self._step("stop", spec)
        with self._lock:
            self._running.discard(spec.code)

    def remove(self, spec: TerminalSpec) -> None:
        validate_spec(self._cfg, spec, need_pid=True)
        self._step("remove", spec)
        base = self._base(spec.code)
        marker = base / MARKER
        if not base.exists():
            return
        if not marker.exists() or marker.read_text() != spec.provision_id:
            raise OwnershipError("kepemilikan tidak terbukti")
        self.stop(spec)
        shutil.rmtree(base)

    def status(self, spec: TerminalSpec) -> dict:
        if self.running_override is not None:
            return {"running": bool(self.running_override)}
        with self._lock:
            return {"running": spec.code in self._running}


class HelperProvisioner:
    """Memanggil helper privileged via sudo (produksi)."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._helper = cfg.provisioner_helper
        self._sudo = cfg.provisioner_sudo

    def _call(self, op: str, spec: TerminalSpec, need_pid: bool,
              with_port: bool = True) -> dict:
        validate_spec(self._cfg, spec, need_pid=need_pid)
        argv = [self._sudo, "-n", self._helper, op, "--code", spec.code]
        if with_port:
            argv += ["--port", str(int(spec.port))]
        if need_pid:
            argv += ["--provision-id", spec.provision_id]
        if op == "install":
            argv += ["--rpc-mode", spec.rpc_mode]
        try:
            r = subprocess.run(argv, shell=False, capture_output=True,
                               text=True, timeout=self._cfg.
                               provisioner_timeout_seconds,
                               env={"PATH": "/usr/bin:/bin"}, check=False)
        except subprocess.TimeoutExpired as e:
            raise ProvisionError(f"helper {op} timeout") from e
        try:
            out = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {"ok": False, "error": "keluaran helper tidak sah"}
        if r.returncode == 3:
            raise OwnershipError(str(out.get("error")))
        if r.returncode != 0 or not out.get("ok"):
            raise ProvisionError(str(out.get("error") or f"helper {op} gagal"))
        return out

    def install(self, spec):
        self._call("install", spec, need_pid=True)

    def start(self, spec):
        self._call("start", spec, need_pid=False)

    def stop(self, spec):
        self._call("stop", spec, need_pid=False, with_port=False)

    def remove(self, spec):
        self._call("remove", spec, need_pid=True, with_port=False)

    def status(self, spec) -> dict:
        return self._call("status", spec, need_pid=False)


def build_provisioner(cfg):
    if getattr(cfg, "terminal_provision_mode", "mock") == "real":
        return HelperProvisioner(cfg)
    return MockProvisioner(cfg)
