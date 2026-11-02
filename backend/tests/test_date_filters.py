"""Test filter tanggal, timezone boundary, dan rekonsiliasi (Phase 1B).

Dataset SINTETIS Oktober 2026 yang mereproduksi angka laporan pengguna
(bukan data produksi — hanya nilai numerik yang dilaporkan):
- Okt 2026 penuh      : 37 trade closed, net +22,036.79
- Okt minus 1 Okt     : net +228,257.81  (memodelkan window MT5 mulai 2 Okt)
- Okt minus 1-2 Okt   : net +842,918.16  (memodelkan filter "30H" lama
                        yang mengirim from=tanggal saja dari jam HP)
- Sep 2026 penuh      : 2 trade, net -1,564,837.89
Termasuk edge case: trade lintas bulan, close tepat 00:00:00 UTC,
close tepat 1 Nov 00:00 WIB (31 Okt 17:00 UTC), partial close lintas
interval, deposit/withdrawal yang tidak boleh masuk profit.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mt5client.mock import (
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_TYPE_BALANCE, DEAL_TYPE_BUY,
    DEAL_TYPE_SELL,
    AccountInfo, SymbolInfo, TradeDeal,
)


# --------------------------------------------------------------- helpers
def _sec(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def _deal_abs(ticket, order, sec, dtype, entry, position_id, volume, price,
              profit=0.0, commission=0.0, swap=0.0, fee=0.0,
              symbol="XAUUSD", comment="", magic=777):
    return TradeDeal(
        ticket=ticket, order=order, time=sec, time_msc=sec * 1000,
        type=dtype, entry=entry, magic=magic, position_id=position_id,
        reason=0, volume=volume, price=price, commission=commission,
        swap=swap, profit=profit, symbol=symbol, comment=comment,
        external_id=0, fee=fee,
    )


def _trade(tid, open_sec, close_sec, net, direction="buy", *,
           commission=4.0, volume=0.10, price_in=2000.0, price_out=2010.0):
    """Trade closed sederhana: 1 deal IN + 1 deal OUT. net = profit+comm."""
    d_in = DEAL_TYPE_BUY if direction == "buy" else DEAL_TYPE_SELL
    d_out = DEAL_TYPE_SELL if direction == "buy" else DEAL_TYPE_BUY
    profit = round(net - (-commission), 2)  # net = profit - commission
    return [
        _deal_abs(tid * 10 + 1, tid, open_sec, d_in, DEAL_ENTRY_IN, tid,
                  volume, price_in, commission=-commission),
        _deal_abs(tid * 10 + 2, tid + 1, close_sec, d_out, DEAL_ENTRY_OUT,
                  tid, volume, price_out, profit=profit),
    ]


def october_dataset():
    deals: list = []
    orders: list = []
    pid = 9001

    # --- September: 2 trade, net -1,564,837.89 -------------------------
    deals += _trade(9101, _sec(2026, 9, 10, 9), _sec(2026, 9, 15, 10),
                    -1_614_837.89, commission=4.0)
    deals += _trade(9104, _sec(2026, 9, 18, 9), _sec(2026, 9, 20, 10),
                    50_000.0, commission=4.0)
    # deposit Sep (bukan trading)
    deals.append(_deal_abs(9000, 0, _sec(2026, 9, 3, 8), DEAL_TYPE_BALANCE,
                           DEAL_ENTRY_IN, 0, 0.0, 0.0, profit=8000.0,
                           symbol="", comment="Deposit"))

    # --- Oktober: 5 trade kunci (angka laporan pengguna) ---------------
    # T001: buka 30 Sep, close TEPAT 1 Okt 00:00:00 UTC (lintas bulan +
    # boundary tengah malam). Atribusi close-date -> masuk Oktober.
    deals += _trade(9001, _sec(2026, 9, 30, 22), _sec(2026, 10, 1, 0, 0, 0),
                    -206_221.02, commission=4.0)
    deals += _trade(9004, _sec(2026, 10, 2, 7), _sec(2026, 10, 2, 10),
                    -614_660.35, commission=4.0)
    deals += _trade(9007, _sec(2026, 10, 5, 6), _sec(2026, 10, 5, 9),
                    441_819.05, commission=4.0)
    deals += _trade(9010, _sec(2026, 10, 7, 10), _sec(2026, 10, 7, 13),
                    499_959.97, commission=4.0)
    deals += _trade(9013, _sec(2026, 10, 8, 13), _sec(2026, 10, 8, 16),
                    226_693.99, commission=4.0)

    # --- 32 trade kecil lain di Oktober (total -325,554.85) ------------
    # T006: close tepat 31 Okt 17:00 UTC = 1 Nov 00:00 WIB (boundary WIB)
    deals += _trade(9016, _sec(2026, 10, 31, 14), _sec(2026, 10, 31, 17, 0, 0),
                    -10_500.0, commission=4.0)
    # T007: close tepat 1 Nov 00:00 UTC -> DI LUAR Oktober UTC
    # (eksklusif); net 0 (breakeven) agar tidak mengubah angka net.
    deals += _trade(9019, _sec(2026, 10, 31, 21), _sec(2026, 11, 1, 0, 0, 0),
                    0.0, commission=4.0)
    # T008..T038: 31 trade kecil; i=0 = -54,85, sisanya 30 x -10,500
    # -> bersama T006 total -325,554.85 dan 32 trade dalam Oktober
    tid = 9022
    for i in range(31):
        day = 9 + (i % 21)
        hour = 8 + (i // 21) * 2
        net = -10_500.0 if i > 0 else -54.85
        deals += _trade(tid, _sec(2026, 10, day, hour),
                        _sec(2026, 10, day, hour + 1), net, commission=4.0)
        tid += 3

    # --- deposit/withdrawal Oktober (TIDAK boleh masuk profit) ---------
    deals.append(_deal_abs(9500, 0, _sec(2026, 10, 6, 9), DEAL_TYPE_BALANCE,
                           DEAL_ENTRY_IN, 0, 0.0, 0.0, profit=5000.0,
                           symbol="", comment="Deposit"))
    deals.append(_deal_abs(9501, 0, _sec(2026, 10, 12, 9), DEAL_TYPE_BALANCE,
                           DEAL_ENTRY_IN, 0, 0.0, 0.0, profit=-2000.0,
                           symbol="", comment="Withdrawal"))

    symbols = {
        "XAUUSD": SymbolInfo(name="XAUUSD", point=0.01, digits=2,
                             trade_tick_size=0.01, trade_tick_value=1.0,
                             trade_contract_size=100.0, description="Gold"),
    }
    account = AccountInfo(
        login=12345678, trade_mode=0, leverage=500, limit_orders=200,
        margin_so_mode=0, trade_allowed=True, trade_expert=False,
        margin_mode=0, currency_digits=2, fifo_close=False,
        balance=0.0, credit=0.0, profit=0.0, equity=0.0,
        margin=0.0, margin_free=0.0, margin_level=0.0,
        margin_so_call=50.0, margin_so_so=20.0, margin_initial=0.0,
        margin_maintenance=0.0, assets=0.0, liabilities=0.0,
        commission_blocked=False, name="Uji", email="t@example.com",
        server="Exness-MT5Real", currency="USD", company="Exness",
    )
    return {"deals": deals, "orders": orders, "positions": [],
            "symbols": symbols, "account": account}


def partial_cross_dataset():
    """Trade partial close lintas interval utk test rekonsiliasi:
    open 5 Okt, partial out 20 Okt (+30,000), final out 1 Nov (+10,000).
    """
    deals = [
        _deal_abs(9701, 970, _sec(2026, 10, 5, 8), DEAL_TYPE_BUY,
                  DEAL_ENTRY_IN, 9701, 0.20, 2000.0, commission=-4.0),
        _deal_abs(9702, 971, _sec(2026, 10, 20, 9), DEAL_TYPE_SELL,
                  DEAL_ENTRY_OUT, 9701, 0.10, 2010.0, profit=30000.0),
        _deal_abs(9703, 972, _sec(2026, 11, 1, 9), DEAL_TYPE_SELL,
                  DEAL_ENTRY_OUT, 9701, 0.10, 2010.0, profit=10004.0,
                  commission=-4.0),
    ]
    symbols = {
        "XAUUSD": SymbolInfo(name="XAUUSD", point=0.01, digits=2,
                             trade_tick_size=0.01, trade_tick_value=1.0,
                             trade_contract_size=100.0, description="Gold"),
    }
    account = AccountInfo(
        login=12345678, trade_mode=0, leverage=500, limit_orders=200,
        margin_so_mode=0, trade_allowed=True, trade_expert=False,
        margin_mode=0, currency_digits=2, fifo_close=False,
        balance=0.0, credit=0.0, profit=0.0, equity=0.0,
        margin=0.0, margin_free=0.0, margin_level=0.0,
        margin_so_call=50.0, margin_so_so=20.0, margin_initial=0.0,
        margin_maintenance=0.0, assets=0.0, liabilities=0.0,
        commission_blocked=False, name="Uji", email="t@example.com",
        server="Exness-MT5Real", currency="USD", company="Exness",
    )
    return {"deals": deals, "orders": [], "positions": [],
            "symbols": symbols, "account": account}


# ------------------------------------------------------------------ tests
OCT_FROM = "2026-10-01T00:00:00Z"
OCT_TO_EXCL = "2026-11-01T00:00:00Z"   # [1 Okt, 1 Nov) UTC
OCT_WIB_FROM = "2026-09-30T17:00:00Z"  # 1 Okt 00:00 WIB
OCT_WIB_TO_EXCL = "2026-10-31T17:00:00Z"  # 1 Nov 00:00 WIB
SEP_FROM = "2026-09-01T00:00:00Z"
SEP_TO_EXCL = "2026-10-01T00:00:00Z"


@pytest.fixture()
def oct_client(make_client, cfg):
    _CURRENT_DATASET["ds"] = october_dataset()
    c = make_client(dataset=october_dataset())
    _sync_dataset(cfg)
    return c


def _sync_dataset(cfg):
    """Isi DB test dengan dataset via SyncService (mock, mode full)."""
    import db as dbmod
    from mt5client.mock import MockMT5Client
    from services.sync import SyncService
    dbmod.close_db()
    svc = SyncService(cfg, lambda: MockMT5Client())
    svc._factory = lambda: MockMT5Client(_CURRENT_DATASET["ds"])
    summary = svc.sync(mode="full")
    assert summary["status"] == "ok", summary
    dbmod.close_db()


_CURRENT_DATASET = {"ds": None}


AUTH = {"X-API-Token": "TESTTOKEN"}  # dari conftest.cfg


def _get(client, path, params=None):
    r = client.get(path, query_string=params, headers=AUTH)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r


def _stats(client, **params):
    return _get(client, "/api/v1/journal/stats", params).get_json()


def _recon(client, **params):
    return _get(client, "/api/v1/journal/reconciliation", params).get_json()


class TestOctoberRegression:
    """Regresi angka laporan pengguna — dokumentasi perilaku filter."""

    def test_oktober_penuh_utc(self, oct_client):
        s = _stats(oct_client, **{"from": OCT_FROM, "to": OCT_TO_EXCL,
                                  "to_exclusive": "1"})
        assert s["total_closed"] == 37
        assert s["net_profit"] == 22036.79

    def test_oktober_dari_2_okt(self, oct_client):
        # memodelkan window MT5 yang mulai 2 Okt -> +228,257.81
        s = _stats(oct_client, **{"from": "2026-10-02T00:00:00Z"})
        assert s["net_profit"] == 228257.81

    def test_filter_30h_lama_tanggal_saja(self, oct_client):
        # memodelkan perilaku LAMA APK: from = tanggal dari jam HP,
        # tanpa `to` -> +842,918.16 (tampil ~843rb di APK)
        s = _stats(oct_client, **{"from": "2026-10-03"})
        assert s["net_profit"] == 842918.16

    def test_september_penuh(self, oct_client):
        s = _stats(oct_client, **{"from": SEP_FROM, "to": SEP_TO_EXCL,
                                  "to_exclusive": "1"})
        assert s["total_closed"] == 2
        assert s["net_profit"] == -1564837.89


class TestCalendarVsRolling:
    def test_oktober_wib_mengecualikan_1_nov_wib(self, oct_client):
        # boundary WIB: trade T006 close 31 Okt 17:00 UTC (= 1 Nov 00:00
        # WIB) TIDAK termasuk Oktober-WIB -> 36 trade, +32,536.79
        s = _stats(oct_client, **{"from": OCT_WIB_FROM,
                                  "to": OCT_WIB_TO_EXCL,
                                  "to_exclusive": "1"})
        assert s["total_closed"] == 36
        assert s["net_profit"] == 32536.79

    def test_trade_lintas_bulan_masuk_bulan_tutup(self, oct_client):
        # T001 buka 30 Sep, close 1 Okt -> masuk Oktober (bukan September)
        sep = _stats(oct_client, **{"from": SEP_FROM, "to": SEP_TO_EXCL,
                                    "to_exclusive": "1"})
        okt = _stats(oct_client, **{"from": OCT_FROM, "to": OCT_TO_EXCL,
                                    "to_exclusive": "1"})
        assert sep["net_profit"] == -1564837.89
        assert okt["net_profit"] == 22036.79

    def test_boundary_midnight_inklusif_dari(self, oct_client):
        # T001 close TEPAT 1 Okt 00:00:00Z: termasuk interval [1 Okt, ...)
        s = _stats(oct_client, **{"from": OCT_FROM, "to": OCT_TO_EXCL,
                                  "to_exclusive": "1"})
        assert s["net_profit"] == 22036.79  # jika excluded nilainya beda

    def test_boundary_to_exclusive_tepat(self, oct_client):
        # T007 close TEPAT 1 Nov 00:00:00Z: keluar saat to eksklusif;
        # breakeven (net 0) -> jumlah trade berkurang, net tetap.
        s_incl = _stats(oct_client, **{"from": OCT_FROM, "to": OCT_TO_EXCL})
        s_excl = _stats(oct_client, **{"from": OCT_FROM, "to": OCT_TO_EXCL,
                                       "to_exclusive": "1"})
        assert s_incl["total_closed"] - s_excl["total_closed"] == 1
        assert s_excl["net_profit"] == 22036.79


class TestRollingWindow:
    def test_rolling_7_hari_konsisten_dengan_custom(self, oct_client):
        # rolling 7 hari = [now-7d, now) — uji via endpoint: rentang sama
        # harus memberi hasil sama antara to eksklusif vs to inklusif
        # yang dikurangi 1 detik (granularitas DB = detik).
        a = _stats(oct_client, **{"from": "2026-10-05T00:00:00Z",
                                  "to": "2026-10-12T00:00:00Z",
                                  "to_exclusive": "1"})
        b = _stats(oct_client, **{"from": "2026-10-05T00:00:00Z",
                                  "to": "2026-10-11T23:59:59Z"})
        assert a["net_profit"] == b["net_profit"]
        assert a["total_closed"] == b["total_closed"]

    def test_rolling_30_90_lebih_besar_dari_7(self, oct_client):
        s7 = _stats(oct_client, **{"from": "2026-10-25T00:00:00Z",
                                   "to": "2026-11-02T00:00:00Z",
                                   "to_exclusive": "1"})
        s30 = _stats(oct_client, **{"from": "2026-10-03T00:00:00Z",
                                    "to": "2026-11-02T00:00:00Z",
                                    "to_exclusive": "1"})
        s90 = _stats(oct_client, **{"from": "2026-08-04T00:00:00Z",
                                    "to": "2026-11-02T00:00:00Z",
                                    "to_exclusive": "1"})
        assert s90["total_closed"] >= s30["total_closed"] > s7["total_closed"]


class TestDepositWithdrawal:
    def test_ops_balance_tidak_masuk_profit(self, oct_client):
        # Deposit +5000 (6 Okt) & WD -2000 (12 Okt) ada di DB,
        # tapi net Oktober tetap +22,036.79 (bukan +27,036.79 dst).
        s = _stats(oct_client, **{"from": OCT_FROM, "to": OCT_TO_EXCL,
                                  "to_exclusive": "1"})
        assert s["net_profit"] == 22036.79


class TestKonsistensiEndpoint:
    def test_stats_trades_kurva_konsisten(self, oct_client):
        s = _stats(oct_client, **{"from": OCT_FROM, "to": OCT_TO_EXCL,
                                  "to_exclusive": "1"})
        tl = _get(oct_client, "/api/v1/journal/trades",
                  {"from": OCT_FROM, "to": OCT_TO_EXCL, "to_exclusive": "1",
                   "page_size": "200"}).get_json()
        e = _get(oct_client, "/api/v1/journal/equity-curve",
                 {"from": OCT_FROM, "to": OCT_TO_EXCL,
                  "to_exclusive": "1"}).get_json()
        assert s["total_closed"] == tl["total"]
        sum_trades = round(sum(t["net_profit"] for t in tl["items"]), 2)
        assert sum_trades == s["net_profit"] == e["final_net_cum"]

    def test_from_setelah_to_ditolak(self, oct_client):
        r = oct_client.get("/api/v1/journal/stats",
                           query_string={"from": "2026-10-10",
                                         "to": "2026-10-01"},
                           headers=AUTH)
        assert r.status_code == 400


class TestRekonsiliasi:
    def test_oktober_delta_nol(self, oct_client):
        rec = _recon(oct_client, **{"from": OCT_FROM, "to": OCT_TO_EXCL,
                                    "to_exclusive": "1"})
        assert rec["from_deals"]["net"] == 22036.79
        assert rec["by_close"]["net_profit"] == 22036.79
        assert rec["delta_close_vs_deals"] == 0
        # ops balance terpisah & tidak masuk profit
        assert rec["balance_ops"]["count"] == 2
        assert rec["balance_ops"]["total"] == 3000.0

    def test_partial_close_lintas_interval(self, make_client, cfg):
        _CURRENT_DATASET["ds"] = partial_cross_dataset()
        c = make_client(dataset=partial_cross_dataset())
        _sync_dataset(cfg)
        rec = _recon(c, **{"from": OCT_FROM, "to": OCT_TO_EXCL,
                           "to_exclusive": "1"})
        # deals dalam interval: +30,000 (partial out 20 Okt) & komisi -4
        assert rec["from_deals"]["net"] == 29996.0
        # trade belum close di interval -> by_close 0
        assert rec["by_close"]["trades"] == 0
        assert rec["by_close"]["net_profit"] == 0.0
        # by_position: trade penuh +39,996 (30,000 + 10,004 - 2x4 komisi)
        assert rec["by_position"]["net_profit"] == 39996.0
        assert rec["delta_close_vs_deals"] == -29996.0

    def test_rekonsiliasi_tanpa_filter(self, oct_client):
        rec = _recon(oct_client)
        # semua dataset: Sep (-1,564,837.89) + Okt (+22,036.79)
        # + partial tidak ada -> net deals = net by_close
        assert rec["delta_close_vs_deals"] == 0
