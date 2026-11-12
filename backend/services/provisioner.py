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

Remediasi F2 — timeout TIDAK berarti helper berhenti:
- API (user non-root) TIDAK DAPAT membunuh helper root (EPERM), dan
  SIGKILL ke sudo tidak diteruskan ke helper (helper jadi yatim tanpa jalur
  sinyal). Karena itu saat timeout: SIGTERM ke sudo (sudo meneruskannya ke
  helper, yang berhenti sendiri dengan exit 5), tunggu grace, LALU periksa
  keadaan nyata lewat `inspect` (flock per customer = helper masih hidup).
- Helper juga punya batas waktu sendiri (op_timeout_seconds) yang lebih
  pendek dari PROVISIONER_TIMEOUT_SECONDS.
- Hasil yang tidak pasti -> ProvisionTimeout(uncertain=True): pemanggil
  WAJIB menahan resource & slot (fail-closed), tidak menghapus baris DB.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

CODE_RE = re.compile(r"^C[0-9]{3,9}$")
PID_RE = re.compile(r"^[0-9a-f]{32}$")
MARKER = ".provision_id"
COMPLETE = ".install_complete"
EXIT_ARGS, EXIT_OWNERSHIP, EXIT_FAIL = 2, 3, 4
EXIT_INTERRUPTED, EXIT_BUSY, EXIT_UNCLEAN = 5, 6, 7
KNOWN_EXITS = (0, EXIT_ARGS, EXIT_OWNERSHIP, EXIT_FAIL, EXIT_INTERRUPTED,
               EXIT_BUSY)
INTERNAL_ERROR = "kegagalan internal"     # helper main(): pengecualian tak terduga


class ProvisionError(Exception):
    """Operasi provisioning gagal."""


class OwnershipError(ProvisionError):
    """Kepemilikan resource tidak terbukti — resource TIDAK disentuh."""


class ProvisionBusy(ProvisionError):
    """Operasi lain atas customer yang sama sedang berjalan (helper exit 6).
    Tidak ada yang diubah oleh panggilan ini."""


class ProvisionIncomplete(ProvisionError):
    """Helper berhenti sendiri (batas waktu/sinyal, exit 5): operasi TIDAK
    lengkap, helper sudah tidak berjalan."""


class ProvisionTimeout(ProvisionError):
    """API menyerah menunggu helper. `state` hasil pemeriksaan setelahnya:

    helper_running    — helper/anaknya MASIH memegang lock (tidak pasti)
    stopped_incomplete— helper berhenti; instalasi tidak lengkap
    stopped_complete  — helper berhenti; instalasi ternyata lengkap
    stopped_absent    — helper berhenti; tidak ada resource
    unknown           — keadaan tidak dapat dipastikan
    `uncertain` True untuk helper_running/unknown -> WAJIB fail-closed."""

    UNCERTAIN = ("helper_running", "unknown")

    def __init__(self, msg: str, state: str):
        super().__init__(f"{msg} [{state}]")
        self.state = state
        self.uncertain = state in self.UNCERTAIN


class ProvisionAbnormalExit(ProvisionTimeout):
    """S2: helper berakhir TIDAK NORMAL (mati karena sinyal, exit tak dikenal,
    atau 'kegagalan internal'). Keadaan ditentukan inspect, sama seperti
    timeout: helper_running/unknown -> fail-closed; stopped_* -> pemulihan
    aman (remove idempoten berbasis bukti). BUKAN sukses."""


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
        marker.write_text(spec.provision_id)   # klaim dulu (paritas helper)
        (base / COMPLETE).unlink(missing_ok=True)
        self._step("install", spec)
        (base / COMPLETE).write_text(spec.provision_id)   # selesai PALING AKHIR

    def start(self, spec: TerminalSpec) -> None:
        validate_spec(self._cfg, spec, need_pid=False)
        self._step("start", spec)
        base = self._base(spec.code)
        if not (base / MARKER).exists() or not (base / COMPLETE).exists():
            raise OwnershipError("terminal belum di-install lengkap")
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

    def inspect(self, spec: TerminalSpec) -> dict:
        base = self._base(spec.code)
        if not base.exists():
            phase = "absent"
        elif (base / COMPLETE).exists():
            phase = "complete"
        else:
            phase = "incomplete"
        return {"ok": True, "busy": False, "phase": phase}


class HelperProvisioner:
    """Memanggil helper privileged via sudo (produksi)."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._helper = cfg.provisioner_helper
        self._sudo = cfg.provisioner_sudo
        self._timeout = float(cfg.provisioner_timeout_seconds)
        self._grace = float(getattr(cfg, "provisioner_term_grace_seconds",
                                    10.0))
        self._inspect_timeout = float(getattr(
            cfg, "provisioner_inspect_timeout_seconds", 15.0))

    def _argv(self, op, spec, need_pid, with_port):
        argv = [self._sudo, "-n", self._helper, op, "--code", spec.code]
        if with_port:
            argv += ["--port", str(int(spec.port))]
        if need_pid:
            argv += ["--provision-id", spec.provision_id]
        if op == "install":
            argv += ["--rpc-mode", spec.rpc_mode]
        return argv

    def _spawn(self, argv, timeout):
        """-> (returncode, stdout), atau ("timeout", sudo_masih_hidup).

        Saat timeout: SIGTERM ke sudo (BUKAN SIGKILL — SIGKILL tidak dapat
        diteruskan sudo dan membuat helper root yatim), tunggu grace.
        Bila sudo masih hidup setelah grace -> None; proses dituai thread
        latar (tanpa zombie) dan keadaan diputuskan lewat inspect."""
        p = subprocess.Popen(argv, shell=False, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             env={"PATH": "/usr/bin:/bin"},
                             start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
            return p.returncode, out
        except subprocess.TimeoutExpired:
            pass
        try:
            os.kill(p.pid, signal.SIGTERM)       # sudo meneruskan ke helper
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.communicate(timeout=self._grace)
            return "timeout", False
        except subprocess.TimeoutExpired:
            threading.Thread(target=p.wait, daemon=True,
                             name="helper-reaper").start()
            return "timeout", True

    def _after_timeout(self, op: str, spec: TerminalSpec,
                       sudo_alive: bool):
        """Keadaan NYATA setelah API menyerah (fail-closed). Sudo yang masih
        hidup = helper masih hidup (sudo menunggu helper) -> helper_running,
        apa pun hasil inspect."""
        if sudo_alive:
            raise ProvisionTimeout(f"helper {op} timeout", "helper_running")
        raise ProvisionTimeout(f"helper {op} timeout",
                               self._inspect_state(spec))

    def _inspect_state(self, spec: TerminalSpec) -> str:
        state = "unknown"
        try:
            argv = [self._sudo, "-n", self._helper, "inspect", "--code",
                    spec.code]
            if spec.provision_id and PID_RE.fullmatch(spec.provision_id):
                argv += ["--provision-id", spec.provision_id]
            res = self._spawn(argv, self._inspect_timeout)
            if res[0] == 0:
                info = json.loads((res[1] or "").strip().splitlines()[-1])
                if info.get("ok") is True and isinstance(info.get("busy"),
                                                         bool):
                    phase = info.get("phase")
                    if info["busy"]:
                        state = "helper_running"
                    elif info.get("owner_match") is False and \
                            phase != "absent":
                        state = "unknown"     # bukan milik provision_id ini
                    elif phase == "complete":
                        state = "stopped_complete"
                    elif phase == "absent":
                        state = "stopped_absent"
                    elif phase in ("incomplete", "unclaimed", "orphan_unit",
                                   "removing"):
                        state = "stopped_incomplete"
        except (ValueError, IndexError, OSError):
            state = "unknown"
        return state

    def _call(self, op: str, spec: TerminalSpec, need_pid: bool,
              with_port: bool = True) -> dict:
        validate_spec(self._cfg, spec, need_pid=need_pid)
        rc, stdout = self._spawn(self._argv(op, spec, need_pid, with_port),
                                 self._timeout)
        if rc == "timeout":
            self._after_timeout(op, spec, stdout)      # selalu raise
        try:
            out = json.loads((stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {"ok": False, "error": "keluaran helper tidak sah"}
        if rc == EXIT_UNCLEAN:
            # helper: turunan operasi MUNGKIN MASIH HIDUP -> selalu tidak
            # pasti (fail-closed), apa pun hasil inspect
            raise ProvisionAbnormalExit(
                f"helper {op}: proses turunan masih hidup (exit 7)",
                "helper_running")
        if rc not in KNOWN_EXITS or (rc == EXIT_FAIL and
                                     out.get("error") == INTERNAL_ERROR):
            # S2: mati karena sinyal (rc<0), exit tak dikenal (sudo dll.),
            # atau pengecualian internal -> keadaan NYATA lewat inspect.
            raise ProvisionAbnormalExit(
                f"helper {op} berakhir tidak normal (rc={rc})",
                self._inspect_state(spec))
        if rc == EXIT_OWNERSHIP:
            raise OwnershipError(str(out.get("error")))
        if rc == EXIT_BUSY:
            raise ProvisionBusy(str(out.get("error") or "helper sibuk"))
        if rc == EXIT_INTERRUPTED:
            raise ProvisionIncomplete(str(out.get("error")
                                          or f"helper {op} dihentikan"))
        if rc != 0 or not out.get("ok"):
            raise ProvisionError(str(out.get("error") or f"helper {op} gagal"))
        return out

    def install(self, spec):
        return self._call("install", spec, need_pid=True)

    def start(self, spec):
        self._call("start", spec, need_pid=False)

    def stop(self, spec):
        self._call("stop", spec, need_pid=False, with_port=False)

    def remove(self, spec):
        self._call("remove", spec, need_pid=True, with_port=False)

    def inspect(self, spec) -> dict:
        """Read-only: busy (helper/anak memegang lock) + phase instalasi."""
        validate_spec(self._cfg, spec, need_pid=False)
        argv = [self._sudo, "-n", self._helper, "inspect", "--code",
                spec.code]
        if spec.provision_id and PID_RE.fullmatch(spec.provision_id):
            argv += ["--provision-id", spec.provision_id]
        res = self._spawn(argv, self._inspect_timeout)
        if res[0] == "timeout":
            raise ProvisionTimeout("helper inspect timeout", "unknown")
        try:
            out = json.loads((res[1] or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {}
        if res[0] != 0 or out.get("ok") is not True:
            raise ProvisionError(str(out.get("error") or "inspect gagal"))
        return out

    def status(self, spec) -> dict:
        out = self._call("status", spec, need_pid=False)
        if out.get("busy"):              # operasi lain berjalan: fail-closed
            out = dict(out, running=False)
        return out


def build_provisioner(cfg):
    if getattr(cfg, "terminal_provision_mode", "mock") == "real":
        return HelperProvisioner(cfg)
    return MockProvisioner(cfg)
