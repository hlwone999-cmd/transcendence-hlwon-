"""Audit log multi-customer (tabel audit_logs).

Prinsip (brief §7):
- Semua aksi penting tercatat: register, login, login_gagal, logout,
  bind, unbind, sync_manual, rate_limited, akses_ditolak.
- TIDAK PERNAH menyimpan password/token/kredensial di kolom detail.
- IP & user-agent diambil dari request bila konteks Flask tersedia.
- Kegagalan menulis audit TIDAK boleh mematikan request utama.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_MAX_DETAIL_JSON = 4000  # batasi ukuran detail


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request_meta() -> tuple[str | None, str | None]:
    try:
        from flask import request
        # Remediasi T2: IP audit memakai IP klien TEPERCAYA (proxy
        # terdaftar) — X-Forwarded-For bawaan klien tidak dipercaya.
        import auth as authmod
        ip = (authmod.current_ip() or "")[:64] or None
        ua = (request.headers.get("User-Agent") or "")[:180] or None
        return ip, ua
    except RuntimeError:  # di luar konteks request (scheduler/tools)
        return None, None


_SECRET_KEYS = ("password", "passwd", "token", "secret", "enc_password",
                "authorization", "api_key", "credential")


def _scrub(obj):
    """Buang nilai ber-kunci rahasia dari detail audit (defense-in-depth:
    pemanggil memang tidak boleh mengirim secret — Tahap 2)."""
    if isinstance(obj, dict):
        return {k: ("[disensor]" if any(s in str(k).lower()
                                       for s in _SECRET_KEYS) else _scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


class AuditWriteError(RuntimeError):
    """B12.1: audit WAJIB (required=True) gagal ditulis — operasi pemanggil
    harus gagal tertutup (tidak dijalankan / tidak dianggap sukses)."""


# B12.1: penghitung kegagalan tulis audit per aksi (monitoring; diekspos
# lewat /admin/health/terminals). Proses-lokal, thread-safe.
_FAIL_LOCK = threading.Lock()
_FAILURES: dict[str, int] = {}
FAIL_MARKER = "AUDIT_WRITE_FAILED"


def failure_stats() -> dict:
    with _FAIL_LOCK:
        return {"total": sum(_FAILURES.values()), "by_action": dict(_FAILURES)}


def _record_failure(action: str) -> None:
    with _FAIL_LOCK:
        _FAILURES[action] = _FAILURES.get(action, 0) + 1


def _scope_allows(conn, customer_id) -> bool:
    """Apakah policy RLS koneksi ini mengizinkan INSERT audit customer_id?
    (008: role aplikasi -> hanya customer context-nya; role sistem ->
    semua bila tanpa context, selain itu hanya customer context-nya)."""
    import db as dbmod
    if not dbmod.is_postgres(conn):
        return True
    ctx = conn.current_customer_context()
    if getattr(conn, "role_kind", "app") == "system" and ctx is None:
        return True
    return ctx is not None and customer_id is not None and \
        int(customer_id) == int(ctx)


def _writer(conn, customer_id):
    """Koneksi penulis: koneksi pemanggil bila scope-nya mengizinkan.
    Koneksi audit role sistem yang TERPISAH hanya untuk: (a) pemanggil
    yang memang koneksi role sistem (mis. pra-auth dengan customer
    context K1 / customer yang sudah dihapus), atau (b) audit tanpa
    customer (customer_id None, mis. kegagalan pra-auth). Koneksi role
    APLIKASI ber-scope A yang menulis audit customer B lain TIDAK
    dialihkan: RLS menolaknya dan kegagalan tercatat (bukan elevasi)."""
    if _scope_allows(conn, customer_id):
        return conn
    if getattr(conn, "role_kind", "app") != "system" and \
            customer_id is not None:
        return conn
    cfg = getattr(conn, "config", None)
    if cfg is None:
        return conn          # koneksi mentah (tool/tes): tanpa penulis lain
    import db as dbmod
    return dbmod.get_audit_db(cfg)


def audit(conn, action: str, *, customer_id: int | None = None,
          user_id: int | None = None, entity: str | None = None,
          detail: dict | None = None, required: bool = False) -> bool:
    """Tulis satu baris audit. Return True bila tersimpan.

    B12.1 — kebijakan:
    - Default (best-effort): kegagalan TIDAK mematikan operasi, tetapi
      TIDAK diam: log ERROR ber-penanda AUDIT_WRITE_FAILED + penghitung
      per aksi (failure_stats, diekspos ke admin health).
    - required=True: kegagalan -> AuditWriteError (pemanggil gagal
      tertutup). Dipakai untuk audit write-ahead aksi admin yang membuat
      identitas/akses baru (lihat routes.admin_create_customer).
    Commit koneksi pemanggil tetap dilakukan seperti sebelumnya."""
    target = None
    try:
        ip, ua = _request_meta()
        raw = json.dumps(_scrub(detail or {}), ensure_ascii=False,
                         default=str)
        if len(raw) > _MAX_DETAIL_JSON:
            raw = raw[:_MAX_DETAIL_JSON]
        target = _writer(conn, customer_id)
        target.execute(
            """INSERT INTO audit_logs (customer_id, user_id, action, entity,
                 ip, user_agent, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (customer_id, user_id, action, entity, ip, ua, raw, _now_iso()),
        )
        target.commit()
        if target is not conn:
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        # rollback HANYA koneksi tempat INSERT gagal (koneksi pemanggil
        # hanya bila memang itu penulisnya — transaksi PG yang gagal wajib
        # diakhiri); transaksi pemanggil tidak dibatalkan karena audit
        # terpisah gagal.
        c = target if target is not None else conn
        try:
            c.rollback()
        except Exception:  # noqa: BLE001
            pass
        _record_failure(action)
        log.error("%s action=%s customer_id=%s required=%s err=%s",
                  FAIL_MARKER, action, customer_id, required,
                  type(e).__name__, exc_info=True)
        if required:
            raise AuditWriteError(f"audit wajib '{action}' gagal ditulis") \
                from e
        return False
