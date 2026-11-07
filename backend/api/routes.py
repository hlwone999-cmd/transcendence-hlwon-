"""Blueprint API Jurnal MT5 multi-customer (url_prefix /api/v1).

AUTENTIKASI (brief §7): token sesi customer via 'Authorization: Bearer'
ATAU header lama 'X-API-Token' (kompatibel klien Flutter yang sudah ada).
Customer context SELALU diturunkan dari token yang diverifikasi server —
customer_id/account_id dari URL/query/body TIDAK PERNAH dipercaya (§6).

SEMANTIK FILTER TANGGAL (tidak berubah — lihat juga services/analytics.py
dan docs/DATE-TIMEZONE-RECONCILIATION.md):
- `from` : INKLUSIF. Format `YYYY-MM-DD` (00:00:00 UTC) atau ISO penuh.
- `to`   : default INKLUSIF; `YYYY-MM-DD` diartikan akhir hari itu.
- `to_exclusive` : `1/true/yes/on` → interval efektif [from, to).
- Semua perbandingan memakai UTC; konversi zona tampilan di klien.

Semua endpoint gagal-aman: kalau MT5/terminal tidak tersedia, tetap
balas JSON status disconnected — tidak pernah crash.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, abort, current_app, g, jsonify, request

import db as dbmod
from mt5client.provider import MT5Error
from services import analytics

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")
log = logging.getLogger(__name__)


# --------------------------------------------------------------- utilitas
def cfg():
    return current_app.config["APP_CFG"]


def get_db():
    import db as dbmod
    return dbmod.get_db(cfg())


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _admin_token_ok() -> bool:
    """Token bootstrap admin (env API_TOKEN) — hanya utk /admin/*."""
    expected = cfg().api_token
    got = request.headers.get("X-API-Token", "")
    return bool(expected) and hmac.compare_digest(got, expected)


# ------------------------------------------------------------ customer ctx
def require_customer(fn):
    """Wajib token sesi valid; set g.customer_id/g.user_id/g.token_id."""
    @wraps(fn)
    def wrapper(*a, **kw):
        import auth as authmod
        token = authmod._extract_token()
        info = authmod.resolve_token(get_db(), token) if token else None
        if info is None:
            abort(401, description="Token sesi tidak valid / kedaluwarsa / "
                                   "sudah logout")
        g.customer_id = info["customer_id"]
        g.user_id = info["user_id"]
        g.token_id = info["token_id"]
        # RLS defense-in-depth (audit #2): scope koneksi PostgreSQL ke
        # customer hasil verifikasi token (SET LOCAL per transaksi).
        # SQLite no-op. Bukan pengganti filter customer_id di query.
        import db as dbmod
        dbmod.set_customer_context(get_db(), g.customer_id)
        return fn(*a, **kw)
    return wrapper


def require_rate_limit(action: str, limit: int, window_seconds: int):
    """Dekorator rate-limit per IP+aksi (sliding window in-memory)."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            import auth as authmod
            ip = authmod.current_ip() or "?"
            try:
                authmod.register_rate_limit(f"{action}:{ip}", limit,
                                            window_seconds)
            except authmod.AuthError as e:
                abort(e.code, description=e.message)
            return fn(*a, **kw)
        return wrapper
    return deco


def _own_account(conn):
    """Akun MT5 milik customer pada g — SELALU dari DB, bukan request."""
    row = conn.execute(
        "SELECT * FROM mt5_accounts WHERE customer_id=?",
        (g.customer_id,)).fetchone()
    if row is None:
        abort(404, description="Belum ada akun MT5 ter-bind. "
                               "Bind dulu: POST /api/v1/accounts/bind")
    return row


# ------------------------------------------------------------- parse helper
def _parse_date(name: str, value: str, end_of_day: bool = False) -> str:
    v = (value or "").strip()
    if not v:
        abort(400, description=f"Parameter '{name}' tidak boleh kosong")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(v.replace("Z", ""), fmt.replace("Z", ""))
            if end_of_day and fmt == "%Y-%m-%d":
                dt = dt + timedelta(days=1) - timedelta(seconds=1)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    abort(400, description=f"Parameter '{name}' bukan tanggal valid: {v[:32]}")


def _parse_int(name: str, value, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        abort(400, description=f"Parameter '{name}' harus angka")
    if n < lo or n > hi:
        abort(400, description=f"Parameter '{name}' harus {lo}..{hi}")
    return n


def _parse_enum(name: str, value: str, allowed: tuple) -> str:
    if value not in allowed:
        abort(400, description=f"Parameter '{name}' harus salah satu: "
                               f"{', '.join(allowed)}")
    return value


def _bool_arg(name: str) -> bool:
    v = (request.args.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _json_filters(end_of_day_to: bool = True):
    """Parse `from`/`to`/`symbol`/`to_exclusive` dari query string."""
    args = request.args
    f_from = _parse_date("from", args.get("from", "2000-01-01")) \
        if "from" in args else None
    f_to = None
    if "to" in args:
        f_to = _parse_date("to", args["to"], end_of_day=end_of_day_to)
    if f_from and f_to and f_from > f_to:
        abort(400, description="Parameter 'from' tidak boleh setelah 'to'")
    symbol = (args.get("symbol") or "").strip()[:32] or None
    to_exclusive = _bool_arg("to_exclusive")
    return f_from, f_to, symbol, to_exclusive


def _audit(action: str, **kw):
    import audit as auditmod
    auditmod.audit(get_db(), action, customer_id=getattr(g, "customer_id",
                                                         None),
                   user_id=getattr(g, "user_id", None), **kw)


# ------------------------------------------------------------------ health
# Tabel minimum yang WAJIB ada (brief §5) supaya backend benar-benar
# siap melayani — bukan sekadar proses hidup.
CORE_TABLES = ("customers", "users", "auth_tokens", "mt5_accounts",
               "mt5_credentials", "mt5_terminals", "mt5_deals",
               "mt5_orders")


def _existing_tables(conn) -> set[str]:
    """Nama tabel yang benar-benar ada (dipakai health readiness)."""
    if dbmod.is_postgres(conn):
        rows = conn.execute(
            "SELECT tablename AS t FROM pg_tables"
            " WHERE schemaname='public'").fetchall()
    else:
        rows = conn.execute(
            "SELECT name AS t FROM sqlite_master WHERE type='table'"
        ).fetchall()
    return {str(r["t"]) for r in rows}


def _database_readiness(conn) -> dict:
    """Cek koneksi + skema minimum (audit round-3 #4).

    Return dict laporan; key 'ok' = readiness penuh. TIDAK pernah
    melempar exception — kegagalan dilaporkan sebagai status.
    """
    try:
        engine = "postgresql" if dbmod.is_postgres(conn) else "sqlite"
        conn.execute("SELECT 1")
        tables = _existing_tables(conn)
        missing = [t for t in CORE_TABLES if t not in tables]
        report = {"ok": not missing, "engine": engine}
        if missing:
            report["missing_tables"] = missing
        return report
    except Exception as e:  # noqa: BLE001 — health tidak boleh 500 mentah
        return {"ok": False, "engine": None,
                "error": f"{type(e).__name__}: {str(e)[:120]}"}


def _terminals_overview(conn) -> dict:
    """Ringkasan status terminal MT5 (audit round-3 #4).

    DILAPORKAN TERPISAH dan TIDAK pernah memengaruhi 200/503 health
    backend: backend tetap 'ok' walau MT5/terminal mati. Pada
    PostgreSQL tanpa scope customer hanya baris yang terlihat oleh
    role aplikasi yang dihitung (RLS default-deny); probe per customer
    tersedia di GET /api/v1/admin/health/terminals.
    """
    try:
        rows = conn.execute(
            "SELECT customer_id, status, rpyc_port, last_health_at"
            " FROM mt5_terminals ORDER BY customer_id").fetchall()
        return {"reported": len(rows),
                "probe_endpoint": "/api/v1/admin/health/terminals",
                "note": "tidak dipakai untuk penentuan health backend"}
    except Exception as e:  # noqa: BLE001
        return {"reported": None,
                "error": f"{type(e).__name__}: {str(e)[:120]}"}


@api_bp.get("/health")
def health():
    """Readiness backend: database + skema minimum (audit round-3 #4).

    - 200 : koneksi DB hidup DAN tabel inti ada.
    - 503 : DB tidak terjangkau / skema belum dimigrasi.
    Status terminal MT5 hanya DILAPORKAN (sub-objek `mt5_terminals`)
    dan tidak pernah menurunkan health backend — tidak ada kewajiban
    MT5 live untuk health (audit: jangan wajibkan MT5 untuk health).
    """
    # koneksi DB gagal pun harus tetap 503 terstruktur, bukan 500 mentah
    try:
        conn = get_db()
    except Exception as e:  # noqa: BLE001
        db_report = {"ok": False, "engine": None,
                     "error": f"{type(e).__name__}: {str(e)[:120]}"}
        terminals = {"reported": None, "error": "database tidak tersedia"}
    else:
        db_report = _database_readiness(conn)
        # health hanya SELECT — akhiri transaksi agar tidak idle-in-tx;
        # koneksi thread-local tetap dipakai request lain setelah ini.
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        terminals = _terminals_overview(conn)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
    ok = bool(db_report.get("ok"))
    body = {"ok": ok, "service": "jurnal-mt5", "time": _now_iso(),
            "database": db_report, "mt5_terminals": terminals}
    return jsonify(body), (200 if ok else 503)


@api_bp.get("/admin/health/terminals")
def admin_health_terminals():
    """Status terminal per customer — probe NYATA, terpisah dari health.

    Bootstrap admin via env API_TOKEN. Untuk setiap customer aktif:
    scope RLS ke customer itu, baca baris terminal, lalu probe TCP+
    handshake RPyC (terminal_registry.health_check — mode deep MT5
    HANYA bila ?deep=1). Kegagalan satu terminal tidak menggagalkan
    endpoint; endpoint tetap 200 dengan status per customer (bila DB
    sendiri mati, /api/v1/health yang sudah melapor 503).
    """
    if not _admin_token_ok():
        abort(401, description="Token admin tidak valid")
    conn = get_db()
    deep = _bool_arg("deep")
    ids = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM customers ORDER BY id").fetchall()]
    items = []
    registry = current_app.extensions.get("mt5_registry")
    for cid in ids:
        try:
            dbmod.set_customer_context(conn, cid)
            if registry is None:
                items.append({"customer_id": cid, "status": "unknown",
                              "error": "registry tidak tersedia"})
                continue
            items.append(registry.health_check(conn, cid, deep=deep,
                                               timeout=2.0))
        except Exception as e:  # noqa: BLE001 — 1 customer gagal ≠ semua
            items.append({"customer_id": cid, "status": "error",
                          "error": f"{type(e).__name__}: {str(e)[:120]}"})
        finally:
            dbmod.clear_customer_context(conn)
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass
    return jsonify({"ok": True, "checked_at": _now_iso(),
                    "items": items})


# ------------------------------------------------------------------- auth
@api_bp.post("/auth/register")
@require_rate_limit("register", 5, 3600)
def auth_register():
    import audit as auditmod
    import auth as authmod
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")
    display_name = str(body.get("display_name") or "") or None
    conn = get_db()
    try:
        created = authmod.register_customer(conn, cfg(), email, password,
                                            display_name)
    except authmod.AuthError as e:
        abort(e.code, description=e.message)
    auditmod.audit(conn, "register", customer_id=created["customer_id"],
                   user_id=created["user_id"],
                   detail={"code": created["code"]})
    return jsonify({"ok": True, "customer_code": created["code"],
                    "email": created["email"]}), 201


@api_bp.post("/auth/login")
@require_rate_limit("login", 10, 300)
def auth_login():
    import audit as auditmod
    import auth as authmod
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")
    conn = get_db()
    try:
        result = authmod.login(conn, cfg(), email, password,
                               authmod.current_ip())
    except authmod.AuthError as e:
        auditmod.audit(conn, "login_gagal",
                       detail={"reason": e.message[:120]})
        abort(e.code, description=e.message)
    auditmod.audit(conn, "login", customer_id=result["customer_id"],
                   user_id=result["user_id"])
    return jsonify({"ok": True, "token": result["token"],
                    "token_type": "bearer"})


@api_bp.post("/auth/logout")
@require_customer
def auth_logout():
    import auth as authmod
    authmod.revoke_token(get_db(), g.token_id)
    _audit("logout")
    return jsonify({"ok": True})


@api_bp.get("/auth/me")
@require_customer
def auth_me():
    conn = get_db()
    row = conn.execute(
        """SELECT c.id AS customer_id, c.code, c.display_name, c.status,
                  u.email, u.last_login_at
           FROM customers c JOIN users u ON u.customer_id = c.id
           WHERE c.id=?""", (g.customer_id,)).fetchone()
    acc = conn.execute(
        "SELECT id, login, server, currency, last_sync_at FROM mt5_accounts"
        " WHERE customer_id=?", (g.customer_id,)).fetchone()
    term = conn.execute(
        "SELECT rpyc_port, service_name, status FROM mt5_terminals"
        " WHERE customer_id=?", (g.customer_id,)).fetchone()
    return jsonify({
        "customer": dict(row) if row else None,
        "account": dict(acc) if acc else None,
        "terminal": dict(term) if term else None,
    })


# ----------------------------------------------------- akun MT5 (bind/unbind)
@api_bp.get("/accounts")
@require_customer
def accounts_current():
    conn = get_db()
    acc = conn.execute(
        "SELECT * FROM mt5_accounts WHERE customer_id=?",
        (g.customer_id,)).fetchone()
    return jsonify({"account": dict(acc) if acc else None})


@api_bp.post("/accounts/bind")
@require_customer
def accounts_bind():
    """Bind 1 akun MT5 (MVP: satu per customer; kredensial dienkripsi)."""
    import audit as auditmod
    from security import crypto
    from services.terminals import TerminalError
    body = request.get_json(silent=True) or {}
    try:
        login_val = int(body.get("login"))
    except (TypeError, ValueError):
        abort(400, description="'login' harus angka (login MT5)")
    password = str(body.get("password") or "")
    server = str(body.get("server") or "").strip()
    if not login_val or not server or not password:
        abort(400, description="'login', 'password', dan 'server' wajib")
    if len(server) > 128 or len(password) > 256:
        abort(400, description="'server'/'password' terlalu panjang")

    conn = get_db()
    if conn.execute("SELECT 1 FROM mt5_accounts WHERE customer_id=?",
                    (g.customer_id,)).fetchone() is not None:
        abort(409, description="Customer sudah punya akun MT5 (MVP: 1 akun "
                               "per customer). Unbind dulu untuk mengganti.")
    if conn.execute("SELECT 1 FROM mt5_accounts WHERE login=? AND server=?",
                    (login_val, server)).fetchone() is not None:
        abort(409, description="Akun MT5 itu sudah ter-bind ke customer lain")
    try:
        enc, key_version = crypto.encrypt_password(cfg(), password)
    except crypto.CredentialCryptoError as e:
        abort(503, description=str(e))
    registry = current_app.extensions["mt5_registry"]
    try:
        term = registry.provision_terminal(conn, g.customer_id)
    except TerminalError as e:
        abort(409, description=str(e))
    now = _now_iso()
    try:
        conn.execute(
            """INSERT INTO mt5_accounts (customer_id, login, server,
                 first_sync_at, last_sync_at) VALUES (?,?,?,?,?)""",
            (g.customer_id, login_val, server, now, None))
        acc_id = int(conn.execute(
            "SELECT id FROM mt5_accounts WHERE customer_id=?",
            (g.customer_id,)).fetchone()["id"])
        conn.execute(
            "INSERT INTO mt5_credentials (account_id, enc_password,"
            " key_version) VALUES (?,?,?)", (acc_id, enc, key_version))
        conn.commit()
    except dbmod.INTEGRITY_ERRORS as e:
        # balapan bind login/server sama antar customer (UNIQUE) —
        # rollback melepas lock terminal juga; respons tetap 409.
        conn.rollback()
        log.warning("bind bentrok (UNIQUE) customer=%s login=%s: %s",
                    g.customer_id, login_val, e)
        abort(409, description="Akun MT5 itu sudah ter-bind ke customer "
                               "lain (bentrok)")
    auditmod.audit(conn, "bind", customer_id=g.customer_id, user_id=g.user_id,
                   entity=f"mt5_account:{acc_id}",
                   detail={"server": server, "rpyc_port": term["rpyc_port"]})

    # sync awal di background — bind tidak diblokir riwayat 90 hari.
    # Dimatikan bila provider di-inject (konteks test) agar deterministik.
    if current_app.extensions.get("auto_initial_sync", True):
        app_obj = current_app._get_current_object()

        def _initial_sync(app=app_obj, acc=acc_id):
            try:
                svc = app.extensions["mt5_sync"]
                with app.app_context():
                    svc.sync_account(acc, mode="initial")
            except Exception:  # noqa: BLE001
                log.exception("sync awal akun %d gagal", acc)
        threading.Thread(target=_initial_sync, daemon=True,
                         name=f"initial-sync-{acc_id}").start()

    acc = conn.execute("SELECT * FROM mt5_accounts WHERE id=?",
                       (acc_id,)).fetchone()
    out = dict(acc)
    out["terminal"] = term
    return jsonify(out), 201


@api_bp.post("/accounts/unbind")
@require_customer
def accounts_unbind():
    """Lepas akun: hapus akun+data (CASCADE), evict runtime & terminal."""
    import audit as auditmod
    conn = get_db()
    acc = _own_account(conn)
    acc_id = int(acc["id"])
    registry = current_app.extensions["mt5_registry"]
    registry.evict(acc_id)          # terminal lain TIDAK terpengaruh
    conn.execute("DELETE FROM mt5_terminals WHERE customer_id=?",
                 (g.customer_id,))
    conn.execute("DELETE FROM mt5_accounts WHERE id=?", (acc_id,))
    conn.commit()
    auditmod.audit(conn, "unbind", customer_id=g.customer_id,
                   user_id=g.user_id, entity=f"mt5_account:{acc_id}")
    return jsonify({"ok": True, "removed_account_id": acc_id})


# ------------------------------------------------- self-link akun MT5 (D1/D2)
def _server_slug(server: str) -> str:
    """Slug server utk email sintetis internal (TIDAK pernah ditampilkan
    di UI): karakter non [a-z0-9] -> '-'; kosong -> 'srv'."""
    s = "".join(ch if ch.isalnum() else "-" for ch in server.lower())
    s = s.strip("-")[:48] or "srv"
    return s


@api_bp.post("/mt5/connect")
@require_rate_limit("mt5_connect", 10, 300)
def mt5_connect():
    """Self-link otomatis akun MT5 — redesign UI Jurnal MT5 (D1/D2).

    Identitas customer = nomor akun MT5 + server broker + password
    INVESTOR (sesi MT5 read-only). UI tidak punya input URL/email/
    password customer/token manual — URL backend ditentukan konfigurasi
    aplikasi, dan endpoint ini membuat (atau mengenali) customer + akun
    dalam satu langkah lalu mengeluarkan token sesi.

    Keamanan:
    - Password investor HANYA di memori proses selama request; tersimpan
      TERENKRIPSI (Fernet) di mt5_credentials; TIDAK pernah di-log dan
      TIDAK masuk audit_logs.
    - Kapasitas: maks cfg.max_customers (default 4) customer aktif —
      cek-dan-tulis diserialisasi capacity_guard (aman pendaftaran
      bersamaan, audit #4); alokasi terminal juga ter-guard + UNIQUE port.
    - UNIQUE(login, server) mt5_accounts — balapan menjadi 409 rapi.
    - Rate limit per IP (10/5 menit) + lockout per (login, server)
      setelah verifikasi password gagal.
    - Pesan gagal seragam (tidak membocorkan keberadaan akun); alasan
      sebenarnya hanya di audit_logs.
    - Dapat dinonaktifkan admin: MT5_SELF_LINK_ENABLED=false -> 403.

    PUTUSKAN KONEKSI (D3) = POST /mt5/disconnect — hanya mencabut token
    sesi; riwayat jurnal tidak dihapus; unbind tetap hanya via
    POST /api/v1/accounts/unbind (API/admin).
    """
    import audit as auditmod
    import auth as authmod
    from security import crypto
    from services.terminals import TerminalError

    c = cfg()
    if not getattr(c, "mt5_self_link_enabled", True):
        abort(403, description="Pendaftaran mandiri (self-link) sedang "
                               "dinonaktifkan admin. Hubungi admin untuk "
                               "provisioning akun.")

    body = request.get_json(silent=True) or {}
    try:
        login_val = int(body.get("login"))
    except (TypeError, ValueError):
        abort(400, description="'login' harus angka (nomor akun MT5)")
    password = str(body.get("password") or "")
    server = str(body.get("server") or "").strip()
    if not login_val or login_val < 0 or not server or not password:
        abort(400, description="'login', 'server', dan "
                               "'password investor' wajib diisi")
    if len(server) > 128 or len(password) > 256:
        abort(400, description="'server'/'password' terlalu panjang")

    lock = authmod._lockout_for(c)
    lock_key = f"mt5-connect:{login_val}:{server}"
    if lock.is_locked(lock_key):
        abort(429, description="Terlalu banyak percobaan gagal — coba lagi "
                               "nanti")

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM mt5_accounts WHERE login=? AND server=?",
        (login_val, server)).fetchone()

    # ---- akun sudah terdaftar: verifikasi password vs kredensial
    # terenkripsi (plaintext hanya di memori; perbandingan konstanta-
    # waktu), lalu keluarkan token sesi customer pemiliknya.
    if existing is not None:
        try:
            cred = conn.execute(
                "SELECT enc_password, key_version FROM mt5_credentials"
                " WHERE account_id=?", (int(existing["id"]),)).fetchone()
            if cred is None:
                abort(503, description="Kredensial akun tidak lengkap — "
                                       "hubungi admin")
            stored = crypto.decrypt_password(c, cred["enc_password"],
                                             cred["key_version"])
            matched = hmac.compare_digest(stored, password)
        except crypto.CredentialCryptoError:
            abort(503, description="Kredensial akun tidak bisa diverifikasi "
                                   "— hubungi admin")
        if not matched:
            lock.record_failure(lock_key)
            auditmod.audit(conn, "mt5_connect_gagal",
                           detail={"login": login_val, "server": server,
                                   "reason": "password_salah"})
            abort(401, description="Verifikasi gagal — periksa nomor akun, "
                                   "server, dan password investor")
        crow = conn.execute(
            "SELECT status FROM customers WHERE id=?",
            (int(existing["customer_id"]),)).fetchone()
        if crow is None or crow["status"] != "active":
            abort(403, description="Customer dinonaktifkan — hubungi admin")
        urow = conn.execute(
            "SELECT id FROM users WHERE customer_id=?",
            (int(existing["customer_id"]),)).fetchone()
        if urow is None:
            abort(503, description="Identitas customer tidak lengkap — "
                                   "hubungi admin")
        lock.clear(lock_key)
        token = authmod.issue_token(conn, c,
                                    user_id=int(urow["id"]),
                                    customer_id=int(existing["customer_id"]),
                                    ip=authmod.current_ip())
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?",
                     (_now_iso(), int(urow["id"])))
        conn.commit()
        auditmod.audit(conn, "mt5_connect",
                       customer_id=int(existing["customer_id"]),
                       user_id=int(urow["id"]),
                       entity=f"mt5_account:{int(existing['id'])}",
                       detail={"mode": "login", "server": server})
        return jsonify({"ok": True, "mode": "login", "token": token,
                        "token_type": "bearer",
                        "account": {"login": int(existing["login"]),
                                    "server": existing["server"],
                                    "currency": existing["currency"]}}), 200

    # ---- akun baru: daftarkan customer (capacity-guard, D2) + akun +
    # kredensial terenkripsi + terminal, lalu keluarkan token sesi.
    try:
        enc, key_version = crypto.encrypt_password(c, password)
    except crypto.CredentialCryptoError as e:
        abort(503, description=str(e))

    # Password user internal = secret acak yang tidak pernah dikirim ke
    # siapa pun: login customer self-link SELALU lewat /mt5/connect
    # (verifikasi password investor), bukan /auth/login.
    internal_secret = secrets.token_urlsafe(32)
    synthetic_email = f"{login_val}.{_server_slug(server)}@mt5.link"

    # Identitas (login, server) bisa saja SUDAH pernah terdaftar tanpa
    # akun aktif — mis. setelah unbind via API/admin (yang sengaja tidak
    # menghapus customer). Pakai ulang customer yang sama; kapasitas
    # tidak bertambah karena slot itu memang sudah terpakai.
    idrow = conn.execute(
        """SELECT u.id AS user_id, u.customer_id, c.code AS code,
                  c.status AS customer_status,
                  (SELECT COUNT(*) FROM mt5_accounts a
                     WHERE a.customer_id = u.customer_id) AS bound_accounts
           FROM users u JOIN customers c ON c.id = u.customer_id
           WHERE u.email=?""", (synthetic_email,)).fetchone()
    reuse = idrow is not None
    if reuse:
        if idrow["customer_status"] != "active":
            abort(403, description="Customer dinonaktifkan — hubungi admin")
        if int(idrow["bound_accounts"]) > 0:
            abort(409, description="Customer sudah punya akun MT5 "
                                   "ter-bind. Unbind dulu untuk mengganti.")
        created = {"customer_id": int(idrow["customer_id"]),
                   "user_id": int(idrow["user_id"]),
                   "code": idrow["code"]}
    else:
        created = None
        try:
            created = authmod.register_customer(
                conn, c, synthetic_email, internal_secret, None,
                skip_password_validation=True)
            conn.commit()  # persist customer+user SEBELUM provisioning
        except authmod.AuthError as e:
            if e.code == 403:  # kapasitas penuh (max_customers)
                auditmod.audit(conn, "mt5_connect_gagal",
                               detail={"login": login_val, "server": server,
                                       "reason": "kapasitas_penuh"})
            abort(e.code, description=e.message)

    try:
        term = registry_provision(current_app, conn,
                                  created["customer_id"])
    except TerminalError:
        if not reuse:  # customer baru gagal provisioning -> buang
            _discard_orphan_customer(conn, created["customer_id"])
        abort(409, description="Provisioning terminal MT5 gagal — kapasitas "
                               "terminal penuh atau alokasi port bentrok")
    now = _now_iso()
    try:
        conn.execute(
            """INSERT INTO mt5_accounts (customer_id, login, server,
                 first_sync_at, last_sync_at) VALUES (?,?,?,?,?)""",
            (created["customer_id"], login_val, server, now, None))
        acc_id = int(conn.execute(
            "SELECT id FROM mt5_accounts WHERE customer_id=?",
            (created["customer_id"],)).fetchone()["id"])
        conn.execute(
            "INSERT INTO mt5_credentials (account_id, enc_password,"
            " key_version) VALUES (?,?,?)", (acc_id, enc, key_version))
        conn.commit()
    except dbmod.INTEGRITY_ERRORS as e:
        # balapan UNIQUE(login, server) antar request bersamaan —
        # customer BARU dibuang agar slot kapasitas tidak bocor;
        # customer hasil pakai-ulang tidak disentuh (bukan yatim).
        conn.rollback()
        if not reuse:
            _discard_orphan_customer(conn, created["customer_id"])
        log.warning("mt5_connect bentrok (UNIQUE) login=%s: %s",
                    login_val, e)
        abort(409, description="Akun MT5 itu baru saja terdaftar — "
                               "coba hubungkan lagi dengan password "
                               "investor yang benar")
    except Exception:
        conn.rollback()
        if not reuse:
            _discard_orphan_customer(conn, created["customer_id"])
        raise

    auditmod.audit(conn, "mt5_connect",
                   customer_id=created["customer_id"],
                   user_id=created["user_id"],
                   entity=f"mt5_account:{acc_id}",
                   detail={"mode": "self_link", "server": server,
                           "rpyc_port": term["rpyc_port"]})

    # sync awal di background — sama seperti bind; tidak memblokir request.
    if current_app.extensions.get("auto_initial_sync", True):
        app_obj = current_app._get_current_object()

        def _initial_sync(app=app_obj, acc=acc_id):
            try:
                svc = app.extensions["mt5_sync"]
                with app.app_context():
                    svc.sync_account(acc, mode="initial")
            except Exception:  # noqa: BLE001
                log.exception("sync awal akun %d gagal", acc)
        threading.Thread(target=_initial_sync, daemon=True,
                         name=f"initial-sync-{acc_id}").start()

    token = authmod.issue_token(conn, c,
                                user_id=created["user_id"],
                                customer_id=created["customer_id"],
                                ip=authmod.current_ip())
    acc = conn.execute("SELECT * FROM mt5_accounts WHERE id=?",
                       (acc_id,)).fetchone()
    return jsonify({"ok": True, "mode": "self_link", "token": token,
                    "token_type": "bearer",
                    "customer_code": created["code"],
                    "account": {"login": int(acc["login"]),
                                "server": acc["server"],
                                "currency": acc["currency"]},
                    "terminal": {"rpyc_port": term["rpyc_port"],
                                 "status": term["status"]}}), 201


def registry_provision(app, conn, customer_id: int) -> dict:
    """Provision terminal via registry (pemisahan kecil utk keterbacaan)."""
    return app.extensions["mt5_registry"].provision_terminal(conn,
                                                             customer_id)


def _discard_orphan_customer(conn, customer_id: int) -> None:
    """Hapus customer yang BARU dibuat tapi provisioningnya gagal —
    agar slot kapasitas max_customers tidak bocor. HANYA untuk customer
    tanpa akun MT5 (guard: tidak ada baris mt5_accounts)."""
    try:
        if conn.execute(
                "SELECT 1 FROM mt5_accounts WHERE customer_id=?",
                (customer_id,)).fetchone() is None:
            conn.execute("DELETE FROM customers WHERE id=?",
                         (customer_id,))
            conn.commit()
    except Exception:  # noqa: BLE001 — pembersihan tak boleh menutupi
        conn.rollback()
        log.exception("gagal membersihkan customer yatim %d", customer_id)


@api_bp.post("/mt5/disconnect")
@require_customer
def mt5_disconnect():
    """PUTUSKAN KONEKSI (D3): logout + cabut token sesi INI saja.

    Riwayat jurnal, akun ter-bind, kredensial, dan terminal TIDAK
    disentuh. Unbind (menghapus data) tetap hanya melalui
    POST /api/v1/accounts/unbind yang tidak diekspos di UI.
    """
    import auth as authmod
    authmod.revoke_token(get_db(), g.token_id)
    _audit("mt5_disconnect")
    return jsonify({"ok": True})


# ------------------------------------------------------------------- MT5
@api_bp.get("/mt5/status")
@require_customer
def mt5_status():
    c = cfg()
    conn = get_db()
    status = {
        "provider": c.mt5_provider,
        "enabled": c.mt5_enabled,
        "connected": False,
        "reason": None if c.mt5_enabled else "disabled",
        "time": _now_iso(),
    }
    acc = conn.execute(
        "SELECT * FROM mt5_accounts WHERE customer_id=?",
        (g.customer_id,)).fetchone()
    term = conn.execute(
        "SELECT * FROM mt5_terminals WHERE customer_id=?",
        (g.customer_id,)).fetchone()
    if acc is not None:
        status["account_id"] = int(acc["id"])
    if term is not None:
        # additive (kompatibel): info terminal milik customer ini saja
        status["terminal"] = {
            "rpyc_port": term["rpyc_port"],
            "service_name": term["service_name"],
            "status": term["status"],
        }
    if c.mt5_enabled and acc is not None:
        try:
            registry = current_app.extensions["mt5_registry"]
            rt = registry.runtime_for(conn, int(acc["id"]))
            rt.provider.connect()
            info = rt.provider.account_info()
            rt.provider.disconnect()
            status["connected"] = True
            status["reason"] = None
            status["live_account"] = {
                "login": info.get("login"), "server": info.get("server"),
                "currency": info.get("currency"),
                "balance": info.get("balance"), "equity": info.get("equity"),
            }
        except MT5Error as e:
            status["reason"] = str(e)
        except Exception as e:  # noqa: BLE001 — status tidak boleh crash
            status["reason"] = f"{type(e).__name__}"
    state = None
    if acc is not None:
        state = conn.execute(
            "SELECT * FROM sync_state WHERE account_id=?",
            (acc["id"],)).fetchone()
    last_log = None
    if acc is not None:
        last_log = conn.execute(
            "SELECT * FROM sync_logs WHERE account_id=? ORDER BY id DESC"
            " LIMIT 1", (acc["id"],)).fetchone()
    counts = {"trades": 0, "deals": 0}
    if acc is not None:
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM " + _trades_table() +
            " WHERE account_id=?) AS trades,"
            " (SELECT COUNT(*) FROM mt5_deals WHERE account_id=?) AS deals",
            (acc["id"], acc["id"])).fetchone()
        counts = {"trades": counts["trades"], "deals": counts["deals"]}
    status["last_sync"] = dict(state) if state else None
    status["last_run"] = dict(last_log) if last_log else None
    status["stored"] = counts
    return jsonify(status)


def _trades_table() -> str:
    import db as dbmod
    return dbmod.trades_table(get_db())


@api_bp.get("/mt5/account")
@require_customer
def mt5_account():
    row = _own_account(get_db())
    return jsonify(dict(row))


@api_bp.post("/mt5/sync")
@require_customer
def mt5_sync():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "incremental")
    _parse_enum("mode", mode, ("incremental", "initial", "full", "manual"))
    full = bool(body.get("full", False))
    days = None
    if body.get("days") is not None:
        days = _parse_int("days", body.get("days"), 1, 3650)
    conn = get_db()
    acc = _own_account(conn)
    svc = current_app.extensions["mt5_sync"]
    try:
        summary = svc.sync_account(int(acc["id"]), mode=mode, full=full,
                                   days=days)
    except Exception as e:  # SyncLockBusy dari thread lain
        if "sedang berjalan" in str(e):
            abort(409, description="Sync lain sedang berjalan, coba lagi "
                                   "nanti")
        raise
    _audit("sync_manual", entity=f"mt5_account:{acc['id']}",
           detail={"mode": mode, "status": summary.get("status")})
    return jsonify(summary), 200


@api_bp.get("/mt5/sync/status")
@require_customer
def mt5_sync_status():
    conn = get_db()
    acc = _own_account(conn)
    state = conn.execute(
        "SELECT * FROM sync_state WHERE account_id=?",
        (acc["id"],)).fetchone()
    logs = [dict(r) for r in conn.execute(
        "SELECT * FROM sync_logs WHERE account_id=? ORDER BY id DESC"
        " LIMIT 20", (acc["id"],))]
    sched = current_app.extensions.get("mt5_scheduler")
    return jsonify({
        "state": dict(state) if state else None,
        "recent_logs": logs,
        "scheduler": sched.state() if sched else None,
        "interval_seconds": cfg().mt5_sync_interval_seconds,
    })


# ---------------------------------------------------------------- journal
@api_bp.get("/journal/trades")
@require_customer
def journal_trades():
    conn = get_db()
    account_id = int(_own_account(conn)["id"])
    tt = _trades_table()
    f_from, f_to, symbol, to_excl = _json_filters()
    status = request.args.get("status", "all")
    _parse_enum("status", status, ("all", "open", "closed"))
    direction = request.args.get("direction", "all")
    _parse_enum("direction", direction, ("all", "buy", "sell"))
    sort = request.args.get("sort", "close_time")
    _parse_enum("sort", sort, ("close_time", "open_time", "net_profit"))
    order = request.args.get("order", "desc")
    _parse_enum("order", order, ("asc", "desc"))
    page = _parse_int("page", request.args.get("page", 1), 1, 10_000)
    page_size = _parse_int("page_size",
                           request.args.get("page_size", 50), 1, 200)

    where = "account_id=?"
    args: list = [account_id]
    if status != "all":
        where += " AND status=?"
        args.append(status)
    if direction != "all":
        where += " AND direction=?"
        args.append(direction)
    if symbol:
        where += " AND symbol=?"
        args.append(symbol)
    if f_from:
        where += " AND COALESCE(close_time_utc, open_time_utc) >= ?"
        args.append(f_from)
    if f_to:
        where += (" AND COALESCE(close_time_utc, open_time_utc) < ?"
                  if to_excl else
                  " AND COALESCE(close_time_utc, open_time_utc) <= ?")
        args.append(f_to)
    order_col = {"close_time": "COALESCE(close_time_utc, open_time_utc)",
                 "open_time": "open_time_utc",
                 "net_profit": "net_profit"}[sort]

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM {tt} WHERE {where}",
        args).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM {tt} WHERE {where}"
        f" ORDER BY {order_col} {order.upper()} LIMIT ? OFFSET ?",
        args + [page_size, (page - 1) * page_size],
    ).fetchall()

    meta = analytics.load_symbol_meta(conn, account_id)
    items = [analytics.enrich_trade(dict(r), meta, cfg().symbol_config)
             for r in rows]
    return jsonify({
        "items": items,
        "page": page, "page_size": page_size, "total": total,
        "pages": (total + page_size - 1) // page_size if total else 0,
        "filters": {"from": f_from, "to": f_to,
                    "to_exclusive": to_excl, "symbol": symbol},
    })


@api_bp.get("/journal/trades/<int:position_id>")
@require_customer
def journal_trade_detail(position_id: int):
    conn = get_db()
    account_id = int(_own_account(conn)["id"])
    tt = _trades_table()
    row = conn.execute(
        f"SELECT * FROM {tt} WHERE account_id=? AND position_id=?",
        (account_id, position_id)).fetchone()
    if row is None:
        abort(404, description=f"Trade position_id={position_id} tidak "
                               f"ditemukan")
    meta = analytics.load_symbol_meta(conn, account_id)
    trade = analytics.enrich_trade(dict(row), meta, cfg().symbol_config)
    deals = [dict(r) for r in conn.execute(
        "SELECT * FROM mt5_deals WHERE account_id=? AND position_id=?"
        " ORDER BY time_utc, ticket", (account_id, position_id))]
    orders = [dict(r) for r in conn.execute(
        "SELECT * FROM mt5_orders WHERE account_id=? AND position_id=?"
        " ORDER BY time_setup_utc, ticket", (account_id, position_id))]
    return jsonify({"trade": trade, "deals": deals, "orders": orders})


@api_bp.get("/journal/stats")
@require_customer
def journal_stats():
    conn = get_db()
    account_id = int(_own_account(conn)["id"])
    f_from, f_to, symbol, to_excl = _json_filters()
    stats = analytics.compute_stats(conn, account_id, cfg().symbol_config,
                                    f_from, f_to, symbol,
                                    to_exclusive=to_excl)
    return jsonify(stats)


@api_bp.get("/journal/equity-curve")
@require_customer
def journal_equity_curve():
    conn = get_db()
    account_id = int(_own_account(conn)["id"])
    f_from, f_to, symbol, to_excl = _json_filters()
    curve = analytics.equity_curve(conn, account_id, cfg().symbol_config,
                                   f_from, f_to, symbol,
                                   to_exclusive=to_excl)
    return jsonify(curve)


@api_bp.get("/journal/reconciliation")
@require_customer
def journal_reconciliation():
    """Rekonsiliasi raw deals vs trades pada interval yang identik."""
    conn = get_db()
    account_id = int(_own_account(conn)["id"])
    f_from, f_to, symbol, to_excl = _json_filters()
    rec = analytics.reconciliation(conn, account_id,
                                   f_from, f_to, symbol,
                                   to_exclusive=to_excl)
    return jsonify(rec)


@api_bp.get("/symbols")
@require_customer
def symbols():
    conn = get_db()
    account_id = int(_own_account(conn)["id"])
    rows = conn.execute(
        "SELECT symbol, point, digits, pip_size FROM mt5_symbols"
        " WHERE account_id=? ORDER BY symbol", (account_id,)).fetchall()
    return jsonify({"items": [dict(r) for r in rows]})


# ------------------------------------------------------------------ admin
@api_bp.get("/admin/customers")
def admin_list_customers():
    """Bootstrap admin via env API_TOKEN — untuk operasional VPS."""
    if not _admin_token_ok():
        abort(401, description="Token admin tidak valid")
    rows = [dict(r) for r in get_db().execute(
        """SELECT c.id, c.code, c.display_name, c.status, c.created_at,
                  u.email,
                  (SELECT login FROM mt5_accounts a WHERE a.customer_id=c.id)
                    AS bound_login
           FROM customers c LEFT JOIN users u ON u.customer_id=c.id
           ORDER BY c.id""")]
    return jsonify({"items": rows})


@api_bp.post("/admin/customers")
def admin_create_customer():
    if not _admin_token_ok():
        abort(401, description="Token admin tidak valid")
    import audit as auditmod
    import auth as authmod
    body = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        created = authmod.register_customer(
            conn, cfg(), str(body.get("email") or ""),
            str(body.get("password") or ""),
            str(body.get("display_name") or "") or None)
    except authmod.AuthError as e:
        abort(e.code, description=e.message)
    auditmod.audit(conn, "admin_create_customer",
                   customer_id=created["customer_id"],
                   detail={"code": created["code"]})
    return jsonify(created), 201


# ------------------------------------------------------------- error json
@api_bp.errorhandler(400)
@api_bp.app_errorhandler(400)
def bad_request(e):  # pragma: no cover - dipakai via abort
    return jsonify({"error": {"code": 400, "message": e.description}}), 400


@api_bp.errorhandler(401)
def unauthorized(e):  # pragma: no cover
    return jsonify({"error": {"code": 401, "message": e.description}}), 401


@api_bp.errorhandler(403)
def forbidden(e):
    return jsonify({"error": {"code": 403, "message": e.description}}), 403


@api_bp.app_errorhandler(404)
def not_found(e):
    return jsonify({"error": {"code": 404,
                              "message": getattr(e, "description",
                                                 "tidak ditemukan")}}), 404


@api_bp.app_errorhandler(409)
def conflict(e):
    return jsonify({"error": {"code": 409, "message": e.description}}), 409


@api_bp.errorhandler(429)
def too_many(e):
    return jsonify({"error": {"code": 429, "message": e.description}}), 429


@api_bp.errorhandler(503)
def service_unavailable(e):
    return jsonify({"error": {"code": 503, "message": e.description}}), 503


@api_bp.app_errorhandler(500)
def server_error(e):  # pragma: no cover
    log.exception("500 di %s", request.path)
    return jsonify({"error": {"code": 500,
                              "message": "kesalahan internal"}}), 500
