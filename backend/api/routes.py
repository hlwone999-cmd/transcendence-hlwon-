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
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

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


def require_rate_limit(action: str, limit, window_seconds: int):
    """Dekorator rate-limit per IP klien tepercaya + aksi (persisten di DB,
    lintas worker). [limit] int atau nama atribut config."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            import auth as authmod
            ip = authmod.current_ip() or "?"
            lim = limit if isinstance(limit, int) else \
                int(getattr(cfg(), limit))
            try:
                authmod.register_rate_limit(f"{action}:{ip}", lim,
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
def _subnet_of(ip: str | None) -> str:
    """Subnet klien untuk rate-limit registrasi (/24 IPv4, /64 IPv6)."""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return "?"
    prefix = 24 if addr.version == 4 else 64
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


@api_bp.post("/auth/register")
@require_rate_limit("register", "register_per_ip_hour", 3600)
def auth_register():
    """Registrasi portal publik (keputusan produk #1: TETAP AKTIF).

    Perlindungan (Tahap 1): validasi input (auth.normalize_email /
    validate_password / validate_display_name), rate limit PERSISTEN per
    IP klien tepercaya (per jam + per hari) dan per subnet (per hari) —
    X-Forwarded-For dari klien tidak dipercaya (auth.current_ip).
    Registrasi TIDAK memberi terminal/slot MT5: slot hanya dialokasikan
    saat bind/connect akun MT5 yang lolos verifikasi.
    """
    import audit as auditmod
    import auth as authmod
    c = cfg()
    if not getattr(c, "public_register_enabled", True):
        abort(403, description="Registrasi publik dinonaktifkan — "
                               "hubungi admin untuk provisioning customer")
    ip = authmod.current_ip() or "?"
    try:
        authmod.register_rate_limit(f"register-day:{ip}",
                                    c.register_per_ip_day, 86400)
        authmod.register_rate_limit(f"register-net:{_subnet_of(ip)}",
                                    c.register_per_subnet_day, 86400)
    except authmod.AuthError as e:
        abort(e.code, description=e.message)
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")
    display_name = str(body.get("display_name") or "") or None
    conn = get_db()
    try:
        created = authmod.register_customer(conn, c, email, password,
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
        "SELECT id, login, server, currency, last_sync_at, state"
        " FROM mt5_accounts WHERE customer_id=?", (g.customer_id,)).fetchone()
    # Remediasi R1: port RPyC & nama unit systemd adalah metadata
    # infrastruktur internal — tidak diekspos ke klien.
    term = conn.execute(
        "SELECT status FROM mt5_terminals"
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
    """Bind 1 akun MT5 untuk customer portal (sesi sudah ada).

    - Keputusan produk #4: customer hanya dapat (re)aktivasi akun MT5
      yang SAMA (login+server); akun lain ditolak 409 (riwayat dijaga).
    - Slot MT5 penuh -> 403 (registrasi tidak memakai slot).
    - MT5_ENABLED=true: auto-provisioning + verifikasi live fail-closed
      lewat job (T5: password MASTER ditolak di jalur ini juga). Respons
      201/200 bila job selesai dalam waktu tunggu, selain itu 202 +
      job_id/job_secret untuk polling POST /mt5/connect/status.
    - MT5_ENABLED=false: tidak ada terminal untuk diverifikasi —
      kredensial tersimpan TIDAK terpakai sampai MT5 diaktifkan; saat itu
      sync menegakkan investor-only (services.sync.enforce_investor_session).
    """
    import audit as auditmod
    from security import crypto
    from services.terminals import TerminalCapacityError, TerminalError
    body = request.get_json(silent=True) or {}
    try:
        login_val = int(body.get("login"))
    except (TypeError, ValueError):
        abort(400, description="'login' harus angka (login MT5)")
    password = str(body.get("password") or "")
    server = str(body.get("server") or "").strip()
    if not login_val or login_val < 0 or not server or not password:
        abort(400, description="'login', 'password', dan 'server' wajib")
    if len(server) > 128 or len(password) > 256:
        abort(400, description="'server'/'password' terlalu panjang")

    c = cfg()
    conn = get_db()
    coord = current_app.extensions["mt5_coordinator"]
    coord.recover_expired(conn)
    mine = conn.execute("SELECT * FROM mt5_accounts WHERE customer_id=?",
                        (g.customer_id,)).fetchone()
    if mine is not None:
        same = (int(mine["login"]) == login_val and
                str(mine["server"]).strip().casefold() == server.casefold())
        if mine["state"] in ("active", "pending") or not same:
            abort(409, description="Customer sudah punya akun MT5 (MVP: 1 "
                                   "akun per customer; riwayat jurnal akun "
                                   "lama dipertahankan). Hubungi admin "
                                   "untuk mengganti akun.")
    elif _find_account(conn, login_val, server) is not None:
        abort(409, description="Akun MT5 itu sudah ter-bind ke customer lain")
    try:
        enc, key_version = crypto.encrypt_password(c, password)
    except crypto.CredentialCryptoError as e:
        abort(503, description=str(e))
    registry = current_app.extensions["mt5_registry"]

    prev_state = mine["state"] if mine is not None else None
    if mine is not None and not _claim_pending(conn, int(mine["id"]),
                                               prev_state):
        abort(409, description="Aktivasi akun ini sedang berjalan")
    try:
        term = registry.provision_terminal(conn, g.customer_id)
    except TerminalCapacityError as e:
        _restore_state(conn, mine, prev_state)
        abort(403, description=str(e))
    except TerminalError:
        _restore_state(conn, mine, prev_state)
        abort(409, description="Provisioning terminal MT5 gagal")
    try:
        if mine is not None:
            acc_id = int(mine["id"])
            _store_credentials(conn, g.customer_id, acc_id, enc, key_version)
        else:
            acc_id = _insert_account(conn, g.customer_id, login_val, server,
                                     enc, key_version, commit=False)
    except dbmod.INTEGRITY_ERRORS as e:
        _discard_unstarted(conn, term, g.customer_id)
        _restore_state(conn, mine, prev_state)
        # paritas PR #4: bentrok tercatat di log operasional (tanpa secret)
        log.warning("bind bentrok (UNIQUE) customer=%s: %s",
                    g.customer_id, type(e).__name__)
        abort(409, description="Akun MT5 itu sudah ter-bind ke customer "
                               "lain (bentrok)")

    if not c.mt5_enabled:
        # tanpa terminal hidup: kredensial disimpan (terenkripsi), akun
        # aktif, terminal disiapkan (install) tetapi tidak dijalankan.
        if term.get("created"):
            from services.provisioner import TerminalSpec
            try:
                coord.provisioner.install(TerminalSpec(
                    _code_of(conn, g.customer_id), int(term["rpyc_port"]),
                    term["provision_id"]))
            except Exception:  # noqa: BLE001
                log.exception("install terminal (MT5 nonaktif) gagal")
            conn.execute("UPDATE mt5_terminals SET lifecycle='stopped'"
                         " WHERE id=?", (term["terminal_id"],))
        conn.execute("UPDATE mt5_accounts SET state='active', unbound_at=NULL"
                     " WHERE id=?", (acc_id,))
        conn.commit()
        auditmod.audit(conn, "bind", customer_id=g.customer_id,
                       user_id=g.user_id, entity=f"mt5_account:{acc_id}",
                       detail={"server": server, "live_verified": False,
                               "reactivate": mine is not None})
        acc = conn.execute("SELECT * FROM mt5_accounts WHERE id=?",
                           (acc_id,)).fetchone()
        out = dict(acc)
        out["terminal"] = {"status": term["status"]}
        return jsonify(out), 200 if mine is not None else 201

    job_id, secret = coord.create_job(
        conn, kind="bind", customer_id=g.customer_id, user_id=g.user_id,
        account_id=acc_id, terminal=term, created_customer=False,
        created_account=mine is None, prev_account_state=prev_state,
        require_real=False, lock_ip_hash=None, lock_acct_hash=None)
    coord.start(job_id)
    job = coord.wait(conn, job_id, c.mt5_connect_sync_wait_seconds)
    auditmod.audit(conn, "bind_job", customer_id=g.customer_id,
                   user_id=g.user_id, entity=f"mt5_account:{acc_id}",
                   detail={"server": server, "state": job["state"]})
    return _job_response(conn, coord, job, secret, new_job=True)


@api_bp.post("/accounts/unbind")
@require_customer
def accounts_unbind():
    """Lepas akun MT5 — keputusan produk #2, #3 & #4.

    - Riwayat jurnal DIPERTAHANKAN (state='unbound', baris akun tetap).
    - Kredensial terenkripsi DIHAPUS; runtime di-evict; SEMUA token sesi
      customer dicabut (termasuk token request ini).
    - Terminal dihentikan & dihapus lewat helper DENGAN bukti kepemilikan
      (provision_id) — slot MT5 bebas hanya bila penghapusan terbukti
      berhasil. Tanpa bukti kepemilikan terminal TIDAK dihapus (ditandai
      'failed', slot tetap terpakai sampai admin menangani) sehingga port
      tidak pernah dialokasikan ke customer lain selama proses lama
      mungkin masih berjalan.
    """
    import audit as auditmod
    import auth as authmod
    conn = get_db()
    acc = _own_account(conn)
    acc_id = int(acc["id"])
    if acc["state"] == "pending":
        abort(409, description="Aktivasi akun sedang berjalan — coba lagi")
    registry = current_app.extensions["mt5_registry"]
    registry.evict(acc_id)
    conn.execute("DELETE FROM mt5_credentials WHERE account_id=?", (acc_id,))
    conn.execute("UPDATE mt5_accounts SET state='unbound', unbound_at=?"
                 " WHERE id=?", (_now_iso(), acc_id))
    conn.commit()
    authmod.revoke_all_for_customer(conn, g.customer_id)
    released = True
    term = conn.execute("SELECT id, provision_id FROM mt5_terminals WHERE"
                        " customer_id=?", (g.customer_id,)).fetchone()
    if term is not None:
        coord = current_app.extensions["mt5_coordinator"]
        released = coord.remove_terminal(conn, int(term["id"]),
                                         term["provision_id"])
    auditmod.audit(conn, "unbind", customer_id=g.customer_id,
                   user_id=g.user_id, entity=f"mt5_account:{acc_id}",
                   detail={"terminal_released": released})
    return jsonify({"ok": True, "unbound_account_id": acc_id,
                    "history_retained": True, "sessions_revoked": True,
                    "terminal_released": released})


# ------------------------------------------------- self-link akun MT5 (D1/D2)
# Pesan SERAGAM untuk semua kegagalan identitas (remediasi S2).
GENERIC_VERIFY_FAIL = (
    "Verifikasi ke server broker gagal — periksa nomor akun, server, dan "
    "password investor. Pastikan server dipilih sesuai akun (mis. "
    "Exness-Real vs Exness-Demo). Akun belum diaktifkan.")

_VERIFY_REASON = {
    "auth_failed": "live_auth_failed",
    "master_password": "master_password_ditolak",
    "identity_mismatch": "identitas_akun_tidak_cocok",
    "unverified": "account_info_tidak_lengkap",
    "not_real": "bukan_akun_real",
    "unreachable": "broker_unreachable",
    "timeout": "broker_timeout",
    "busy": "broker_busy",
}

# Alias kompatibilitas (implementasi di services.verification).
from mt5client.provider import (  # noqa: E402,F401
    BROKER_AUTH_MARKERS as _BROKER_AUTH_MARKERS,
    looks_like_broker_auth_error as _looks_like_broker_auth_error)
from services.verification import (  # noqa: E402,F401
    ACCOUNT_TRADE_MODE_REAL,
    classify_account_info as _classify_account_info,
    live_verify_account as _live_verify_account)


def _server_slug(server: str) -> str:
    """Slug server utk email sintetis internal (TIDAK pernah ditampilkan
    di UI): karakter non [a-z0-9] -> '-'; kosong -> 'srv'."""
    s = "".join(ch if ch.isalnum() else "-" for ch in server.lower())
    s = s.strip("-")[:48] or "srv"
    return s


def _synthetic_email(login_val: int, server: str) -> str:
    """Email internal identitas (login, server) — R6: slug + sidik jari
    SHA-256 nama server ter-normalisasi (casefold)."""
    import hashlib
    norm = server.strip().casefold()
    fp = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:10]
    return f"{login_val}.{_server_slug(server)[:32]}-{fp}@mt5.link"


def _find_account(conn, login_val: int, server: str):
    """Akun (login, server) — server case-insensitive (S2/R6)."""
    return conn.execute(
        "SELECT * FROM mt5_accounts WHERE login=? AND lower(server)=lower(?)",
        (login_val, server.strip())).fetchone()


def _code_of(conn, customer_id: int) -> str:
    return conn.execute("SELECT code FROM customers WHERE id=?",
                        (customer_id,)).fetchone()["code"]


def _store_credentials(conn, customer_id: int, acc_id: int, enc: str,
                       key_version: int) -> None:
    """Ganti kredensial terenkripsi dalam scope RLS pemilik (K1)."""
    dbmod.set_customer_context(conn, customer_id)
    conn.execute("DELETE FROM mt5_credentials WHERE account_id=?", (acc_id,))
    conn.execute("INSERT INTO mt5_credentials (account_id, enc_password,"
                 " key_version) VALUES (?,?,?)", (acc_id, enc, key_version))


def _insert_account(conn, customer_id: int, login_val: int, server: str,
                    enc: str, key_version: int, commit: bool = True) -> int:
    """INSERT akun (state 'pending') + kredensial dalam scope RLS (K1).

    mt5_credentials ber-RLS (USING juga berlaku sebagai WITH CHECK);
    scope dipasang ke customer PEMILIK baris — bukan pelemahan policy.
    """
    dbmod.set_customer_context(conn, customer_id)
    now = _now_iso()
    conn.execute(
        """INSERT INTO mt5_accounts (customer_id, login, server,
             first_sync_at, last_sync_at, state)
           VALUES (?,?,?,?,?,'pending')""",
        (customer_id, login_val, server, now, None))
    acc_id = int(conn.execute(
        "SELECT id FROM mt5_accounts WHERE customer_id=?",
        (customer_id,)).fetchone()["id"])
    conn.execute(
        "INSERT INTO mt5_credentials (account_id, enc_password,"
        " key_version) VALUES (?,?,?)", (acc_id, enc, key_version))
    if commit:
        conn.commit()
    return acc_id


def _claim_pending(conn, acc_id: int, prev_state: str) -> bool:
    """Klaim atomik akun untuk aktivasi (cegah dua job bersamaan)."""
    cur = conn.execute("UPDATE mt5_accounts SET state='pending' WHERE id=?"
                       " AND state=?", (acc_id, prev_state))
    conn.commit()
    return cur.rowcount == 1


def _restore_state(conn, acc_row, prev_state) -> None:
    if acc_row is None or prev_state is None:
        return
    try:
        conn.rollback()
        conn.execute("UPDATE mt5_accounts SET state=? WHERE id=? AND"
                     " state='pending'", (prev_state, int(acc_row["id"])))
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()


def _discard_unstarted(conn, term: dict, customer_id: int) -> None:
    """Gagal SEBELUM job dibuat: transaksi (baris terminal yang belum
    di-commit) di-rollback; berkas contoh mode mock dihapus HANYA bila
    terminal itu dibuat request ini dan tidak ada baris terminal lain
    untuk customer tsb (T4)."""
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass
    if not term.get("created") or cfg().terminal_provision_mode != "mock":
        return
    if conn.execute("SELECT 1 FROM mt5_terminals WHERE customer_id=?",
                    (customer_id,)).fetchone() is None:
        import shutil
        shutil.rmtree(str(Path(term["terminal_dir"]).parent),
                      ignore_errors=True)


def _abort_for_verify_failure(klass: str) -> None:
    if klass in ("auth_failed", "identity_mismatch"):
        abort(401, description=GENERIC_VERIFY_FAIL)
    if klass == "master_password":
        abort(401, description="Password MASTER terdeteksi — hubungkan "
                               "hanya dengan password INVESTOR "
                               "(read-only). Akun tidak diaktifkan.")
    if klass == "not_real":
        abort(403, description="Pendaftaran mandiri hanya untuk akun MT5 "
                               "REAL. Akun demo/kontes tidak dapat "
                               "didaftarkan — hubungi admin.")
    abort(503, description="Verifikasi ke server broker tidak berhasil "
                           "dalam batas waktu (terminal MT5 atau server "
                           "broker tidak terjangkau). Akun BELUM "
                           "diaktifkan dan slot tidak terpakai — coba "
                           "lagi nanti.")


def _job_response(conn, coord, job, secret: str | None, *,
                  new_job: bool):
    """Respons standar job aktivasi.

    - connected : self_link/reactivate -> token sesi (SEKALI, klaim
                  atomik); bind -> data akun (sesi sudah ada).
    - failed    : status HTTP sesuai klasifikasi (pesan seragam S2).
    - lainnya   : 202 + state; job_secret hanya dikirim saat job dibuat.
    """
    import auth as authmod
    from services.provisioning import is_final
    state = job["state"]
    if state == "failed" and not is_final(job):
        state = "cleanup"            # gagal, pembersihan masih berjalan
    if state == "failed":
        _abort_for_verify_failure(job["error_class"] or "internal")
    if state != "connected":
        out = {"ok": True, "state": state, "job_id": job["id"],
               "connection_state": "provisioning", "live_verified": False,
               "poll_after_ms": 1000}
        if new_job:
            out["job_secret"] = secret
        return jsonify(out), 202
    acc = conn.execute("SELECT * FROM mt5_accounts WHERE id=?",
                       (job["account_id"],)).fetchone()
    term = conn.execute("SELECT status, lifecycle FROM mt5_terminals"
                        " WHERE customer_id=?",
                        (job["customer_id"],)).fetchone()
    tinfo = {"status": term["status"], "lifecycle": term["lifecycle"]} \
        if term else None
    if job["kind"] == "bind":
        if not coord.claim_token(conn, job["id"]):
            abort(410, description="Hasil job ini sudah diambil")
        out = dict(acc)
        out["terminal"] = tinfo
        return jsonify(out), 201 if job["created_account"] else 200
    if not coord.claim_token(conn, job["id"]):
        abort(410, description="Token untuk aktivasi ini sudah diterbitkan")
    # sync awal dimulai koordinator saat job 'connected' (tidak bergantung
    # pada klien mengambil hasil job)
    token = authmod.issue_token(conn, cfg(), user_id=int(job["user_id"]),
                                customer_id=int(job["customer_id"]),
                                ip=authmod.current_ip())
    mode = "self_link" if job["kind"] == "self_link" else "reactivate"
    return jsonify({"ok": True, "mode": mode, "token": token,
                    "token_type": "bearer", "connection_state": "connected",
                    "live_verified": True,
                    "account": {"login": int(acc["login"]),
                                "server": acc["server"],
                                "currency": acc["currency"]},
                    "terminal": tinfo}), \
        201 if job["kind"] == "self_link" else 200


@api_bp.post("/mt5/connect")
@require_rate_limit("mt5_connect", 10, 300)
def mt5_connect():
    """Self-link / reaktivasi akun MT5 dengan AUTO-PROVISIONING terminal.

    Identitas = nomor akun MT5 + server + password INVESTOR.

    - Akun AKTIF   : password dibandingkan dgn kredensial tersimpan
                     (konstanta-waktu, dalam scope RLS — K1), lalu
                     verifikasi live di terminal customer SENDIRI. Broker
                     menolak -> akun 'credentials_rejected', kredensial
                     dihapus, SEMUA sesi dicabut (Tahap 2). Terminal tidak
                     terjangkau -> sesi 'terminal_pending' (S6, tidak
                     pernah diklaim TERHUBUNG).
    - Akun UNBOUND / CREDENTIALS_REJECTED : reaktivasi dgn password yang
                     dikirim (boleh password BARU) — terbukti hanya lewat
                     verifikasi live broker di job aktivasi.
    - Akun BARU    : customer sintetis + terminal (slot MT5, 403 bila
                     penuh) + job aktivasi; wajib akun REAL (T3).
    Job: install -> start -> health -> verifikasi FAIL-CLOSED; token HANYA
    setelah 'connected'. Respons sinkron bila selesai dalam
    MT5_CONNECT_SYNC_WAIT_SECONDS, selain itu 202 + job_id/job_secret
    untuk polling POST /mt5/connect/status.
    """
    import audit as auditmod
    import auth as authmod
    from security import crypto
    from services.terminals import TerminalCapacityError, TerminalError

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

    ip = authmod.current_ip() or "?"
    acct_key = f"mt5-connect:{login_val}:{server.casefold()}"
    ip_key = f"{acct_key}:{ip}"
    lock_ip = authmod._lockout_for(c)
    lock_acct = authmod._account_lockout_for(c)
    if lock_ip.is_locked(ip_key) or lock_acct.is_locked(acct_key):
        abort(429, description="Terlalu banyak percobaan gagal — coba lagi "
                               "nanti")
    conn = get_db()

    def _fail(reason: str, customer_id=None):
        lock_ip.record_failure(ip_key)
        lock_acct.record_failure(acct_key)
        auditmod.audit(conn, "mt5_connect_gagal", customer_id=customer_id,
                       detail={"login": login_val, "server": server,
                               "reason": reason})

    registry = current_app.extensions["mt5_registry"]
    coord = current_app.extensions["mt5_coordinator"]
    coord.recover_expired(conn)
    existing = _find_account(conn, login_val, server)

    # ======================================== akun terdaftar & AKTIF
    if existing is not None and existing["state"] == "active":
        cust_id = int(existing["customer_id"])
        dbmod.set_customer_context(conn, cust_id)   # K1
        matched = False
        try:
            cred = conn.execute(
                "SELECT enc_password, key_version FROM mt5_credentials"
                " WHERE account_id=?", (int(existing["id"]),)).fetchone()
            if cred is None:
                abort(503, description="Kredensial akun tidak lengkap — "
                                       "hubungi admin")
            stored = crypto.decrypt_password(c, cred["enc_password"],
                                             cred["key_version"])
            matched = hmac.compare_digest(stored.encode("utf-8"),
                                          password.encode("utf-8"))
        except crypto.CredentialCryptoError:
            abort(503, description="Kredensial akun tidak bisa diverifikasi "
                                   "— hubungi admin")
        if not matched:
            _fail("password_salah")
            abort(401, description=GENERIC_VERIFY_FAIL)
        crow = conn.execute("SELECT status FROM customers WHERE id=?",
                            (cust_id,)).fetchone()
        if crow is None or crow["status"] != "active":
            abort(403, description="Customer dinonaktifkan — hubungi admin")
        urow = conn.execute("SELECT id FROM users WHERE customer_id=?",
                            (cust_id,)).fetchone()
        if urow is None:
            abort(503, description="Identitas customer tidak lengkap — "
                                   "hubungi admin")
        term = conn.execute("SELECT id FROM mt5_terminals WHERE"
                            " customer_id=?", (cust_id,)).fetchone()
        spec = coord.spec_for(conn, int(term["id"])) if term else None
        ver = _live_verify_account(
            conn, registry, int(existing["id"]),
            timeout=c.mt5_verify_timeout_seconds,
            enforce_investor_readonly=c.mt5_enforce_investor_readonly,
            expected_login=int(existing["login"]),
            expected_server=existing["server"],
            listener_check=coord.listener_check(spec) if spec else None)
        klass = ver["class"]
        if klass in ("master_password", "auth_failed", "identity_mismatch"):
            _fail({"auth_failed": "broker_auth_failed"}.get(
                klass, _VERIFY_REASON[klass]), customer_id=cust_id)
            # Tahap 2: kredensial tersimpan tidak lagi sah -> akun
            # 'credentials_rejected', kredensial dihapus, SEMUA sesi dicabut
            conn.execute("UPDATE mt5_accounts SET state='credentials_rejected'"
                         " WHERE id=? AND state='active'",
                         (int(existing["id"]),))
            conn.execute("DELETE FROM mt5_credentials WHERE account_id=?",
                         (int(existing["id"]),))
            conn.commit()
            authmod.revoke_all_for_customer(conn, cust_id)
            if klass == "master_password":
                abort(401, description="Password MASTER terdeteksi — "
                                       "hubungkan hanya dengan password "
                                       "INVESTOR (read-only). Hubungi admin "
                                       "bila password investor tidak "
                                       "tersedia.")
            if klass == "auth_failed":
                abort(401, description="Kredensial tidak lagi diterima "
                                       "server broker — kemungkinan password "
                                       "MT5 di broker sudah diubah. Semua "
                                       "sesi dicabut; hubungkan ulang dengan "
                                       "password investor yang baru.")
            abort(401, description=GENERIC_VERIFY_FAIL)
        live_ok = klass == "ok"
        lock_ip.clear(ip_key)
        lock_acct.clear(acct_key)
        token = authmod.issue_token(conn, c, user_id=int(urow["id"]),
                                    customer_id=cust_id,
                                    ip=authmod.current_ip())
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?",
                     (_now_iso(), int(urow["id"])))
        conn.commit()
        detail = {"mode": "login", "server": existing["server"],
                  "live_verified": live_ok}
        if not live_ok:
            detail["verify_class"] = klass     # S6: tanpa klaim TERHUBUNG
        auditmod.audit(conn, "mt5_connect", customer_id=cust_id,
                       user_id=int(urow["id"]),
                       entity=f"mt5_account:{int(existing['id'])}",
                       detail=detail)
        return jsonify({"ok": True, "mode": "login", "token": token,
                        "token_type": "bearer",
                        "connection_state":
                            "connected" if live_ok else "terminal_pending",
                        "live_verified": live_ok,
                        "account": {"login": int(existing["login"]),
                                    "server": existing["server"],
                                    "currency": existing["currency"]}}), 200

    if existing is not None and existing["state"] == "pending":
        abort(409, description="Aktivasi akun ini sedang berjalan — coba "
                               "lagi sebentar")

    try:
        enc, key_version = crypto.encrypt_password(c, password)
    except crypto.CredentialCryptoError as e:
        abort(503, description=str(e))

    # ======================== reaktivasi: unbound / credentials_rejected
    if existing is not None:
        cust_id = int(existing["customer_id"])
        crow = conn.execute("SELECT status FROM customers WHERE id=?",
                            (cust_id,)).fetchone()
        urow = conn.execute("SELECT id FROM users WHERE customer_id=?",
                            (cust_id,)).fetchone()
        if crow is None or crow["status"] != "active" or urow is None:
            abort(403, description="Customer dinonaktifkan — hubungi admin")
        prev_state = existing["state"]
        if not _claim_pending(conn, int(existing["id"]), prev_state):
            abort(409, description="Aktivasi akun ini sedang berjalan")
        try:
            term = registry.provision_terminal(conn, cust_id)
        except TerminalCapacityError as e:
            _restore_state(conn, existing, prev_state)
            abort(403, description=str(e))
        except TerminalError:
            _restore_state(conn, existing, prev_state)
            abort(409, description="Provisioning terminal MT5 gagal")
        _store_credentials(conn, cust_id, int(existing["id"]), enc,
                           key_version)
        job_id, secret = coord.create_job(
            conn, kind="reactivate", customer_id=cust_id,
            user_id=int(urow["id"]), account_id=int(existing["id"]),
            terminal=term, created_customer=False, created_account=False,
            prev_account_state=prev_state, require_real=False,
            lock_ip_hash=authmod._key_hash(ip_key),
            lock_acct_hash=authmod._key_hash(acct_key))
        coord.start(job_id)
        job = coord.wait(conn, job_id, c.mt5_connect_sync_wait_seconds)
        auditmod.audit(conn, "mt5_connect_job", customer_id=cust_id,
                       detail={"kind": "reactivate", "state": job["state"]})
        return _job_response(conn, coord, job, secret, new_job=True)

    # ================================================== akun BARU
    if not _server_allowed(c, server):
        _fail("server_tidak_diizinkan")
        abort(401, description=GENERIC_VERIFY_FAIL)
    synthetic_email = _synthetic_email(login_val, server)
    idrow = conn.execute(
        """SELECT u.id AS user_id, u.customer_id, c.status AS customer_status,
                  (SELECT COUNT(*) FROM mt5_accounts a
                     WHERE a.customer_id = u.customer_id) AS bound_accounts
           FROM users u JOIN customers c ON c.id = u.customer_id
           WHERE u.email=?""", (synthetic_email,)).fetchone()
    created_customer = False
    if idrow is not None:
        if idrow["customer_status"] != "active":
            abort(403, description="Customer dinonaktifkan — hubungi admin")
        if int(idrow["bound_accounts"]) > 0:
            _fail("identitas_sudah_terikat")
            abort(401, description=GENERIC_VERIFY_FAIL)
        cust_id, user_id = int(idrow["customer_id"]), int(idrow["user_id"])
    else:
        try:
            created = authmod.register_customer(
                conn, c, synthetic_email, secrets.token_urlsafe(32), None,
                skip_password_validation=True, enforce_portal_cap=False)
            conn.commit()
        except authmod.AuthError as e:
            abort(e.code, description=e.message)
        cust_id, user_id = created["customer_id"], created["user_id"]
        created_customer = True

    def _drop_customer():
        if created_customer:
            try:
                conn.rollback()
                if conn.execute(
                        "SELECT (SELECT COUNT(*) FROM mt5_accounts WHERE"
                        " customer_id=?) + (SELECT COUNT(*) FROM"
                        " mt5_terminals WHERE customer_id=?) AS n",
                        (cust_id, cust_id)).fetchone()["n"] == 0:
                    conn.execute("DELETE FROM customers WHERE id=?",
                                 (cust_id,))
                conn.commit()
            except Exception:  # noqa: BLE001
                conn.rollback()

    try:
        term = registry.provision_terminal(conn, cust_id)
    except TerminalCapacityError as e:
        _drop_customer()
        auditmod.audit(conn, "mt5_connect_gagal",
                       detail={"login": login_val, "server": server,
                               "reason": "slot_mt5_penuh"})
        abort(403, description=str(e))
    except TerminalError:
        _drop_customer()
        abort(409, description="Provisioning terminal MT5 gagal — alokasi "
                               "port bentrok")
    try:
        acc_id = _insert_account(conn, cust_id, login_val, server, enc,
                                 key_version, commit=False)
    except dbmod.INTEGRITY_ERRORS as e:
        _discard_unstarted(conn, term, cust_id)
        _drop_customer()
        log.warning("mt5_connect bentrok (UNIQUE) login=%s: %s",
                    login_val, type(e).__name__)
        abort(409, description="Akun MT5 itu baru saja terdaftar — coba "
                               "hubungkan lagi dengan password investor "
                               "yang benar")
    job_id, secret = coord.create_job(
        conn, kind="self_link", customer_id=cust_id, user_id=user_id,
        account_id=acc_id, terminal=term, created_customer=created_customer,
        created_account=True, prev_account_state=None,
        require_real=getattr(c, "mt5_self_link_require_real", True),
        lock_ip_hash=authmod._key_hash(ip_key),
        lock_acct_hash=authmod._key_hash(acct_key))
    coord.start(job_id)
    job = coord.wait(conn, job_id, c.mt5_connect_sync_wait_seconds)
    auditmod.audit(conn, "mt5_connect_job",
                   customer_id=cust_id if job["state"] != "failed" else None,
                   detail={"kind": "self_link", "state": job["state"],
                           "reason": _VERIFY_REASON.get(
                               job["error_class"] or "", job["error_class"])})
    return _job_response(conn, coord, job, secret, new_job=True)


@api_bp.post("/mt5/connect/status")
@require_rate_limit("mt5_connect_status", 120, 300)
def mt5_connect_status():
    """Polling job aktivasi: body {"job_id", "job_secret"}.

    job_secret (32 byte acak, disimpan sebagai SHA-256) adalah kapabilitas
    — tanpanya status tidak dapat dibaca. Token sesi diterbitkan SEKALI
    saat job 'connected' (klaim atomik); polling berikutnya 410.
    """
    body = request.get_json(silent=True) or {}
    job_id = str(body.get("job_id") or "")[:64]
    secret = str(body.get("job_secret") or "")[:128]
    conn = get_db()
    coord = current_app.extensions["mt5_coordinator"]
    coord.recover_expired(conn)
    job = coord.load(conn, job_id) if job_id else None
    if not coord.check_secret(job, secret):
        abort(404, description="Job tidak ditemukan")
    return _job_response(conn, coord, job, None, new_job=False)


def _server_allowed(c, server: str) -> bool:
    """Allowlist server self-link (T3). Kosong = semua server (slot tetap
    dijaga syarat akun REAL). Pola fnmatch case-insensitive."""
    import fnmatch
    pats = [p for p in (getattr(c, "mt5_self_link_server_allowlist", ())
                        or ()) if p]
    if not pats:
        return True
    s = server.strip().casefold()
    return any(fnmatch.fnmatchcase(s, p.strip().casefold()) for p in pats)


@api_bp.post("/mt5/disconnect")
@require_customer
def mt5_disconnect():
    """PUTUSKAN KONEKSI (D3): logout + cabut token sesi.

    Default: hanya token sesi INI. Body {"all": true}: cabut SEMUA sesi
    customer. Riwayat jurnal, akun, kredensial, terminal TIDAK disentuh.
    """
    import auth as authmod
    body = request.get_json(silent=True) or {}
    if body.get("all") is True:
        authmod.revoke_all_for_customer(get_db(), g.customer_id)
        _audit("mt5_disconnect", detail={"scope": "all"})
        return jsonify({"ok": True, "revoked": "all"})
    authmod.revoke_token(get_db(), g.token_id)
    _audit("mt5_disconnect")
    return jsonify({"ok": True, "revoked": "current"})


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
        # Remediasi R1: hanya status — port RPyC/unit systemd internal.
        status["terminal"] = {"status": term["status"],
                              "lifecycle": term["lifecycle"]}
    if acc is not None and acc["state"] != "active":
        status["reason"] = f"akun_{acc['state']}"
    if c.mt5_enabled and acc is not None and acc["state"] == "active":
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
    if acc["state"] != "active":
        abort(409, description="Akun MT5 tidak aktif (unbind / kredensial "
                               "ditolak broker) — hubungkan ulang dulu")
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
                  (SELECT login FROM mt5_accounts a WHERE a.customer_id=c.id
                     AND a.state='active') AS bound_login
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


@api_bp.post("/admin/customers/<int:customer_id>/revoke-sessions")
def admin_revoke_sessions(customer_id: int):
    """Pencabutan akses oleh admin (Tahap 2): cabut SEMUA sesi customer.

    Riwayat jurnal & akun tidak disentuh. Audit tidak menyimpan token.
    """
    if not _admin_token_ok():
        abort(401, description="Token admin tidak valid")
    import audit as auditmod
    import auth as authmod
    conn = get_db()
    if conn.execute("SELECT 1 FROM customers WHERE id=?",
                    (customer_id,)).fetchone() is None:
        abort(404, description="Customer tidak ditemukan")
    n = conn.execute("SELECT COUNT(*) AS n FROM auth_tokens WHERE"
                     " customer_id=? AND revoked_at IS NULL",
                     (customer_id,)).fetchone()["n"]
    authmod.revoke_all_for_customer(conn, customer_id)
    auditmod.audit(conn, "admin_revoke_sessions", customer_id=customer_id,
                   detail={"revoked": int(n)})
    return jsonify({"ok": True, "revoked": int(n)})


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
