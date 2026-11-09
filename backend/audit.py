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


def audit(conn, action: str, *, customer_id: int | None = None,
          user_id: int | None = None, entity: str | None = None,
          detail: dict | None = None) -> None:
    """Tulis satu baris audit. Aman dipanggil dari mana saja."""
    try:
        ip, ua = _request_meta()
        raw = json.dumps(_scrub(detail or {}), ensure_ascii=False,
                         default=str)
        if len(raw) > _MAX_DETAIL_JSON:
            raw = raw[:_MAX_DETAIL_JSON]
        conn.execute(
            """INSERT INTO audit_logs (customer_id, user_id, action, entity,
                 ip, user_agent, detail, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (customer_id, user_id, action, entity, ip, ua, raw, _now_iso()),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — audit jangan crash-kan request
        log.exception("gagal menulis audit_logs (action=%s)", action)
