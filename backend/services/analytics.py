"""Analitik jurnal trading dari tabel `trades` (+ ops balance utk equity).

Aturan penting:
- Statistik hanya dari trade CLOSED. Deposit/withdrawal (mt5_deals
  deal_type=BALANCE) TIDAK dihitung menang/kalah — hanya muncul sebagai
  langkah kurva saldo.
- pip per simbol dari definisi aplikasi: override SYMBOL_CONFIG ->
  symbol_info -> 10*point. pip_distance = abs(exit-entry)/pip_size.
- RR JUJUR: kalau SL awal tidak ada -> rr = None (tampil "N/A").

SEMANTIK INTERVAL WAKTU (harmonis dengan api/routes.py):
- Semua perbandingan memakai string ISO UTC yang tersimpan di DB
  ("YYYY-MM-DDTHH:MM:SSZ") — pembandingan leksikografis valid.
- `date_from` selalu INKLUSIF:  close_time_utc >= date_from.
- `date_to` default INKLUSIF:  close_time_utc <= date_to.
  Jika `to_exclusive=True`:    close_time_utc <  date_to  → interval
  efektif [date_from, date_to) seperti kontrak API.
- ATRIBUSI PERIODE: trade dihitung penuh di periode tanggal PENUTUP-nya
  (close_time_utc). Trade yang membuka bulan lalu dan menutup bulan ini
  masuk bulan ini — sama seperti atribusi "Positions" di MT5 yang
  mengelompokkan per tanggal penutupan. Tidak ada pembagian pro-rata.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import db as dbmod


def _dt_of(s: str | None):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_symbol_meta(conn, account_id: int) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT * FROM mt5_symbols WHERE account_id=?", (account_id,)):
        meta[r["symbol"]] = dict(r)
    return meta


def enrich_trade(t: dict, symbol_meta: dict, symbol_cfg: dict) -> dict:
    """Tambah metrik turunan per trade: pips & RR (tanpa mengarang SL)."""
    out = dict(t)
    sym = t.get("symbol") or ""
    meta = symbol_meta.get(sym) or {}
    pip_size = symbol_cfg.get(sym, {}).get("pip_size") or meta.get("pip_size")
    entry, exitp = t.get("entry_price"), t.get("exit_price")

    out["pip_size"] = pip_size
    out["pip_distance"] = None
    out["pips"] = None
    if pip_size and entry is not None and exitp is not None:
        out["pip_distance"] = round(abs(exitp - entry) / pip_size, 2)
        sign = 1.0 if (t.get("direction") == "buy") else -1.0
        out["pips"] = round(sign * (exitp - entry) / pip_size, 2)

    out["rr"] = None  # None => tampil "N/A" di aplikasi
    sl = t.get("sl_initial")
    if sl and entry is not None and abs(entry - sl) > 1e-12 and out["pips"] is not None:
        risk_pips = abs(entry - sl) / pip_size
        if risk_pips > 0:
            out["rr"] = round(abs(out["pips"]) / risk_pips, 2)
    return out


def _bucket_keys(iso: str) -> dict[str, str]:
    dt = _dt_of(iso)
    if not dt:
        return {}
    iso_yw = dt.strftime("%G-W%V")  # minggu ISO, Senin mulai
    return {"daily": iso[:10], "weekly": iso_yw, "monthly": iso[:7]}


def _empty_group() -> dict:
    return {"trades": 0, "wins": 0, "losses": 0, "net_profit": 0.0, "volume": 0.0}


def compute_stats(conn, account_id: int, symbol_cfg: dict,
                  date_from: str | None = None, date_to: str | None = None,
                  symbol: str | None = None,
                  to_exclusive: bool = False) -> dict:
    meta = load_symbol_meta(conn, account_id)
    tt = dbmod.trades_table(conn)

    q = (f"SELECT * FROM {tt} WHERE account_id=? AND status='closed'")
    args = [account_id]
    if date_from:
        q += " AND close_time_utc >= ?"
        args.append(date_from)
    if date_to:
        q += " AND close_time_utc < ?" if to_exclusive else " AND close_time_utc <= ?"
        args.append(date_to)
    if symbol:
        q += " AND symbol = ?"
        args.append(symbol)
    q += " ORDER BY close_time_utc ASC"
    rows = [dict(r) for r in conn.execute(q, args)]
    rows = [enrich_trade(r, meta, symbol_cfg) for r in rows]

    total = len(rows)
    wins = [t for t in rows if t["net_profit"] > 0]
    losses = [t for t in rows if t["net_profit"] < 0]
    be = [t for t in rows if t["net_profit"] == 0]
    gross_profit = sum(t["net_profit"] for t in wins)
    gross_loss = abs(sum(t["net_profit"] for t in losses))
    net = sum(t["net_profit"] for t in rows)
    volume = sum(t["volume_out"] or 0 for t in rows)
    pips_total = sum(t["pips"] or 0 for t in rows)

    win_rate = (len(wins) / total * 100.0) if total else None
    avg_win = (gross_profit / len(wins)) if wins else None
    avg_loss = (gross_loss / len(losses)) if losses else None
    pf = (gross_profit / gross_loss) if gross_loss > 1e-12 else None
    # expectancy per trade: p*avg_win - (1-p)*avg_loss (dalam mata uang akun)
    expectancy = None
    if total and len(wins) + len(losses) > 0:
        p = len(wins) / (len(wins) + len(losses))
        if avg_win is not None and avg_loss is not None:
            expectancy = p * avg_win - (1 - p) * avg_loss

    # ---- kelompok per simbol / hari / minggu / bulan -------------------
    by_symbol: dict[str, dict] = defaultdict(_empty_group)
    by_daily: dict[str, dict] = defaultdict(_empty_group)
    by_weekly: dict[str, dict] = defaultdict(_empty_group)
    by_monthly: dict[str, dict] = defaultdict(_empty_group)

    def add(g: dict, t: dict) -> None:
        g["trades"] += 1
        g["net_profit"] = round(g["net_profit"] + t["net_profit"], 8)
        g["volume"] = round(g["volume"] + (t["volume_out"] or 0), 8)
        if t["net_profit"] > 0:
            g["wins"] += 1
        elif t["net_profit"] < 0:
            g["losses"] += 1

    for t in rows:
        add(by_symbol[t.get("symbol") or "?"], t)
        buckets = _bucket_keys(t["close_time_utc"] or "")
        if buckets:
            add(by_daily[buckets["daily"]], t)
            add(by_weekly[buckets["weekly"]], t)
            add(by_monthly[buckets["monthly"]], t)

    def finalize(d: dict) -> dict:
        out = {}
        for k in sorted(d):
            g = d[k]
            g["win_rate"] = round(g["wins"] / g["trades"] * 100, 2) if g["trades"] else None
            out[k] = g
        return out

    # ---- open positions ringkas -----------------------------------------
    open_rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM {tt} WHERE account_id=? AND status='open'",
        (account_id,))]

    return {
        "account_id": account_id,
        "filters": {"from": date_from, "to": date_to,
                    "to_exclusive": bool(to_exclusive), "symbol": symbol},
        "total_closed": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(be),
        "win_rate": round(win_rate, 2) if win_rate is not None else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_profit": round(net, 2),
        "profit_factor": round(pf, 2) if pf is not None else None,
        "average_win": round(avg_win, 2) if avg_win is not None else None,
        "average_loss": round(avg_loss, 2) if avg_loss is not None else None,
        "expectancy": round(expectancy, 2) if expectancy is not None else None,
        "total_volume": round(volume, 2),
        "total_pips": round(pips_total, 1) if rows else None,
        "open_trades": len(open_rows),
        "by_symbol": finalize(by_symbol),
        "daily": finalize(by_daily),
        "weekly": finalize(by_weekly),
        "monthly": finalize(by_monthly),
    }


def equity_curve(conn, account_id: int, symbol_cfg: dict,
                 date_from: str | None = None, date_to: str | None = None,
                 symbol: str | None = None,
                 to_exclusive: bool = False) -> dict:
    """Kurva berbasis transaksi (relatif rentang, bukan saldo absolut):
    - 'trade'     : trade closed, nilai = kumulatif net P/L
    - 'deposit'/'withdrawal' dsb.: ops balance dari raw deals
    Dari kurva ini dihitung drawdown historis (peak-to-trough).

    PENTING: ini KURVA P/L TRADING kumulatif, BUKAN kurva equity akun
    historis — snapshot equity harian tidak tersedia dari MT5 history.
    max_drawdown_pct juga dihitung terhadap puncak P/L kumulatif (bukan
    saldo); bila puncak <= 0 nilainya None (UI: "N/A") — jangan pernah
    menyebutnya drawdown equity akun.
    """
    meta = load_symbol_meta(conn, account_id)
    tt = dbmod.trades_table(conn)

    q = (f"SELECT position_id, symbol, direction, close_time_utc, net_profit,"
         f" volume_out, exit_reason FROM {tt}"
         " WHERE account_id=? AND status='closed'")
    args = [account_id]
    if date_from:
        q += " AND close_time_utc >= ?"
        args.append(date_from)
    if date_to:
        q += " AND close_time_utc < ?" if to_exclusive else " AND close_time_utc <= ?"
        args.append(date_to)
    if symbol:
        q += " AND symbol = ?"
        args.append(symbol)
    points = []
    for r in conn.execute(q, args):
        points.append({"t": r["close_time_utc"], "type": "trade",
                       "delta": r["net_profit"],
                       "label": f"{r['symbol']} #{r['position_id']}"})

    qb = ("SELECT ticket, time_utc, profit, comment FROM mt5_deals"
          " WHERE account_id=? AND deal_type=2")
    bargs = [account_id]
    if date_from:
        qb += " AND time_utc >= ?"
        bargs.append(date_from)
    if date_to:
        # Phase 1.1 fix (bug audit): SAMAKAN dengan cabang trades di atas
        # dan cabang balance-ops pada reconciliation — to_exclusive WAJIB
        # dihormati, kalau tidak operasi balance tepat di batas `to`
        # dobel terhitung terhadap kontrak interval [from, to).
        qb += " AND time_utc < ?" if to_exclusive else " AND time_utc <= ?"
        bargs.append(date_to)
    for r in conn.execute(qb, bargs):
        p = float(r["profit"] or 0)
        points.append({
            "t": r["time_utc"],
            "type": "deposit" if p >= 0 else "withdrawal",
            "delta": p,
            "label": r["comment"] or "",
        })

    points.sort(key=lambda x: (x["t"] or "", 0 if x["type"] == "deposit" else 1))

    cum = 0.0
    bal = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_pct: float | None = None  # None = TIDAK ADA penyebut valid
    max_dd_at = None
    out_points = []
    for pt in points:
        if pt["type"] in ("deposit", "withdrawal"):
            # ops balance tidak mengubah kurva P/L trading
            bal = round(bal + pt["delta"], 8)
        else:
            cum = round(cum + pt["delta"], 8)
        out_points.append({
            "t": pt["t"], "type": pt["type"], "label": pt["label"],
            "net_cum": cum, "balance_ops_cum": bal,
        })
        # drawdown dihitung dari kurva TRADING saja — deposit/withdrawal
        # bukan performa trading, tidak boleh dihitung sebagai drawdown.
        if pt["type"] == "trade":
            peak = max(peak, cum)
            dd = peak - cum
            if dd > max_dd + 1e-12:
                max_dd = dd
                # Phase 1.1 fix (Temuan 6): persen TANPA penyebut valid
                # (puncak P/L kumulatif <= 0, mis. akun belum pernah untung
                # pada rentang ini) mengembalikan None -> UI tampil "N/A".
                # Dahulu 0.0 — menyesatkan (nominal DD ada, persen 0%).
                max_dd_pct = (dd / peak * 100.0) if peak > 1e-12 else None
                max_dd_at = pt["t"]

    # Kurva untung terus (tidak ada drawdown) tetapi puncak VALID (> 0):
    # 0% adalah jawaban sah — penyebut ada. Hanya puncak <= 0 yang N/A.
    if max_dd_pct is None and max_dd == 0.0 and peak > 1e-12:
        max_dd_pct = 0.0

    return {
        "points": out_points,
        "final_net_cum": round(cum, 2),
        "balance_ops_cum": round(bal, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": (round(max_dd_pct, 2)
                             if max_dd_pct is not None else None),
        "max_drawdown_at": max_dd_at,
    }


def reconciliation(conn, account_id: int,
                   date_from: str | None = None, date_to: str | None = None,
                   symbol: str | None = None,
                   to_exclusive: bool = False) -> dict:
    """Rekonsiliasi: raw MT5 deals vs trades hasil rekonstruksi.

    Metode (sumber kebenaran = tabel mt5_deals):
    1. Pilih SEMUA deals trading (deal_type 0/1) dalam interval yang sama
       (inklusif dari, to tergantung to_exclusive — konsisten dengan
       compute_stats). Deposit/withdrawal TIDAK masuk profit trading,
       hanya dilaporkan terpisah.
    2. from_deals  : jumlah profit/commission/swap/fee dari deals itu.
    3. by_position : trades yang position_id-nya terlibat — menjelaskan
       trade yang SEBAGIAN deals-nya di dalam interval (partial close
       lintas batas): jumlah net_profit trade PENUH bisa > deals interval.
    4. by_close    : trades CLOSED yang close_time-nya di interval —
       basis yang sama dipakai compute_stats.
    5. delta_close_vs_deals = by_close.net - from_deals.net; 0 berarti
       konsisten; != 0 menandakan ada deal masuk/keluar interval yang
       dimiliki trade yang close di luar interval (normal, dijelaskan di
       docs/DATE-TIMEZONE-RECONCILIATION.md).

    Endpoint ini ALAT DIAGNOSA — tidak mengubah data apa pun.
    """
    meta = load_symbol_meta(conn, account_id)
    tt = dbmod.trades_table(conn)

    # --- 1) deals trading dalam interval ---------------------------------
    cond = ["account_id=?", "deal_type IN (0,1)"]
    dargs: list = [account_id]
    if date_from:
        cond.append("time_utc >= ?")
        dargs.append(date_from)
    if date_to:
        cond.append("time_utc < ?" if to_exclusive else "time_utc <= ?")
        dargs.append(date_to)
    if symbol:
        cond.append("symbol = ?")
        dargs.append(symbol)
    where = " AND ".join(cond)

    deals = [dict(r) for r in conn.execute(
        f"SELECT ticket, position_id, symbol, time_utc, profit, commission,"
        f" swap, fee FROM mt5_deals WHERE {where}", dargs)]
    in_deals = [d for d in deals if d["position_id"] is not None]

    def _sum(rows, k):
        return round(sum(r[k] or 0 for r in rows), 8)

    from_deals = {
        "deals": len(deals),
        "profit": _sum(deals, "profit"),
        "commission": _sum(deals, "commission"),
        "swap": _sum(deals, "swap"),
        "fee": _sum(deals, "fee"),
    }
    from_deals["net"] = round(from_deals["profit"] + from_deals["commission"]
                              + from_deals["swap"] + from_deals["fee"], 8)

    # ops balance (deposit/withdrawal) dalam interval yang sama —
    # dilaporkan TERPISAH, tidak pernah masuk profit trading.
    bcond = ["account_id=?", "deal_type=2"]
    bargs: list = [account_id]
    if date_from:
        bcond.append("time_utc >= ?")
        bargs.append(date_from)
    if date_to:
        bcond.append("time_utc < ?" if to_exclusive else "time_utc <= ?")
        bargs.append(date_to)
    bops = [dict(r) for r in conn.execute(
        f"SELECT ticket, time_utc, profit, comment FROM mt5_deals"
        f" WHERE {' AND '.join(bcond)}", bargs)]

    # --- 2) trades yang terlibat (berdasar position_id dari deals) -------
    pids = sorted({d["position_id"] for d in in_deals})
    by_position = {"positions": len(pids), "net_profit": 0.0, "closed": 0}
    if pids:
        ph = ",".join("?" * len(pids))
        rows = [dict(r) for r in conn.execute(
            f"SELECT status, net_profit FROM {tt} WHERE account_id=?"
            f" AND position_id IN ({ph})", [account_id] + pids)]
        by_position["net_profit"] = round(
            sum(r["net_profit"] or 0 for r in rows), 8)
        by_position["closed"] = sum(1 for r in rows if r["status"] == "closed")

    # --- 3) trades CLOSED dengan close_time di interval (basis stats) ----
    tcond = ["account_id=?", "status='closed'"]
    targs: list = [account_id]
    if date_from:
        tcond.append("close_time_utc >= ?")
        targs.append(date_from)
    if date_to:
        tcond.append("close_time_utc < ?" if to_exclusive else "close_time_utc <= ?")
        targs.append(date_to)
    if symbol:
        tcond.append("symbol = ?")
        targs.append(symbol)
    trows = [dict(r) for r in conn.execute(
        f"SELECT position_id, profit, commission, swap, fee, net_profit"
        f" FROM {tt} WHERE {' AND '.join(tcond)}", targs)]
    by_close = {
        "trades": len(trows),
        "profit": _sum(trows, "profit"),
        "commission": _sum(trows, "commission"),
        "swap": _sum(trows, "swap"),
        "fee": _sum(trows, "fee"),
        "net_profit": _sum(trows, "net_profit"),
    }

    return {
        "interval": {"from": date_from, "to": date_to,
                     "to_exclusive": bool(to_exclusive), "symbol": symbol},
        "from_deals": from_deals,
        "by_position": by_position,
        "by_close": by_close,
        "balance_ops": {
            "count": len(bops),
            "total": round(sum(float(r["profit"] or 0) for r in bops), 8),
            "items": bops[:50],
        },
        "delta_close_vs_deals": round(
            by_close["net_profit"] - from_deals["net"], 8),
        "note": ("delta = by_close.net - from_deals.net. Nilai 0 = konsisten. "
                 "Selisih normal terjadi jika ada deal di dalam interval yang "
                 "milik trade yang close di luar interval (partial close / "
                 "trade lintas periode). Deposit & withdrawal tidak pernah "
                 "dihitung profit trading."),
    }
