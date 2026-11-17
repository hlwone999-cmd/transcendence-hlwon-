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
MID_RE = re.compile(r"^[0-9a-f]{32}$")
NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
EXIT_ARGS, EXIT_OWNERSHIP, EXIT_FAIL = 2, 3, 4
EXIT_INTERRUPTED, EXIT_BUSY, EXIT_UNCLEAN = 5, 6, 7
# MT5 RPC Hardening: fence migrasi ditolak / kebijakan produksi dilanggar.
# Helper mengembalikan keduanya SEBELUM perubahan apa pun (pasti, bukan
# keadaan tidak pasti).
EXIT_FENCE, EXIT_POLICY = 8, 9
KNOWN_EXITS = (0, EXIT_ARGS, EXIT_OWNERSHIP, EXIT_FAIL, EXIT_INTERRUPTED,
               EXIT_BUSY, EXIT_FENCE, EXIT_POLICY)
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


class FenceError(ProvisionError):
    """Helper exit 8: fence migrasi menolak operasi (worker generasi lama,
    migrasi lain memegang terminal, atau fence belum di-advance). Tidak
    ada yang diubah oleh panggilan ini."""


class PolicyError(ProvisionError):
    """Helper exit 9: kebijakan produksi dilanggar (mis. classic di
    produksi, konfigurasi produksi tidak lengkap). Tidak ada yang diubah."""


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
    # CATATAN: default 'classic' DIPERTAHANKAN di kelompok 1 karena
    # api/routes.py (di luar cakupan kelompok ini) membuat TerminalSpec tanpa
    # rpc_mode. Peralihan default ke 'ro' dilakukan di kelompok 2 bersama
    # config.py & routes.py. Produksi tetap aman: helper production=true
    # menolak install/start classic (exit 9).
    rpc_mode: str = "classic"
    # MT5 RPC Hardening (opsional; dipakai worker migrasi kelompok 3):
    migration_id: str | None = None      # fence: id migrasi (32 hex)
    fence: int | None = None             # fence: generation (>= 1)
    expect_mode: str | None = None       # start: mode yang wajib tercatat


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
    if spec.expect_mode is not None and spec.expect_mode not in ("classic",
                                                                 "ro"):
        raise ProvisionError("expect_mode tidak sah")
    has_mid = spec.migration_id is not None
    if has_mid != (spec.fence is not None):
        raise ProvisionError("migration_id & fence wajib berpasangan")
    if has_mid and not MID_RE.fullmatch(spec.migration_id or ""):
        raise ProvisionError("migration_id tidak sah")
    if spec.fence is not None and (isinstance(spec.fence, bool) or
                                   not isinstance(spec.fence, int) or
                                   spec.fence < 1):
        raise ProvisionError("fence tidak sah")


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
        # paritas aturan fence helper (dev/test)
        self.fences: dict[str, dict] = {}

    def _base(self, code: str) -> Path:
        return Path(self._cfg.term_base_dir) / code

    def _step(self, op: str, spec: TerminalSpec) -> None:
        with self._lock:
            self.calls.append((op, spec.code))
        if self.delays.get(op):
            time.sleep(self.delays[op])
        if self.fail.get(op):
            raise ProvisionError(f"mock: {op} gagal")

    def _check_fence(self, spec: TerminalSpec) -> None:
        """Aturan check_fence helper (exit 8 -> FenceError)."""
        with self._lock:
            stored = self.fences.get(spec.code)
        if spec.migration_id is None:
            if stored is not None and not stored["released"]:
                raise FenceError("terminal sedang dipegang migrasi RPC")
            return
        if stored is None or stored["released"] or \
                stored["migration_id"] != spec.migration_id or \
                stored["gen"] != spec.fence:
            raise FenceError("fence migrasi tidak cocok")

    def _expect_pid(self, spec: TerminalSpec) -> None:
        if spec.provision_id and PID_RE.fullmatch(spec.provision_id):
            base = self._base(spec.code)
            marker = base / MARKER
            if base.exists() and (not marker.exists() or
                                  marker.read_text() != spec.provision_id):
                raise OwnershipError("marker instalasi tidak cocok")

    def install(self, spec: TerminalSpec) -> None:
        validate_spec(self._cfg, spec, need_pid=True)
        self._check_fence(spec)
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
        self._check_fence(spec)
        self._expect_pid(spec)
        self._step("start", spec)
        base = self._base(spec.code)
        if not (base / MARKER).exists() or not (base / COMPLETE).exists():
            raise OwnershipError("terminal belum di-install lengkap")
        with self._lock:
            self._running.add(spec.code)

    def stop(self, spec: TerminalSpec) -> None:
        self._check_fence(spec)
        self._expect_pid(spec)
        self._step("stop", spec)
        with self._lock:
            self._running.discard(spec.code)

    def remove(self, spec: TerminalSpec) -> None:
        validate_spec(self._cfg, spec, need_pid=True)
        self._check_fence(spec)
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
        marker = base / MARKER
        with self._lock:
            f = self.fences.get(spec.code)
        return {"ok": True, "busy": False, "phase": phase,
                "marker": marker.read_text() if marker.exists() else None,
                "fence": dict(f) if f else None}

    # ---- MT5 RPC Hardening (paritas helper) ----------------------------
    def fence_advance(self, spec: TerminalSpec) -> dict:
        validate_spec(self._cfg, spec, need_pid=False)
        if spec.migration_id is None:
            raise ProvisionError("fence_advance butuh migration_id/fence")
        with self._lock:
            st = self.fences.get(spec.code)
            if st is not None:
                same = st["migration_id"] == spec.migration_id
                if st["released"] and same:
                    raise FenceError("fence migrasi ini sudah dilepas")
                if not st["released"] and not same:
                    raise FenceError("migrasi lain masih memegang fence")
                if not st["released"] and spec.fence < st["gen"]:
                    raise FenceError("generasi lebih kecil")
            if st is None or st["released"] or spec.fence > st["gen"]:
                self.fences[spec.code] = {"migration_id": spec.migration_id,
                                          "gen": spec.fence,
                                          "released": False}
        return self.inspect(spec)

    def fence_release(self, spec: TerminalSpec) -> dict:
        validate_spec(self._cfg, spec, need_pid=False)
        with self._lock:
            st = self.fences.get(spec.code)
            if st is None or st["migration_id"] != spec.migration_id or \
                    st["gen"] != spec.fence:
                raise FenceError("fence bukan milik migrasi ini")
            st["released"] = True
            return {"ok": True, "fence": dict(st)}

    def selfcheck(self, nonce: str | None = None) -> dict:
        """Mock: tidak pernah produksi; bukti secret memakai MT5_RPC_SECRET
        API sendiri (paritas format helper)."""
        from security import rpc_policy
        secret = getattr(self._cfg, "mt5_rpc_secret", "") or ""
        out = {"ok": True, "production": False, "service_user_mode": "mock",
               "ro_service": True, "broker_allow_mode": "any",
               "wine_python_ok": False,
               "secret_ok": rpc_policy.secret_strength_problem(secret)
               is None,
               "op_timeout_seconds": 0.0, "lock_wait_seconds": 0.0,
               "lock_dir_ok": True, "unclean_count": 0,
               "production_problems": ["mock_provisioner"]}
        if nonce is not None:
            if not NONCE_RE.fullmatch(nonce):
                raise ProvisionError("nonce tidak sah")
            out["secret_proof"] = (rpc_policy.selfcheck_proof(secret, nonce)
                                   if secret else None)
        return out

    def audit_all(self) -> dict:
        root = Path(self._cfg.term_base_dir)
        terms = []
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if not CODE_RE.fullmatch(d.name):
                    continue
                spec = TerminalSpec(code=d.name,
                                    port=int(self._cfg.rpyc_port_base),
                                    provision_id=None)
                info = self.inspect(spec)
                terms.append({"code": d.name, "phase": info["phase"],
                              "marker": info["marker"],
                              "fence": info["fence"]})
        return {"ok": True, "production": False, "terminals": terms,
                "unknown_entries": [], "user_orphans": [], "listeners": []}


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
        # Hardening: identitas instalasi diverifikasi helper SEBELUM
        # start/stop/status (marker == provision_id spec) — bila spec
        # membawa provision_id yang sah.
        if op in ("start", "stop", "status") and spec.provision_id and \
                PID_RE.fullmatch(spec.provision_id):
            argv += ["--expect-pid", spec.provision_id]
        if op == "start" and spec.expect_mode is not None:
            argv += ["--expect-mode", spec.expect_mode]
        if op in ("install", "start", "stop", "remove") and \
                spec.migration_id is not None:
            argv += ["--migration-id", spec.migration_id,
                     "--fence", str(int(spec.fence))]
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
        if rc == EXIT_FENCE:
            raise FenceError(str(out.get("error") or "fence ditolak"))
        if rc == EXIT_POLICY:
            raise PolicyError(str(out.get("error") or "kebijakan ditolak"))
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

    # ---- MT5 RPC Hardening ---------------------------------------------
    def _simple(self, argv: list, timeout: float, what: str) -> dict:
        """Panggil op helper tanpa resource (fence/selfcheck/audit)."""
        res = self._spawn(argv, timeout)
        if res[0] == "timeout":
            raise ProvisionTimeout(f"helper {what} timeout",
                                   "helper_running" if res[1] else "unknown")
        try:
            out = json.loads((res[1] or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            out = {"ok": False, "error": "keluaran helper tidak sah"}
        rc = res[0]
        if rc == EXIT_FENCE:
            raise FenceError(str(out.get("error") or "fence ditolak"))
        if rc == EXIT_POLICY:
            raise PolicyError(str(out.get("error") or "kebijakan ditolak"))
        if rc == EXIT_BUSY:
            raise ProvisionBusy(str(out.get("error") or "helper sibuk"))
        if rc == EXIT_OWNERSHIP:
            raise OwnershipError(str(out.get("error")))
        if rc not in KNOWN_EXITS or rc == EXIT_UNCLEAN:
            raise ProvisionAbnormalExit(
                f"helper {what} berakhir tidak normal (rc={rc})", "unknown")
        if rc != 0 or out.get("ok") is not True:
            raise ProvisionError(str(out.get("error") or f"{what} gagal"))
        return out

    def _fence_argv(self, op: str, spec: TerminalSpec) -> list:
        validate_spec(self._cfg, spec, need_pid=False)
        if spec.migration_id is None:
            raise ProvisionError(f"{op} butuh migration_id/fence")
        return [self._sudo, "-n", self._helper, op, "--code", spec.code,
                "--migration-id", spec.migration_id,
                "--gen", str(int(spec.fence))]

    def fence_advance(self, spec: TerminalSpec) -> dict:
        """K1: majukan fence DI BAWAH flock helper; mengembalikan inspect
        yang dibaca di dalam lock yang sama. Timeout pemanggilan memakai
        batas helper normal (helper menunggu operasi lama selesai)."""
        return self._simple(self._fence_argv("fence-advance", spec),
                            self._timeout, "fence-advance")

    def fence_release(self, spec: TerminalSpec) -> dict:
        return self._simple(self._fence_argv("fence-release", spec),
                            self._timeout, "fence-release")

    def selfcheck(self, nonce: str | None = None) -> dict:
        argv = [self._sudo, "-n", self._helper, "selfcheck"]
        if nonce is not None:
            if not NONCE_RE.fullmatch(nonce):
                raise ProvisionError("nonce tidak sah")
            argv += ["--nonce", nonce]
        return self._simple(argv, self._inspect_timeout, "selfcheck")

    def audit_all(self) -> dict:
        return self._simple([self._sudo, "-n", self._helper, "audit-all"],
                            self._timeout, "audit-all")


def build_provisioner(cfg):
    if getattr(cfg, "terminal_provision_mode", "mock") == "real":
        return HelperProvisioner(cfg)
    return MockProvisioner(cfg)
