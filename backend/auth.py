"""Autentikasi & otorisasi multi-customer (brief §7).

- Password  : werkzeug pbkdf2:sha256 (default 600.000 iterasi, salt acak).
- Token     : secrets.token_urlsafe(32) — server HANYA simpan SHA-256-nya
              (tabel auth_tokens) + expires_at + revoked_at.
- Transport : header 'Authorization: Bearer <t>' ATAU 'X-API-Token: <t>'
              (nama header lama dipertahankan agar klien Flutter yang
              sudah ada tetap kompatibel).
- Otorisasi : require_customer menurunkan customer context dari token
              yang TERVERIFIKASI SERVER — customer_id/account_id dari
              URL/query/body tidak pernah dipercaya (brief §6).
- Rate limit: sliding-window in-memory per IP+aksi; lockout per email
              setelah N login gagal (MVP single-process; untuk multi-
              replica: pindah ke Redis — dicatat di issue terbuka).
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

_EMAIL_MAX = 254
_EMAIL_LOCAL = 64


class AuthError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def normalize_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not e or "@" not in e or len(e) > _EMAIL_MAX:
        raise AuthError(400, "Email tidak valid")
    local, _, domain = e.partition("@")
    if not local or len(local) > _EMAIL_LOCAL or not domain or "." not in domain:
        raise AuthError(400, "Email tidak valid")
    return e


def validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 8:
        raise AuthError(400, "Password minimal 8 karakter")
    if len(password) > 256:
        raise AuthError(400, "Password terlalu panjang")
    return password


# --------------------------------------------------------------- rate limit
class RateLimiter:
    """Sliding-window in-memory (thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Catatan 1 hit. Return False bila melebihi limit."""
        now = time.monotonic()
        with self._lock:
            bucket = [t for t in self._hits.get(key, [])
                      if now - t < window_seconds]
            if len(bucket) >= limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class FailureLockout:
    """Lockout kumulatif per kunci (email) setelah N kegagalan."""

    def __init__(self, max_failures: int, lockout_seconds: int):
        self._max = max_failures
        self._secs = lockout_seconds
        self._lock = threading.Lock()
        self._fails: dict[str, tuple[int, float]] = {}

    def is_locked(self, key: str) -> bool:
        with self._lock:
            rec = self._fails.get(key)
            if not rec:
                return False
            count, until = rec
            if until <= 0:
                return False
            if time.monotonic() > until:
                del self._fails[key]
                return False
            return True

    def record_failure(self, key: str) -> None:
        with self._lock:
            count, _ = self._fails.get(key, (0, 0.0))
            count += 1
            until = time.monotonic() + self._secs if count >= self._max else 0.0
            self._fails[key] = (count, until)

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


limiter = RateLimiter()


def _lockout_for(config) -> FailureLockout:
    # instance per konfigurasi (cache sederhana berdasar parameter)
    global _lockout_cache  # noqa: PLW0603
    key = (getattr(config, "max_failed_logins", 5),
           getattr(config, "lockout_seconds", 900))
    inst = _lockout_cache.get(key)
    if inst is None:
        inst = FailureLockout(*key)
        _lockout_cache[key] = inst
    return inst


_lockout_cache: dict[tuple[int, int], FailureLockout] = {}


# ------------------------------------------------------------------ token
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(conn, config, *, user_id: int, customer_id: int,
                ip: str | None) -> str:
    """Buat token sesi baru; simpan HANYA hash-nya. Return token asli."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc)
               + timedelta(hours=config.auth_token_ttl_hours))
    conn.execute(
        """INSERT INTO auth_tokens (user_id, customer_id, token_hash,
             expires_at, created_ip, created_at)
           VALUES (?,?,?,?,?,?)""",
        (user_id, customer_id, _hash_token(token),
         expires.strftime("%Y-%m-%dT%H:%M:%SZ"), ip, _now_iso()),
    )
    conn.commit()
    return token


def resolve_token(conn, token: str) -> dict | None:
    """Verifikasi token -> {customer_id, user_id, token_id} atau None.

    Menolak token tak dikenal, kedaluwarsa, atau yang sudah di-revoke.
    """
    if not token or len(token) > 512:
        return None
    row = conn.execute(
        """SELECT t.id AS token_id, t.customer_id, t.user_id, t.expires_at,
                  t.revoked_at, c.status AS customer_status
           FROM auth_tokens t JOIN customers c ON c.id = t.customer_id
           WHERE t.token_hash = ?""", (_hash_token(token),)).fetchone()
    if row is None:
        return None
    if row["revoked_at"] is not None:
        return None
    try:
        if _parse_iso(row["expires_at"]) < datetime.now(timezone.utc):
            return None
    except ValueError:
        return None
    if row["customer_status"] != "active":
        return None
    return {"customer_id": int(row["customer_id"]),
            "user_id": int(row["user_id"]),
            "token_id": int(row["token_id"])}


def revoke_token(conn, token_id: int) -> None:
    conn.execute(
        "UPDATE auth_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (_now_iso(), token_id))
    conn.commit()


def revoke_all_for_customer(conn, customer_id: int) -> None:
    """Cabut semua sesi customer (mis. saat unbind akun MT5)."""
    conn.execute(
        "UPDATE auth_tokens SET revoked_at=? WHERE customer_id=?"
        " AND revoked_at IS NULL", (_now_iso(), customer_id))
    conn.commit()


# ---------------------------------------------------------------- register
def register_customer(conn, config, email: str, password: str,
                      display_name: str | None = None) -> dict:
    """Buat customer + user. Batas kapasitas MVP: max_customers aktif."""
    email = normalize_email(email)
    validate_password(password)
    n_active = conn.execute(
        "SELECT COUNT(*) AS n FROM customers WHERE status='active'"
    ).fetchone()["n"]
    if int(n_active) >= config.max_customers:
        raise AuthError(403, f"Kapasitas maksimal {config.max_customers} "
                             "customer aktif sudah tercapai")
    if conn.execute("SELECT 1 FROM users WHERE email=?",
                    (email,)).fetchone() is not None:
        raise AuthError(409, "Email sudah terdaftar")
    n_all = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    code = f"C{int(n_all) + 1:03d}"
    conn.execute("INSERT INTO customers (code, display_name) VALUES (?,?)",
                 (code, (display_name or "").strip()[:80] or None))
    row = conn.execute("SELECT id FROM customers WHERE code=?",
                       (code,)).fetchone()
    customer_id = int(row["id"])
    conn.execute(
        "INSERT INTO users (customer_id, email, password_hash) VALUES (?,?,?)",
        (customer_id, email, generate_password_hash(password)))
    urow = conn.execute("SELECT id FROM users WHERE email=?",
                        (email,)).fetchone()
    return {"customer_id": customer_id, "user_id": int(urow["id"]),
            "code": code, "email": email}


# ------------------------------------------------------------------- login
def login(conn, config, email: str, password: str, ip: str | None) -> dict:
    email = normalize_email(email)
    lock = _lockout_for(config)
    if lock.is_locked(email):
        raise AuthError(429, "Terlalu banyak percobaan gagal — coba lagi "
                             "nanti")
    row = conn.execute(
        """SELECT u.id AS user_id, u.password_hash, u.customer_id,
                  c.status AS customer_status
           FROM users u JOIN customers c ON c.id = u.customer_id
           WHERE u.email=?""", (email,)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        lock.record_failure(email)
        raise AuthError(401, "Email atau password salah")
    if row["customer_status"] != "active":
        raise AuthError(403, "Customer dinonaktifkan")
    lock.clear(email)
    token = issue_token(conn, config, user_id=int(row["user_id"]),
                        customer_id=int(row["customer_id"]), ip=ip)
    conn.execute("UPDATE users SET last_login_at=? WHERE id=?",
                 (_now_iso(), row["user_id"]))
    conn.commit()
    return {"token": token, "customer_id": int(row["customer_id"]),
            "user_id": int(row["user_id"])}


# ------------------------------------------------------- Flask integration
def _extract_token() -> str:
    from flask import request
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-API-Token") or "").strip()


def current_ip() -> str | None:
    try:
        from flask import request
        fwd = request.headers.get("X-Forwarded-For", "")
        return (fwd.split(",")[0].strip() or request.remote_addr or "")[:64] \
            or None
    except RuntimeError:
        return None


def register_rate_limit(key: str, limit: int, window: int) -> None:
    """Raise AuthError(429) bila melewati batas."""
    if not limiter.hit(key, limit, window):
        raise AuthError(429, "Terlalu banyak permintaan — coba lagi nanti")
