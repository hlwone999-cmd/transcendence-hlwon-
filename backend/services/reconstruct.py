"""Rekonstruksi posisi dari raw deals — BUKAN 1 deal = 1 trade.

Prinsip:
- Hanya DEAL_TYPE_BUY/SELL yang dianggap trading. Deposit, withdrawal,
  bonus, credit, correction (DEAL_TYPE_BALANCE dsb.) disimpan mentah di
  mt5_deals tapi TIDAK PERNAH menjadi trade (tidak dihitung menang/kalah).
- Deals dikelompokkan per position_id — bekerja untuk akun HEDGING dan
  NETTING (netting = satu position_id menampung scale-in/partial close).
- Partial close: volume OUT < volume IN -> trade tetap terbuka dengan
  volume menyusut sampai kumulatif OUT >= IN.
- Scale-in: beberapa IN -> entry VWAP (volume-weighted).
- Reversal (DEAL_ENTRY_INOUT): deal menutup sisa posisi lama; sisa volume
  setelah itu menjadi pembuka posisi berlawanan (position_id baru =
  ticket deal, perilaku netting MT5). Ditangani dua-pass agar tidak
  mengarang data.
- SL/TP awal diambil dari history ORDER pembuka (deals tidak membawa
  SL/TP). Kalau tidak ada -> None -> RR tampil "N/A". Tidak ada SL fiktif.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from mt5client.provider import utcnow_iso

EPS = 1e-9

DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
TRADING_DEAL_TYPES = {DEAL_TYPE_BUY, DEAL_TYPE_SELL}
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
OUT_ENTRIES = {DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY}
DEAL_REASON_SL, DEAL_REASON_TP = 4, 5

TYPE_NAMES = {DEAL_TYPE_BUY: "buy", DEAL_TYPE_SELL: "sell"}
ENTRY_NAMES = {DEAL_ENTRY_IN: "in", DEAL_ENTRY_OUT: "out",
               DEAL_ENTRY_INOUT: "inout", DEAL_ENTRY_OUT_BY: "out_by"}
REASON_EXIT = {DEAL_REASON_SL: "sl", DEAL_REASON_TP: "tp"}


def _iso(sec: int | float | None) -> str | None:
    if not sec:
        return None
    return datetime.fromtimestamp(int(sec), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _vwap(rows) -> float | None:
    tot_v = sum(r["volume"] for r in rows)
    if tot_v <= EPS:
        return None
    return sum(r["volume"] * r["price"] for r in rows) / tot_v


def _trade_row(account_id: int, position_id, symbol: str, deals: list[dict],
               orders_by_pos: dict[int, list[dict]]) -> dict | None:
    ins = [d for d in deals if d["entry"] == DEAL_ENTRY_IN]
    outs = [d for d in deals if d["entry"] in OUT_ENTRIES]
    if not ins:
        return None  # posisi tanpa pembuka (harusnya tak terjadi) -> lewati

    vol_in = sum(d["volume"] for d in ins)
    vol_out = sum(d["volume"] for d in outs)
    closed = vol_out >= vol_in - EPS

    first_in = ins[0]
    direction = TYPE_NAMES.get(first_in["type"])
    open_time = min(int(d["time"]) for d in ins)
    last_out = max(outs, key=lambda d: (int(d["time"]), d["ticket"])) if outs else None
    close_time = int(last_out["time"]) if (closed and last_out) else None

    profit = sum(d["profit"] for d in deals)
    commission = sum(d["commission"] for d in deals)
    swap = sum(d["swap"] for d in deals)
    fee = sum(d["fee"] for d in deals)

    exit_reason = None
    if closed and last_out is not None:
        exit_reason = REASON_EXIT.get(last_out["reason"])

    # SL/TP awal dari order pembuka posisi (order pertama posisi ini).
    sl0 = tp0 = None
    for o in sorted(orders_by_pos.get(position_id, []),
                    key=lambda o: (int(o.get("time") or 0), o.get("ticket") or 0)):
        if o.get("sl"):
            sl0 = o["sl"]
        if o.get("tp"):
            tp0 = o["tp"]
        break  # hanya order pertama

    return {
        "account_id": account_id,
        "position_id": position_id,
        "symbol": symbol,
        "direction": direction,
        "status": "closed" if closed else "open",
        "volume_in": round(vol_in, 8),
        "volume_out": round(vol_out, 8),
        "entry_price": _vwap(ins),
        "exit_price": _vwap(outs) if closed else None,
        "open_time_utc": _iso(open_time),
        "close_time_utc": _iso(close_time),
        "profit": round(profit, 8),
        "commission": round(commission, 8),
        "swap": round(swap, 8),
        "fee": round(fee, 8),
        "net_profit": round(profit + commission + swap + fee, 8),
        "sl_initial": sl0,
        "tp_initial": tp0,
        "exit_reason": exit_reason,
        "magic": first_in["magic"],
        "comment": first_in["comment"],
        "entry_deals": [d["ticket"] for d in ins],
        "exit_deals": [d["ticket"] for d in outs],
    }


def _to_dict(x) -> dict:
    """Terima dict / namedtuple / Row apa adanya -> dict murni."""
    if isinstance(x, dict):
        return dict(x)
    f = getattr(x, "_asdict", None)
    if callable(f):
        return dict(f())
    keys = getattr(x, "keys", None)
    if callable(keys):
        return {k: x[k] for k in keys()}
    return dict(x)


def reconstruct_trades(account_id: int, deals: list,
                       orders: list) -> list[dict]:
    """Bangun ulang SEMUA trade dari kumpulan deals + orders.

    Return daftar dict siap upsert ke tabel `trades`. Idempoten:
    dipanggil berkali-kali dengan data sama menghasilkan sama.
    """
    deals = [_to_dict(d) for d in deals]
    orders = [_to_dict(o) for o in orders]
    # normalisasi nama field: package resmi MT5 memakai 'type',
    # kolom DB memakai 'deal_type' — terima keduanya.
    for d in deals:
        if "deal_type" not in d and "type" in d:
            d["deal_type"] = d["type"]

    # 1) hanya deals trading; sisakan non-trading apa adanya di raw tabel
    trading = [d for d in deals if d["deal_type"] in TRADING_DEAL_TYPES]

    orders_by_pos: dict[int, list[dict]] = defaultdict(list)
    for o in orders:
        pid = o.get("position_id")
        if pid:
            orders_by_pos[pid].append(o)

    groups: dict[int, list[dict]] = defaultdict(list)
    for d in trading:
        groups[d["position_id"]].append(d)

    # 2) tangani reversal (INOUT) gaya netting:
    #    bagi deal menjadi OUT sisa posisi lama + IN posisi baru (ticket deal).
    #    Deal INOUT di grup lama DIGANTI volumenya menjadi bagian yang
    #    benar-benar menutup posisi lama agar VWAP exit tidak meleset.
    for pid, gdeals in list(groups.items()):
        # urutkan by waktu utk hitung sisa volume secara benar
        gdeals.sort(key=lambda d: (int(d["time"]), d["ticket"]))
        open_vol = 0.0
        for idx, d in enumerate(gdeals):
            if d["entry"] == DEAL_ENTRY_IN:
                open_vol += d["volume"]
            elif d["entry"] == DEAL_ENTRY_OUT:
                open_vol = max(0.0, open_vol - d["volume"])
            elif d["entry"] == DEAL_ENTRY_INOUT:
                close_part = min(d["volume"], open_vol)
                open_vol = max(0.0, open_vol - close_part)
                remainder = d["volume"] - close_part
                if close_part > EPS:
                    gdeals[idx] = {**d, "volume": close_part}
                else:
                    gdeals[idx] = None  # tidak menutup apa pun di posisi lama
                if remainder > EPS:
                    new_pid = d["ticket"]
                    groups.setdefault(new_pid, []).append({
                        **d,
                        "position_id": new_pid,
                        "entry": DEAL_ENTRY_IN,
                        "volume": remainder,
                        "entry_name": "in",
                    })
            elif d["entry"] == DEAL_ENTRY_OUT_BY:
                open_vol = max(0.0, open_vol - d["volume"])
        groups[pid] = [d for d in gdeals if d is not None]

    # 3) simbol konsisten per posisi (deal reversal sama symbol)
    trades: list[dict] = []
    for pid, gdeals in groups.items():
        if not gdeals:
            continue
        symbol = next((d["symbol"] for d in gdeals if d["symbol"]), "") or ""
        row = _trade_row(account_id, pid, symbol, gdeals, orders_by_pos)
        if row:
            trades.append(row)
    trades.sort(key=lambda t: (t["open_time_utc"] or "", t["position_id"]))
    return trades
