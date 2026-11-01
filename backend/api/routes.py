"""Blueprint API Jurnal MT5 (url_prefix /api/v1).

Autentikasi: header X-API-Token dibandingkan (constant-time) dengan env
API_TOKEN. Jika API_TOKEN kosong -> mode dev tanpa auth (diperingatkan
di log; WAJIB diisi di produksi).

Semua endpoint gagal-aman: kalau MT5/terminal tidak tersedia, tetap
balas JSON status disconnected — tidak pernah crash.
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, abort, current_app, g, jsonify, request

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


def require_token(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        expected = cfg().api_token
        if expected:
            got = request.headers.get("X-API-Token", "")
            if not got or not hmac.compare_digest(got, expected):
                abort(401, description="Token API tidak valid atau kosong")
        return fn(*a, **kw)
    return wrapper


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
        abort(400, description=f"Parameter '{name}' harus salah satu: {', '.join(allowed)}")
    return value


def _json_filters(end_of_day_to: bool = True):
    args = request.args
    f_from = _parse_date("from", args.get("from", "2000-01-01")) if "from" in args else None
    f_to = None
    if "to" in args:
        f_to = _parse_date("to", args["to"], end_of_day=end_of_day_to)
    symbol = (args.get("symbol") or "").strip()[:32] or None
    return f_from, f_to, symbol


def _account_id(conn) -> int:
    row = conn.execute(
        "SELECT id FROM mt5_accounts ORDER BY last_sync_at DESC LIMIT 1").fetchone()
    if row is None:
        abort(404, description="Belum ada akun MT5 tersinkron. Jalankan sync dulu.")
    return int(row["id"])


# ------------------------------------------------------------------ health
@api_bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "jurnal-mt5", "time": _now_iso()})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------- MT5
@api_bp.get("/mt5/status")
@require_token
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
    if c.mt5_enabled:
        try:
            factory = current_app.extensions["mt5_provider_factory"]
            p = factory()
            p.connect()
            acc = p.account_info()
            p.disconnect()
            status["connected"] = True
            status["reason"] = None
            status["live_account"] = {
                "login": acc.get("login"), "server": acc.get("server"),
                "currency": acc.get("currency"), "balance": acc.get("balance"),
                "equity": acc.get("equity"),
            }
        except MT5Error as e:
            status["reason"] = str(e)
    state = conn.execute(
        "SELECT * FROM sync_state ORDER BY last_sync_finished_at DESC LIMIT 1"
    ).fetchone()
    last_log = conn.execute(
        "SELECT * FROM sync_logs ORDER BY id DESC LIMIT 1").fetchone()
    status["last_sync"] = dict(state) if state else None
    status["last_run"] = dict(last_log) if last_log else None
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM trades) AS trades,"
        " (SELECT COUNT(*) FROM mt5_deals) AS deals").fetchone()
    status["stored"] = {"trades": counts["trades"], "deals": counts["deals"]}
    return jsonify(status)


@api_bp.get("/mt5/account")
@require_token
def mt5_account():
    row = get_db().execute(
        "SELECT * FROM mt5_accounts ORDER BY last_sync_at DESC LIMIT 1").fetchone()
    if row is None:
        abort(404, description="Belum ada akun tersinkron")
    return jsonify(dict(row))


@api_bp.post("/mt5/sync")
@require_token
def mt5_sync():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "incremental")
    _parse_enum("mode", mode, ("incremental", "initial", "full", "manual"))
    full = bool(body.get("full", False))
    days = None
    if body.get("days") is not None:
        days = _parse_int("days", body.get("days"), 1, 3650)
    svc = current_app.extensions["mt5_sync"]
    try:
        summary = svc.sync(mode=mode, full=full, days=days)
    except Exception as e:  # SyncLockBusy dari thread lain
        if "berjalan" in str(e):
            abort(409, description="Sync lain sedang berjalan, coba lagi nanti")
        raise
    return jsonify(summary), 200


@api_bp.get("/mt5/sync/status")
@require_token
def mt5_sync_status():
    conn = get_db()
    c = cfg()
    state = conn.execute(
        "SELECT * FROM sync_state ORDER BY last_sync_finished_at DESC LIMIT 1"
    ).fetchone()
    logs = [dict(r) for r in conn.execute(
        "SELECT * FROM sync_logs ORDER BY id DESC LIMIT 20")]
    sched = current_app.extensions.get("mt5_scheduler")
    return jsonify({
        "state": dict(state) if state else None,
        "recent_logs": logs,
        "scheduler": sched.state() if sched else None,
        "interval_seconds": c.mt5_sync_interval_seconds,
    })


# ---------------------------------------------------------------- journal
@api_bp.get("/journal/trades")
@require_token
def journal_trades():
    conn = get_db()
    account_id = _account_id(conn)
    f_from, f_to, symbol = _json_filters()
    status = request.args.get("status", "all")
    _parse_enum("status", status, ("all", "open", "closed"))
    direction = request.args.get("direction", "all")
    _parse_enum("direction", direction, ("all", "buy", "sell"))
    sort = request.args.get("sort", "close_time")
    _parse_enum("sort", sort, ("close_time", "open_time", "net_profit"))
    order = request.args.get("order", "desc")
    _parse_enum("order", order, ("asc", "desc"))
    page = _parse_int("page", request.args.get("page", 1), 1, 10_000)
    page_size = _parse_int("page_size", request.args.get("page_size", 50), 1, 200)

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
        where += " AND COALESCE(close_time_utc, open_time_utc) <= ?"
        args.append(f_to)
    order_col = {"close_time": "COALESCE(close_time_utc, open_time_utc)",
                 "open_time": "open_time_utc",
                 "net_profit": "net_profit"}[sort]

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM trades WHERE {where}", args).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM trades WHERE {where}"
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
    })


@api_bp.get("/journal/trades/<int:position_id>")
@require_token
def journal_trade_detail(position_id: int):
    conn = get_db()
    account_id = _account_id(conn)
    row = conn.execute(
        "SELECT * FROM trades WHERE account_id=? AND position_id=?",
        (account_id, position_id)).fetchone()
    if row is None:
        abort(404, description=f"Trade position_id={position_id} tidak ditemukan")
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
@require_token
def journal_stats():
    conn = get_db()
    account_id = _account_id(conn)
    f_from, f_to, symbol = _json_filters()
    stats = analytics.compute_stats(conn, account_id, cfg().symbol_config,
                                    f_from, f_to, symbol)
    return jsonify(stats)


@api_bp.get("/journal/equity-curve")
@require_token
def journal_equity_curve():
    conn = get_db()
    account_id = _account_id(conn)
    f_from, f_to, symbol = _json_filters()
    curve = analytics.equity_curve(conn, account_id, cfg().symbol_config,
                                   f_from, f_to, symbol)
    return jsonify(curve)


@api_bp.get("/symbols")
@require_token
def symbols():
    conn = get_db()
    account_id = _account_id(conn)
    rows = conn.execute(
        "SELECT symbol, point, digits, pip_size FROM mt5_symbols"
        " WHERE account_id=? ORDER BY symbol", (account_id,)).fetchall()
    return jsonify({"items": [dict(r) for r in rows]})


# ------------------------------------------------------------- error json
@api_bp.errorhandler(400)
@api_bp.app_errorhandler(400)
def bad_request(e):  # pragma: no cover - dipakai via abort
    return jsonify({"error": {"code": 400, "message": e.description}}), 400


@api_bp.errorhandler(401)
def unauthorized(e):  # pragma: no cover
    return jsonify({"error": {"code": 401, "message": e.description}}), 401


@api_bp.app_errorhandler(404)
def not_found(e):
    return jsonify({"error": {"code": 404, "message": getattr(e, "description", "tidak ditemukan")}}), 404


@api_bp.app_errorhandler(409)
def conflict(e):
    return jsonify({"error": {"code": 409, "message": e.description}}), 409


@api_bp.app_errorhandler(500)
def server_error(e):  # pragma: no cover
    log.exception("500 di %s", request.path)
    return jsonify({"error": {"code": 500, "message": "kesalahan internal"}}), 500
