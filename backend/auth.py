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
- Rate limit: sliding-window per IP klien tepercaya + aksi; lockout per
              email setelah N login gagal — PERSISTEN di database
              (auth_throttle/auth_lockouts), atomik lintas worker dan
              bertahan setelah restart.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

import db as dbmod
from db import CAPACITY_LOCK_REGISTER, INTEGRITY_ERRORS

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


_EMAIL_RE = None


def normalize_email(email: str) -> str:
    """Validasi + normalisasi email (Tahap 1: validasi input & identitas).

    Hanya satu '@', tanpa spasi/karakter kontrol, local-part & domain
    dibatasi karakter aman, domain wajib ber-titik dan tiap label valid.
    """
    import re
    global _EMAIL_RE  # noqa: PLW0603
    if _EMAIL_RE is None:
        _EMAIL_RE = re.compile(
            r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
            r"[a-z]{2,63}$")
    e = (email or "").strip().lower()
    if not e or len(e) > _EMAIL_MAX or e.count("@") != 1:
        raise AuthError(400, "Email tidak valid")
    local, _, domain = e.partition("@")
    if not local or len(local) > _EMAIL_LOCAL or not domain or "." not in domain:
        raise AuthError(400, "Email tidak valid")
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise AuthError(400, "Email tidak valid")
    if not _EMAIL_RE.match(e):
        raise AuthError(400, "Email tidak valid")
    return e


def validate_display_name(name: str | None) -> str | None:
    """Nama tampilan opsional: <=80 karakter, tanpa karakter kontrol."""
    if name is None:
        return None
    n = str(name).strip()
    if not n:
        return None
    if len(n) > 80 or any(ord(ch) < 32 or ord(ch) == 127 for ch in n):
        raise AuthError(400, "Nama tampilan tidak valid")
    return n


def validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 8:
        raise AuthError(400, "Password minimal 8 karakter")
    if len(password) > 256:
        raise AuthError(400, "Password terlalu panjang")
    if any(ord(ch) < 32 for ch in password):
        raise AuthError(400, "Password mengandung karakter tidak valid")
    return password


# --------------------------------------------------------------- rate limit
# Remediasi T2/S3 + Tahap 5: rate-limit & lockout PERSISTEN di database
# (tabel auth_throttle / auth_lockouts, migrasi 003) — konsisten lintas
# worker/proses dan bertahan setelah restart. Operasi memakai UPSERT
# atomik (INSERT ... ON CONFLICT DO UPDATE ... RETURNING) sehingga dua
# worker bersamaan tidak bisa sama-sama lolos di hit terakhir. Kunci
# disimpan sebagai SHA-256 (tanpa IP/email/login mentah di DB).
def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _db(conn=None):
    if conn is not None:
        return conn
    from flask import current_app
    return dbmod.get_db(current_app.config["APP_CFG"])


class RateLimiter:
    """Sliding-window (aproksimasi dua jendela tetap) berbasis DB."""

    def hit(self, key: str, limit: int, window_seconds: int,
            conn=None) -> bool:
        """Catat 1 hit. Return False bila melebihi limit.

        Estimasi = hit jendela berjalan + hit jendela sebelumnya x sisa
        porsi jendela (sliding approximation) — tidak ada lonjakan 2x di
        batas jendela. Hit yang ditolak tetap dihitung (penyalahguna yang
        terus mencoba tidak pernah 'kembali bersih' selama terus mencoba).
        """
        c = _db(conn)
        now = time.time()
        w = max(1, int(window_seconds))
        b = int(now // w)
        kh = _key_hash(key)
        row = c.execute(
            "INSERT INTO auth_throttle (key_hash, window_s, bucket, hits)"
            " VALUES (?,?,?,1) ON CONFLICT (key_hash, window_s, bucket)"
            " DO UPDATE SET hits = auth_throttle.hits + 1 RETURNING hits",
            (kh, w, b)).fetchone()
        cur = int(row["hits"])
        prev = c.execute(
            "SELECT hits FROM auth_throttle WHERE key_hash=? AND window_s=?"
            " AND bucket=?", (kh, w, b - 1)).fetchone()
        c.execute("DELETE FROM auth_throttle WHERE key_hash=? AND window_s=?"
                  " AND bucket<?", (kh, w, b - 1))
        c.commit()
        prev_hits = int(prev["hits"]) if prev else 0
        est = cur + prev_hits * (1.0 - (now - b * w) / w)
        return est <= limit

    def reset(self, key: str, conn=None) -> None:
        c = _db(conn)
        c.execute("DELETE FROM auth_throttle WHERE key_hash=?",
                  (_key_hash(key),))
        c.commit()


class FailureLockout:
    """Lockout setelah N kegagalan dalam jendela [lockout_seconds],
    berbasis DB (persisten lintas worker & restart)."""

    def __init__(self, max_failures: int, lockout_seconds: int):
        self._max = max(1, int(max_failures))
        self._secs = max(1, int(lockout_seconds))

    def is_locked(self, key: str, conn=None) -> bool:
        row = _db(conn).execute(
            "SELECT locked_until FROM auth_lockouts WHERE key_hash=?",
            (_key_hash(key),)).fetchone()
        return bool(row and row["locked_until"] is not None
                    and float(row["locked_until"]) > time.time())

    def record_failure(self, key: str, conn=None) -> None:
        c = _db(conn)
        now = time.time()
        kh = _key_hash(key)
        row = c.execute(
            "INSERT INTO auth_lockouts (key_hash, failures, window_start,"
            " locked_until) VALUES (?,1,?,NULL)"
            " ON CONFLICT (key_hash) DO UPDATE SET"
            "  failures = CASE WHEN auth_lockouts.window_start < ?"
            "             THEN 1 ELSE auth_lockouts.failures + 1 END,"
            "  window_start = CASE WHEN auth_lockouts.window_start < ?"
            "             THEN ? ELSE auth_lockouts.window_start END"
            " RETURNING failures",
            (kh, now, now - self._secs, now - self._secs, now)).fetchone()
        if int(row["failures"]) >= self._max:
            c.execute("UPDATE auth_lockouts SET locked_until=?, failures=0,"
                      " window_start=? WHERE key_hash=?",
                      (now + self._secs, now, kh))
        c.commit()

    def clear(self, key: str, conn=None) -> None:
        c = _db(conn)
        c.execute("DELETE FROM auth_lockouts WHERE key_hash=?",
                  (_key_hash(key),))
        c.commit()


limiter = RateLimiter()


def _lockout_for(config) -> FailureLockout:
    return FailureLockout(getattr(config, "max_failed_logins", 5),
                          getattr(config, "lockout_seconds", 900))


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
def customer_code_for(customer_id: int) -> str:
    """Kode customer deterministik dari id (C001, C002, ..., C1234)."""
    return f"C{int(customer_id):03d}"


def register_customer(conn, config, email: str, password: str,
                      display_name: str | None = None, *,
                      enforce_portal_cap: bool = True) -> dict:
    """Buat customer + user (registrasi publik — keputusan produk #1).

    Keputusan produk 2026-11: registrasi publik TETAP AKTIF, tetapi
    registrasi TIDAK memberi terminal/slot MT5. Slot MT5 (max_customers,
    default 4) hanya dipakai saat bind/connect akun MT5 yang lolos
    verifikasi (alokasi terminal di services.terminals, ber-capacity
    guard) — registrasi massal tidak bisa menghabiskan 4 slot MT5.

    [max_registered_customers] (0 = tanpa batas) membatasi total akun
    portal bila admin menginginkan; cek-dan-tulis diserialisasi
    capacity_guard (TOCTOU tertutup). Kode customer diturunkan dari id
    (remediasi T1). Pelanggaran UNIQUE -> AuthError(409), bukan 500.
    """
    email = normalize_email(email)
    validate_password(password)
    display_name = validate_display_name(display_name)
    # Batas akun PORTAL (registrasi publik) — TERPISAH dari slot MT5.
    # Customer sintetis self-link (@mt5.link) tidak dihitung & tidak
    # dibatasi di sini (enforce_portal_cap=False): mereka sudah dibatasi
    # slot terminal MT5 + verifikasi live, sehingga banjir registrasi
    # portal tidak dapat menghalangi aktivasi MT5.
    cap = int(getattr(config, "max_registered_customers", 0) or 0) \
        if enforce_portal_cap else 0
    try:
        with dbmod.capacity_guard(conn, CAPACITY_LOCK_REGISTER):
            if cap > 0:
                n_all = conn.execute(
                    "SELECT COUNT(*) AS n FROM users WHERE email NOT LIKE ?",
                    ("%@mt5.link",)).fetchone()["n"]
                if int(n_all) >= cap:
                    raise AuthError(403, "Pendaftaran akun sedang penuh — "
                                         "hubungi admin")
            if conn.execute("SELECT 1 FROM users WHERE email=?",
                            (email,)).fetchone() is not None:
                raise AuthError(409, "Email sudah terdaftar")
            # Remediasi T1: kode customer diturunkan dari id baris (PG
            # BIGSERIAL / SQLite AUTOINCREMENT: monoton, tak dipakai
            # ulang) lewat placeholder acak — bukan COUNT(*)+1.
            placeholder = f"tmp-{secrets.token_hex(12)}"
            conn.execute("INSERT INTO customers (code, display_name)"
                         " VALUES (?,?)", (placeholder, display_name))
            row = conn.execute("SELECT id FROM customers WHERE code=?",
                               (placeholder,)).fetchone()
            customer_id = int(row["id"])
            code = customer_code_for(customer_id)
            conn.execute("UPDATE customers SET code=? WHERE id=?",
                         (code, customer_id))
            conn.execute(
                "INSERT INTO users (customer_id, email, password_hash)"
                " VALUES (?,?,?)",
                (customer_id, email, generate_password_hash(password)))
    except INTEGRITY_ERRORS as e:  # balapan email/kode antar thread/proses
        raise AuthError(409, "Registrasi bentrok — coba lagi") from e
    urow = conn.execute("SELECT id FROM users WHERE email=?",
                        (email,)).fetchone()
    return {"customer_id": customer_id, "user_id": int(urow["id"]),
            "code": code, "email": email}


# ------------------------------------------------------------------- login
def login(conn, config, email: str, password: str, ip: str | None) -> dict:
    email = normalize_email(email)
    lock = _lockout_for(config)
    if lock.is_locked(email, conn=conn):
        raise AuthError(429, "Terlalu banyak percobaan gagal — coba lagi "
                             "nanti")
    row = conn.execute(
        """SELECT u.id AS user_id, u.password_hash, u.customer_id,
                  c.status AS customer_status
           FROM users u JOIN customers c ON c.id = u.customer_id
           WHERE u.email=?""", (email,)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        lock.record_failure(email, conn=conn)
        raise AuthError(401, "Email atau password salah")
    if row["customer_status"] != "active":
        raise AuthError(403, "Customer dinonaktifkan")
    lock.clear(email, conn=conn)
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


def _valid_ip(value: str | None) -> str | None:
    import ipaddress
    v = (value or "").strip()
    if not v:
        return None
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        return None


def client_ip_from(remote_addr: str | None, headers, config) -> str | None:
    """IP klien untuk rate-limit/lockout/audit (remediasi T2).

    - Header IP klien HANYA dipercaya bila REMOTE_ADDR (peer TCP) adalah
      proxy tepercaya (config.trusted_proxies) — klien yang terhubung
      langsung tidak bisa memalsukan identitasnya lewat header.
    - X-Forwarded-For bawaan klien TIDAK pernah dipercaya: bila header
      yang dikonfigurasi adalah X-Forwarded-For, hanya entri PALING
      KANAN (ditambahkan proxy tepercaya) yang dipakai.
    - Nilai header yang bukan alamat IP valid diabaikan (fallback peer).
    """
    peer = _valid_ip(remote_addr)
    trusted = {(_valid_ip(p) or p) for p in
               (getattr(config, "trusted_proxies", ()) or ())}
    header = (getattr(config, "client_ip_header", "") or "").strip()
    if peer is not None and peer in trusted and header:
        raw = headers.get(header, "") if headers is not None else ""
        if header.lower() == "x-forwarded-for":
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            raw = parts[-1] if parts else ""
        ip = _valid_ip(raw)
        if ip is not None:
            return ip
    return peer


def current_ip() -> str | None:
    try:
        from flask import current_app, request
        cfg = current_app.config.get("APP_CFG")
        return client_ip_from(request.remote_addr, request.headers, cfg)
    except RuntimeError:
        return None


def register_rate_limit(key: str, limit: int, window: int,
                        conn=None) -> None:
    """Raise AuthError(429) bila melewati batas."""
    if not limiter.hit(key, limit, window, conn=conn):
        raise AuthError(429, "Terlalu banyak permintaan — coba lagi nanti")
