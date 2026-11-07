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
        ip = (request.headers.get("X-Forwarded-For", "")
              or request.remote_addr or "").split(",")[0].strip()[:64] or None
        ua = (request.headers.get("User-Agent") or "")[:180] or None
        return ip, ua
    except RuntimeError:  # di luar konteks request (scheduler/tools)
        return None, None


def audit(conn, action: str, *, customer_id: int | None = None,
          user_id: int | None = None, entity: str | None = None,
          detail: dict | None = None) -> None:
    """Tulis satu baris audit. Aman dipanggil dari mana saja."""
    try:
        ip, ua = _request_meta()
        raw = json.dumps(detail or {}, ensure_ascii=False, default=str)
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
