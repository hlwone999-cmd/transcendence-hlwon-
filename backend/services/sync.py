"""Sinkronisasi riwayat MT5 -> database jurnal.

Alur setiap sync (READ-ONLY terhadap terminal):
1. connect provider -> account_info -> upsert mt5_accounts
2. tentukan rentang waktu:
   - initial  : now - MT5_HISTORY_INITIAL_DAYS .. now
   - increment: last_deal_time - MT5_SYNC_OVERLAP_SECONDS .. now  (overlap
     aman; dedup oleh UNIQUE(account_id, ticket) sehingga tidak duplikat)
   - full     : 2000-01-01 .. now
3. history_deals_get + history_orders_get -> UPSERT raw (data historis
   tidak pernah dihapus)
4. rekonstruksi ulang trades dari SELURUH deals akun (idempoten) —
   partial close/scale-in/out/netting selalu konsisten
5. positions_get -> perbarui SL/TP/harga berjalan untuk trade terbuka
6. symbol_info per simbol -> cache definisi pip (mt5_symbols)
7. catat sync_logs + sync_state; error ditangkap & dilog, TIDAK crash.

Password TIDAK PERNAH ditulis ke log maupun database.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta, timezone

import db as dbmod
from mt5client.provider import MT5ConnectionError, MT5Error, utcnow_iso
from services.reconstruct import TRADING_DEAL_TYPES, reconstruct_trades

log = logging.getLogger(__name__)

DEAL_TYPE_NAMES = {
    0: "buy", 1: "sell", 2: "balance", 3: "credit", 4: "charge",
    5: "correction", 6: "bonus", 7: "commission", 8: "commission_daily",
    9: "commission_monthly", 11: "sell_canceled", 12: "buy_canceled",
    13: "dividend", 14: "dividend_franked", 15: "tax",
}
ENTRY_NAMES = {0: "in", 1: "out", 2: "inout", 3: "out_by"}
ORDER_TYPE_NAMES = {0: "buy", 1: "sell", 2: "buy_limit", 3: "sell_limit",
                    4: "buy_stop", 5: "sell_stop", 6: "buy_stop_limit",
                    7: "sell_stop_limit", 8: "close_by"}
ORDER_STATE_NAMES = {0: "started", 1: "placed", 2: "canceled", 3: "partial",
                     4: "filled", 5: "rejected", 6: "expired"}


def _iso_epoch(sec) -> str | None:
    if not sec:
        return None
    return datetime.fromtimestamp(int(sec), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_to_epoch(s: str | None) -> int:
    if not s:
        return 0
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _epoch_of(v) -> int | None:
    """Terima int epoch (detik) atau datetime -> int epoch UTC."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp())
    return None


class SyncLockBusy(Exception):
    pass


class SyncService:
    def __init__(self, config, provider_factory):
        self._cfg = config
        self._factory = provider_factory
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ API
    def sync(self, mode: str = "incremental", full: bool = False,
             days: int | None = None, conn=None) -> dict:
        """Jalankan satu siklus sync. mode: initial|incremental|full|manual."""
        if not self._lock.acquire(blocking=False):
            raise SyncLockBusy("sync lain sedang berjalan")
        started = utcnow_iso()
        t0 = _time.monotonic()
        provider = None
        summary = {"status": "ok", "mode": mode, "started_at": started,
                   "deals_upserted": 0, "orders_upserted": 0,
                   "trades_updated": 0, "open_positions": 0}
        try:
            own_conn = conn is None
            conn = conn or dbmod.get_db(self._cfg)
            provider = self._factory()
            provider.connect()

            summary.update(self._do_sync(conn, provider, mode, full, days))
            summary["duration_ms"] = int((_time.monotonic() - t0) * 1000)
            self._log_run(conn, summary, error=None)
            return summary
        except SyncLockBusy:
            raise
        except Exception as e:  # noqa: BLE001 — sync tidak boleh crash-kan backend
            msg = f"{type(e).__name__}: {e}"
            # JANGAN pernah masukkan nilai env/argumen (bisa mengandung password)
            summary["status"] = "error"
            summary["error"] = msg
            summary["duration_ms"] = int((_time.monotonic() - t0) * 1000)
            try:
                self._log_run(conn, summary, error=msg)  # type: ignore[possibly-undefined]
            except Exception:  # pragma: no cover
                log.exception("gagal menulis sync log")
            return summary
        finally:
            if provider is not None:
                try:
                    provider.disconnect()
                except Exception:  # pragma: no cover
                    pass
            if self._lock.locked():
                self._lock.release()

    # -------------------------------------------------------------- internal
    def _do_sync(self, conn, provider, mode, full: bool, days: int | None) -> dict:
        state = self._get_state(conn)
        now = datetime.now(timezone.utc)

        acc = provider.account_info()
        account_id = self._upsert_account(conn, acc, now)

        if mode == "full" or full:
            range_from = datetime(2000, 1, 1, tzinfo=timezone.utc)
        elif state and state["last_deal_time_utc"]:
            last = _parse_iso(state["last_deal_time_utc"]) or now
            range_from = last - timedelta(seconds=self._cfg.mt5_sync_overlap_seconds)
        elif days:
            range_from = now - timedelta(days=max(1, int(days)))
        else:
            range_from = now - timedelta(days=self._cfg.mt5_history_initial_days)
        range_to = now + timedelta(minutes=5)

        deals = provider.history_deals_get(range_from, range_to) or []
        orders = provider.history_orders_get(range_from, range_to) or []

        deals_upserted = self._upsert_deals(conn, account_id, deals)
        orders_upserted = self._upsert_orders(conn, account_id, orders)

        trades_updated = self._rebuild_trades(conn, account_id, provider)
        open_positions = self._refresh_open(conn, account_id, provider)
        self._cache_symbols(conn, account_id, provider)
        self._save_state(conn, account_id, now, "ok")

        conn.commit()
        return {
            "account_id": account_id,
            "range_from_utc": range_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "range_to_utc": range_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "deals_upserted": deals_upserted,
            "orders_upserted": orders_upserted,
            "trades_updated": trades_updated,
            "open_positions": open_positions,
        }

    def _get_state(self, conn):
        return conn.execute(
            "SELECT * FROM sync_state WHERE account_id = ("
            "  SELECT id FROM mt5_accounts ORDER BY last_sync_at DESC LIMIT 1)"
        ).fetchone()

    def _upsert_account(self, conn, acc: dict, now: str) -> int:
        login = int(acc.get("login") or 0)
        server = str(acc.get("server") or "")
        if not login or not server:
            raise MT5Error("account_info tidak valid (login/server kosong)")
        conn.execute(
            """INSERT INTO mt5_accounts (login, server, company, currency,
                 leverage, balance, equity, margin_free, first_sync_at, last_sync_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(login, server) DO UPDATE SET
                 company=excluded.company, currency=excluded.currency,
                 leverage=excluded.leverage, balance=excluded.balance,
                 equity=excluded.equity, margin_free=excluded.margin_free,
                 last_sync_at=excluded.last_sync_at""",
            (login, server, acc.get("company"), acc.get("currency"),
             acc.get("leverage"), acc.get("balance"), acc.get("equity"),
             acc.get("margin_free"), now, now),
        )
        row = conn.execute(
            "SELECT id FROM mt5_accounts WHERE login=? AND server=?",
            (login, server)).fetchone()
        return int(row["id"])

    def _upsert_deals(self, conn, account_id: int, deals: list[dict]) -> int:
        now = utcnow_iso()
        n = 0
        for d in deals:
            d = dict(d)
            sec = _epoch_of(d.get("time"))
            if sec is None:
                continue  # deal tanpa waktu tidak bisa dipercaya
            conn.execute(
                """INSERT INTO mt5_deals (account_id, ticket, order_ticket,
                     position_id, symbol, deal_type, type_name, entry, entry_name,
                     volume, price, profit, commission, swap, fee, comment,
                     magic, reason, time_utc, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, ticket) DO UPDATE SET
                     order_ticket=excluded.order_ticket,
                     position_id=excluded.position_id,
                     symbol=excluded.symbol, deal_type=excluded.deal_type,
                     type_name=excluded.type_name, entry=excluded.entry,
                     entry_name=excluded.entry_name, volume=excluded.volume,
                     price=excluded.price, profit=excluded.profit,
                     commission=excluded.commission, swap=excluded.swap,
                     fee=excluded.fee, comment=excluded.comment,
                     magic=excluded.magic, reason=excluded.reason,
                     time_utc=excluded.time_utc, synced_at=excluded.synced_at""",
                (account_id, int(d.get("ticket") or 0), d.get("order"),
                 d.get("position_id"), d.get("symbol"), int(d.get("type") or 0),
                 DEAL_TYPE_NAMES.get(int(d.get("type") or 0)),
                 int(d.get("entry") or 0),
                 ENTRY_NAMES.get(int(d.get("entry") or 0)),
                 d.get("volume"), d.get("price"), d.get("profit"),
                 d.get("commission"), d.get("swap"), d.get("fee"),
                 str(d.get("comment") or ""), d.get("magic"), d.get("reason"),
                 _iso_epoch(sec), now),
            )
            n += 1
        return n

    def _upsert_orders(self, conn, account_id: int, orders: list[dict]) -> int:
        now = utcnow_iso()
        n = 0
        for o in orders:
            o = dict(o)
            ticket = o.get("ticket")
            if ticket is None:
                continue
            conn.execute(
                """INSERT INTO mt5_orders (account_id, ticket, position_id,
                     symbol, order_type, type_name, volume_initial,
                     volume_current, price_open, price_sl, price_tp,
                     state, state_name, time_setup_utc, time_done_utc,
                     comment, magic, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, ticket) DO UPDATE SET
                     position_id=excluded.position_id, symbol=excluded.symbol,
                     order_type=excluded.order_type, type_name=excluded.type_name,
                     volume_initial=excluded.volume_initial,
                     volume_current=excluded.volume_current,
                     price_open=excluded.price_open, price_sl=excluded.price_sl,
                     price_tp=excluded.price_tp, state=excluded.state,
                     state_name=excluded.state_name,
                     time_setup_utc=excluded.time_setup_utc,
                     time_done_utc=excluded.time_done_utc,
                     comment=excluded.comment, magic=excluded.magic,
                     synced_at=excluded.synced_at""",
                (account_id, int(ticket), o.get("position_id"), o.get("symbol"),
                 o.get("type"), ORDER_TYPE_NAMES.get(int(o.get("type") or 0)),
                 o.get("volume_initial"), o.get("volume_current"),
                 o.get("price_open"), o.get("sl"), o.get("tp"),
                 o.get("state"), ORDER_STATE_NAMES.get(int(o.get("state") or 0)),
                 _iso_epoch(o.get("time_setup")), _iso_epoch(o.get("time_done")),
                 str(o.get("comment") or ""), o.get("magic"), now),
            )
            n += 1
        return n

    def _rebuild_trades(self, conn, account_id: int, provider) -> int:
        """Rekonstruksi penuh dari semua deals akun (idempoten, tanpa duplikat)."""
        deals = [dict(r) for r in conn.execute(
            "SELECT * FROM mt5_deals WHERE account_id=?", (account_id,))]
        orders = [dict(r) for r in conn.execute(
            "SELECT * FROM mt5_orders WHERE account_id=?", (account_id,))]
        # kolom internal -> bentuk yang dimakan reconstruct (waktu = epoch int)
        for d in deals:
            d["type"] = d["deal_type"]
            d["time"] = _iso_to_epoch(d["time_utc"])
        for o in orders:
            o["type"] = o["order_type"]
            o["sl"] = o["price_sl"]
            o["tp"] = o["price_tp"]
            o["time"] = _iso_to_epoch(o["time_setup_utc"])
        trades = reconstruct_trades(account_id, deals, orders)
        now = utcnow_iso()
        kept_pids = set()
        for t in trades:
            kept_pids.add(t["position_id"])
            conn.execute(
                """INSERT INTO trades (account_id, position_id, symbol,
                     direction, status, volume_in, volume_out, entry_price,
                     exit_price, open_time_utc, close_time_utc, profit,
                     commission, swap, fee, net_profit, sl_initial, tp_initial,
                     exit_reason, magic, comment, entry_deals, exit_deals,
                     updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, position_id) DO UPDATE SET
                     symbol=excluded.symbol, direction=excluded.direction,
                     status=excluded.status, volume_in=excluded.volume_in,
                     volume_out=excluded.volume_out,
                     entry_price=excluded.entry_price,
                     exit_price=excluded.exit_price,
                     open_time_utc=excluded.open_time_utc,
                     close_time_utc=excluded.close_time_utc,
                     profit=excluded.profit, commission=excluded.commission,
                     swap=excluded.swap, fee=excluded.fee,
                     net_profit=excluded.net_profit,
                     sl_initial=excluded.sl_initial, tp_initial=excluded.tp_initial,
                     exit_reason=excluded.exit_reason, magic=excluded.magic,
                     comment=excluded.comment, entry_deals=excluded.entry_deals,
                     exit_deals=excluded.exit_deals, updated_at=excluded.updated_at""",
                (account_id, t["position_id"], t["symbol"], t["direction"],
                 t["status"], t["volume_in"], t["volume_out"], t["entry_price"],
                 t["exit_price"], t["open_time_utc"], t["close_time_utc"],
                 t["profit"], t["commission"], t["swap"], t["fee"],
                 t["net_profit"], t["sl_initial"], t["tp_initial"],
                 t["exit_reason"], t["magic"], t["comment"],
                 ",".join(map(str, t["entry_deals"])),
                 ",".join(map(str, t["exit_deals"])), now),
            )
        # trade yang deals-nya lenyap total dari raw (harusnya jarang) -> bersihkan
        conn.execute(
            "DELETE FROM trades WHERE account_id=? AND position_id NOT IN ("
            " SELECT DISTINCT position_id FROM mt5_deals WHERE account_id=?"
            " AND deal_type IN (0,1))", (account_id, account_id))
        return len(trades)

    def _refresh_open(self, conn, account_id: int, provider) -> int:
        """positions_get: perbarui data berjalan posisi terbuka (bukan trade baru)."""
        try:
            positions = provider.positions_get() or []
        except MT5Error:
            return 0
        now = utcnow_iso()
        for p in positions:
            p = dict(p)
            pid = p.get("identifier") or p.get("ticket")
            if pid is None:
                continue
            conn.execute(
                """UPDATE trades SET sl_current=?, tp_current=?,
                     price_current=?, updated_at=?
                   WHERE account_id=? AND position_id=? AND status='open'""",
                (p.get("sl"), p.get("tp"), p.get("price_current"), now,
                 account_id, pid),
            )
        return len(positions)

    def _cache_symbols(self, conn, account_id: int, provider) -> None:
        symbols = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM mt5_deals WHERE account_id=?"
            " AND symbol IS NOT NULL AND symbol<>''", (account_id,))]
        now = utcnow_iso()
        for sym in symbols:
            try:
                info = provider.symbol_info(sym)
            except MT5Error:
                continue
            if not info:
                continue
            info = dict(info)
            conn.execute(
                """INSERT INTO mt5_symbols (account_id, symbol, point, digits,
                     trade_tick_size, trade_tick_value, pip_size, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, symbol) DO UPDATE SET
                     point=excluded.point, digits=excluded.digits,
                     trade_tick_size=excluded.trade_tick_size,
                     trade_tick_value=excluded.trade_tick_value,
                     pip_size=excluded.pip_size, updated_at=excluded.updated_at""",
                (account_id, sym, info.get("point"), info.get("digits"),
                 info.get("trade_tick_size"), info.get("trade_tick_value"),
                 self._cfg.pip_size_for(sym, info.get("point"), info.get("digits")),
                 now),
            )

    def _save_state(self, conn, account_id: int, now: datetime, status: str) -> None:
        row = conn.execute(
            "SELECT MAX(time_utc) AS m FROM mt5_deals WHERE account_id=?"
            " AND deal_type IN (0,1)", (account_id,)).fetchone()
        last_deal = row["m"] if row else None
        conn.execute(
            """INSERT INTO sync_state (account_id, initial_done,
                 last_deal_time_utc, last_sync_started_at, last_sync_finished_at,
                 last_sync_status)
               VALUES (?,1,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET
                 initial_done=1, last_deal_time_utc=excluded.last_deal_time_utc,
                 last_sync_started_at=excluded.last_sync_started_at,
                 last_sync_finished_at=excluded.last_sync_finished_at,
                 last_sync_status=excluded.last_sync_status""",
            (account_id, last_deal, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             utcnow_iso(), status),
        )

    def _log_run(self, conn, summary: dict, error: str | None) -> None:
        try:
            conn.execute(
                """INSERT INTO sync_logs (account_id, mode, started_at,
                     finished_at, status, deals_upserted, orders_upserted,
                     trades_updated, open_positions, range_from_utc,
                     range_to_utc, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (summary.get("account_id"), summary.get("mode"),
                 summary.get("started_at"), utcnow_iso(), summary["status"],
                 summary.get("deals_upserted"), summary.get("orders_upserted"),
                 summary.get("trades_updated"), summary.get("open_positions"),
                 summary.get("range_from_utc"), summary.get("range_to_utc"),
                 error),
            )
            conn.commit()
        except Exception:  # pragma: no cover
            log.exception("gagal menulis sync_logs")
