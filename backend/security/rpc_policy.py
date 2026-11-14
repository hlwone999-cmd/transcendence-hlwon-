"""Kebijakan RPC MT5 TUNGGAL (MT5 RPC Hardening, kelompok 1).

Modul MURNI: tanpa akses DB, jaringan, atau proses. Dipakai API, worker
migrasi, scheduler, sync, dan status (wiring di kelompok berikutnya), dan
DIPARITASKAN dengan helper privileged (provisioner/mt5_provision_helper.py)
lewat tests/test_rpc_policy_parity.py — helper adalah skrip stdlib mandiri
yang tidak boleh mengimpor modul backend, sehingga konstanta & algoritma
yang sama ditulis ulang di sana dan DIUJI identik.

Isi:
- parse_rpc_mode           : parsing KETAT (nilai tak dikenal -> error,
                             tidak pernah jatuh ke 'classic').
- owner tag & Capability   : identitas migrasi (fencing generation).
- check_terminal_usable    : apakah terminal boleh dipakai runtime.
- migration_readiness_problems : aturan readiness per baris migrasi
                             (termasuk FAILED_CREDENTIALS — koreksi #5).
- parse_broker_allow       : validasi daftar CIDR (bukan 'any' di produksi).
- secret_strength_problem / selfcheck_proof : bukti kecocokan secret API &
                             helper TANPA mengekspos secret (challenge-response).
- min_lease_seconds / drain_seconds : syarat lease vs batas waktu helper.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# ------------------------------------------------------------- konstanta
RPC_MODE_RO = "ro"
RPC_MODE_CLASSIC = "classic"
KNOWN_RPC_MODES = (RPC_MODE_CLASSIC, RPC_MODE_RO)
# Satu-satunya mode yang sah di produksi (PARITAS helper:
# HELPER_PRODUCTION_RPC_MODES).
PRODUCTION_RPC_MODES = (RPC_MODE_RO,)

ENFORCEMENT_OFF = "off"            # dev/mock (TERMINAL_PROVISION_MODE=mock)
ENFORCEMENT_MIGRATION = "migration"  # masa transisi: classic DITOLAK runtime
ENFORCEMENT_STRICT = "strict"      # production=true
ENFORCEMENTS = (ENFORCEMENT_OFF, ENFORCEMENT_MIGRATION, ENFORCEMENT_STRICT)

# State machine migrasi (urutan maju). Lihat spesifikasi revisi §2.
MIGRATION_FORWARD_STATES = (
    "claimed", "snapshot_before", "backed_up", "stopping", "stopped",
    "removing", "removed", "pid_recorded", "installed", "started",
    "probed", "verified", "committed", "synced", "done",
)
MIGRATION_FAILURE_STATES = ("cancelled", "quarantined", "blocked",
                            "failed_credentials")
MIGRATION_STATES = MIGRATION_FORWARD_STATES + MIGRATION_FAILURE_STATES
MIGRATION_FINAL_STATES = ("done", "cancelled", "failed_credentials")
# Koreksi #1: capability worker sah di COMMITTED **dan** SYNCED — sync
# verifikasi berjalan COMMITTED->SYNCED dan snapshot_after dibaca di SYNCED.
# Di state lain (termasuk DONE) capability TIDAK sah.
CAPABILITY_STATES = ("committed", "synced")
# CANCELLED hanya sah sebelum resource lama dihapus (remove mulai).
CANCELLABLE_STATES = ("claimed", "snapshot_before", "backed_up",
                      "stopping", "stopped")

OWNER_MIGRATE_PREFIX = "migrate:"
_MID_RE = re.compile(r"^[0-9a-f]{32}$")
_OWNER_RE = re.compile(r"^migrate:([0-9a-f]{32}):([1-9][0-9]{0,17})$")

# Challenge-response secret (PARITAS helper: SELFCHECK_DOMAIN).
SELFCHECK_DOMAIN = "transcendence-selfcheck-v2|"
SECRET_MIN_BYTES = 32
# Placeholder yang DILARANG dipakai sebagai secret (mis. dari .env.example).
SECRET_PLACEHOLDERS = frozenset({
    "", "changeme", "change-me", "secret", "<openssl rand -hex 32>",
    "<random>", "xxx", "todo",
})


class PolicyViolation(Exception):
    """Terminal/konfigurasi melanggar kebijakan RPC. `reason` = kode
    mesin-terbaca (tanpa rahasia), dipakai status API & log."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}{': ' + detail if detail else ''}")
        self.reason = reason
        self.detail = detail


# ------------------------------------------------------------- rpc mode
def parse_rpc_mode(raw: Any) -> str:
    """Parsing KETAT nilai mode RPC (env MT5_RPC_MODE_DEFAULT).

    - None / string kosong (setelah strip spasi) -> 'ro' (default aman).
    - 'ro' / 'classic' persis (case-sensitive) -> nilai itu.
    - Lainnya ('RO', 'rO', 'xyz', ...) -> ValueError. TIDAK PERNAH jatuh
      diam-diam ke 'classic' (perilaku lama config.py:256-258)."""
    if raw is None:
        return RPC_MODE_RO
    if not isinstance(raw, str):
        raise ValueError("mode RPC harus string")
    v = raw.strip()
    if v == "":
        return RPC_MODE_RO
    if v not in KNOWN_RPC_MODES:
        raise ValueError(f"mode RPC tidak dikenal: {v[:16]!r} "
                         "(hanya 'ro' atau 'classic')")
    return v


def parse_enforcement(raw: Any) -> str:
    if not isinstance(raw, str) or raw.strip() not in ENFORCEMENTS:
        raise ValueError("enforcement RPC harus off|migration|strict")
    return raw.strip()


# ------------------------------------------------------- owner/capability
def make_owner_tag(migration_id: str, generation: int) -> str:
    if not _MID_RE.fullmatch(str(migration_id or "")):
        raise ValueError("migration_id tidak sah")
    if isinstance(generation, bool) or not isinstance(generation, int) \
            or generation < 1:
        raise ValueError("generation harus int >= 1")
    return f"{OWNER_MIGRATE_PREFIX}{migration_id}:{generation}"


def parse_owner_tag(tag: Any) -> tuple[str, int] | None:
    """('migrate:<id>:<gen>') -> (id, gen); format lain -> None."""
    if not isinstance(tag, str):
        return None
    m = _OWNER_RE.fullmatch(tag)
    if m is None:
        return None
    return m.group(1), int(m.group(2))


def is_migration_owner(tag: Any) -> bool:
    return isinstance(tag, str) and tag.startswith(OWNER_MIGRATE_PREFIX)


@dataclass(frozen=True)
class Capability:
    """Kapabilitas worker migrasi untuk memakai terminal yang SEDANG
    dimigrasi (hanya sync verifikasi di COMMITTED/SYNCED). Scheduler & API
    tidak pernah membawanya. Terikat ke migrasi, generation (fencing),
    terminal, DAN akun — capability basi (gen lama) tidak sah."""
    migration_id: str
    generation: int
    terminal_id: int
    account_id: int


def _get(row: Mapping | None, key: str):
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def check_terminal_usable(terminal: Mapping | None,
                          migration: Mapping | None, *,
                          enforcement: str,
                          capability: Capability | None = None) -> None:
    """Raise PolicyViolation bila terminal TIDAK boleh dipakai runtime
    (sync, status live, verifikasi di luar job aktivasi).

    [migration] = baris mt5_rpc_migrations NON-FINAL untuk terminal ini
    (atau None). Aturan (spesifikasi revisi §1 + K4 + koreksi #1):
    - mode tidak dikenal/NULL -> rpc_mode_invalid (semua enforcement).
    - classic: enforcement 'migration'/'strict' -> classic_blocked.
      'off' (dev/mock) -> diizinkan (perilaku lama, hanya non-produksi).
    - op_owner 'migrate:*' ATAU baris migrasi non-final -> diblokir,
      KECUALI capability cocok penuh (id, gen, terminal, akun, owner tag
      sama dengan op_owner, state migrasi ∈ CAPABILITY_STATES).
    - capability diberikan padahal tidak ada migrasi -> capability_unexpected.
    op_owner lain ('job:', 'remove:', 'removal-pending') BUKAN wewenang
    modul ini — ditangani logika provisioning yang sudah ada."""
    if enforcement not in ENFORCEMENTS:
        raise PolicyViolation("enforcement_invalid")
    if terminal is None:
        raise PolicyViolation("terminal_missing")
    mode = _get(terminal, "rpc_mode")
    if mode not in KNOWN_RPC_MODES:
        raise PolicyViolation("rpc_mode_invalid")
    owner = _get(terminal, "op_owner")
    if migration is not None or is_migration_owner(owner):
        if migration is None:
            raise PolicyViolation("migration_owner_orphan")
        mstate = _get(migration, "state")
        if mstate in MIGRATION_FINAL_STATES:
            # pemanggil wajib hanya mengirim baris NON-final
            raise PolicyViolation("migration_row_final_passed")
        if capability is None:
            raise PolicyViolation("migration_in_progress",
                                  str(mstate or "?"))
        _check_capability(terminal, migration, capability)
        return
    if capability is not None:
        raise PolicyViolation("capability_unexpected")
    if mode == RPC_MODE_CLASSIC and enforcement != ENFORCEMENT_OFF:
        raise PolicyViolation("classic_blocked")


def _check_capability(terminal: Mapping, migration: Mapping,
                      cap: Capability) -> None:
    try:
        tag = make_owner_tag(cap.migration_id, cap.generation)
    except ValueError:
        raise PolicyViolation("capability_invalid") from None
    checks = (
        _get(migration, "id") == cap.migration_id,
        _get(migration, "generation") == cap.generation,
        _get(migration, "terminal_id") == cap.terminal_id,
        _get(terminal, "id") == cap.terminal_id,
        _get(migration, "account_id") == cap.account_id,
        _get(terminal, "op_owner") == tag,
        _get(migration, "owner_tag") == tag,
        _get(migration, "state") in CAPABILITY_STATES,
    )
    if not all(checks):
        raise PolicyViolation("capability_mismatch")
    # Sync verifikasi hanya untuk identitas yang SUDAH di-commit ke ro.
    if _get(terminal, "rpc_mode") != RPC_MODE_RO or \
            _get(terminal, "provision_id") != _get(migration,
                                                   "new_provision_id"):
        raise PolicyViolation("capability_identity_mismatch")


# ------------------------------------------------------------ readiness
def migration_readiness_problems(migration: Mapping,
                                 terminal: Mapping | None,
                                 account: Mapping | None) -> list[str]:
    """Masalah readiness untuk SATU baris migrasi (kosong = sehat).

    Koreksi #5 — FAILED_CREDENTIALS hanya sehat bila terminal SUDAH
    direkonsiliasi ke identitas ro yang baru, berhenti, tanpa pemegang,
    dan akun berstatus credentials_rejected. Worker WAJIB (kelompok 3)
    meng-commit identitas ro + menghentikan unit SEBELUM menandai
    failed_credentials, sehingga tidak ada classic & tidak ada mismatch
    marker/DB yang tersisa."""
    state = _get(migration, "state")
    mid = _get(migration, "id")
    gen = _get(migration, "generation")
    problems: list[str] = []
    if state not in MIGRATION_STATES:
        return ["migration_state_invalid"]
    if state not in MIGRATION_FINAL_STATES:
        return ["migration_open"]
    try:
        tag = make_owner_tag(mid, gen)
    except ValueError:
        return ["migration_identity_invalid"]
    owner = _get(terminal, "op_owner")
    if state == "cancelled":
        if owner == tag:
            problems.append("cancelled_owner_held")
        return problems
    if terminal is None:
        return ["terminal_missing"]
    new_pid = _get(migration, "new_provision_id")
    if _get(terminal, "rpc_mode") != RPC_MODE_RO:
        problems.append("terminal_not_ro")
    if not new_pid or _get(terminal, "provision_id") != new_pid:
        problems.append("terminal_pid_mismatch")
    if owner is not None:
        problems.append("terminal_owner_held")
    if state == "failed_credentials":
        if _get(terminal, "lifecycle") != "stopped":
            problems.append("failed_credentials_terminal_not_stopped")
        if _get(account, "state") != "credentials_rejected":
            problems.append("failed_credentials_account_not_rejected")
    return problems


def audit_inventory_problems(audit: Any) -> list[str]:
    """Masalah kelengkapan inventaris audit-all helper (kosong = lengkap).

    FAIL-CLOSED: readiness WAJIB merah bila hasil audit tidak ada, bukan
    dict, ok bukan True, audit_complete bukan True (termasuk tidak ada —
    helper lama), inventory_errors tidak ada / bukan list / tidak kosong,
    atau ada terminal yang gagal dibaca. Kode galat helper diteruskan
    apa adanya ("inventory_error:<sumber>:<jenis>")."""
    if not isinstance(audit, Mapping):
        return ["audit_missing"]
    problems: list[str] = []
    if audit.get("ok") is not True:
        problems.append("audit_not_ok")
    errors = audit.get("inventory_errors")
    if not isinstance(errors, list):
        problems.append("audit_inventory_errors_missing")
        errors = []
    if audit.get("audit_complete") is not True:
        problems.append("audit_incomplete")
    elif errors:
        problems.append("audit_complete_inconsistent")
    for e in errors:
        problems.append(f"inventory_error:{e}" if isinstance(e, str)
                        else "inventory_error:invalid")
    terminals = audit.get("terminals")
    if not isinstance(terminals, list):
        problems.append("audit_terminals_missing")
    elif any(not isinstance(t, Mapping) or t.get("error") for t in terminals):
        problems.append("audit_terminal_unreadable")
    return problems


# ---------------------------------------------------------- broker_allow
def parse_broker_allow(raw: Any, *, production: bool) -> list[str]:
    """Validasi nilai broker_allow helper (disisipkan ke unit systemd
    `IPAddressAllow=`). Return daftar jaringan ter-normalisasi.

    - Karakter kontrol / baris baru -> ValueError (injeksi unit).
    - 'any' -> ['any'] di non-produksi; ValueError di produksi.
    - Selain itu: token dipisah spasi, masing-masing ip_network (strict
      host-bits boleh: dinormalisasi), tanpa duplikat, tanpa hostname."""
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
    out: list[str] = []
    for t in toks:
        if t == "any":
            raise ValueError("'any' tidak boleh dicampur CIDR")
        try:
            net = ipaddress.ip_network(t, strict=False)
        except ValueError:
            raise ValueError(f"broker_allow bukan IP/CIDR: {t[:40]!r}") \
                from None
        s = str(net)
        if s not in out:
            out.append(s)
    return out


# -------------------------------------------------------------- secret
def _decode_secret_bytes(secret: str) -> bytes | None:
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


def secret_strength_problem(secret: Any, *,
                            forbid_equal_to: tuple = ()) -> str | None:
    """None bila secret layak; selain itu kode masalah (TANPA nilai).

    Wajib >= 32 byte entropi: hex (>= 64 karakter) atau base64url
    (>= 43 karakter, dekode >= 32 byte). Placeholder & nilai yang sama
    dengan secret lain (MT5_CRED_KEY / API_TOKEN) ditolak."""
    if not isinstance(secret, str):
        return "secret_missing"
    s = secret.strip()
    if s.lower() in SECRET_PLACEHOLDERS:
        return "secret_placeholder"
    for other in forbid_equal_to:
        if isinstance(other, str) and other.strip() and \
                hmac.compare_digest(s.encode(), other.strip().encode()):
            return "secret_reused"
    raw = _decode_secret_bytes(s)
    if raw is None:
        return "secret_encoding"
    if len(raw) < SECRET_MIN_BYTES:
        return "secret_too_short"
    if len(set(raw)) < 8:
        return "secret_low_entropy"
    return None


def new_nonce() -> str:
    return secrets.token_hex(32)


def selfcheck_proof(secret: str, nonce_hex: str) -> str:
    """HMAC-SHA256(secret, DOMAIN || nonce) — 256 bit, hex. Nonce WAJIB
    64 hex (32 byte acak, baru tiap pemeriksaan)."""
    if not isinstance(nonce_hex, str) or \
            not re.fullmatch(r"[0-9a-f]{64}", nonce_hex):
        raise ValueError("nonce harus 64 hex huruf kecil")
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret kosong")
    msg = (SELFCHECK_DOMAIN + nonce_hex).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_selfcheck_proof(secret: str, nonce_hex: str, proof: Any) -> bool:
    if not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}",
                                                      proof):
        return False
    try:
        want = selfcheck_proof(secret, nonce_hex)
    except ValueError:
        return False
    return hmac.compare_digest(want, proof)


# ------------------------------------------------------------- lease
def drain_seconds(op_timeout: float, lock_wait: float,
                  term_grace: float) -> float:
    """Lama maksimum menunggu helper generasi lama selesai (K1/T1)."""
    return float(op_timeout) + float(lock_wait) + float(term_grace) + 10.0


def min_lease_seconds(op_timeout: float, lock_wait: float,
                      term_grace: float, inspect_timeout: float) -> float:
    return 2.0 * (float(op_timeout) + float(lock_wait) + float(term_grace)
                  + float(inspect_timeout))


def lease_problem(lease_seconds: float, *, op_timeout: float,
                  lock_wait: float, term_grace: float,
                  inspect_timeout: float) -> str | None:
    need = min_lease_seconds(op_timeout, lock_wait, term_grace,
                             inspect_timeout)
    if float(lease_seconds) < need:
        return f"lease_too_short (butuh >= {need:.0f} s)"
    return None
