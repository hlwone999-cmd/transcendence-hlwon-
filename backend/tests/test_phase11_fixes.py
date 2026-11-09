"""Phase 1.1 — regresi perbaikan backend:

1. Temuan 6: max_drawdown_pct harus None (N/A di UI) bila puncak P/L
   kumulatif <= 0 — dahulu 0.0 yang menyesatkan. Nilai 0% sah HANYA
   bila puncak valid (> 0) dan tidak ada drawdown.
2. Bug to_exclusive pada equity_curve: cabang balance-ops dahulu SELALU
   memakai `<=` sehingga operasi balance tepat di batas `to` ikut
   terhitung — inkonsisten dengan cabang trades dan rekonsiliasi.
   Kontrak interval [from, to) wajib dihormati di SEMUA cabang.

Dataset SINTETIS kecil — bukan data produksi. Waktu 1-2 Okt 2026 UTC
agar masuk window fetch sync (mode full: dari 2000-01-01).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mt5client.mock import (
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_TYPE_BALANCE, DEAL_TYPE_BUY,
    DEAL_TYPE_SELL, AccountInfo, MockMT5Client, SymbolInfo, TradeDeal,
)
from services.analytics import equity_curve, reconciliation
from services.sync import SyncService


def _sec(y, mo, d, h=0, mi=0, s=0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


CLOSE_10 = _sec(2026, 10, 1, 10)  # 2026-10-01T10:00:00Z
DEPO_11 = _sec(2026, 10, 1, 11)   # 2026-10-01T11:00:00Z

SYMBOLS = {
    "XAUUSD": SymbolInfo(name="XAUUSD", point=0.01, digits=2,
                         trade_tick_size=0.01, trade_tick_value=1.0,
                         trade_contract_size=100.0, description="Gold"),
}

ACCOUNT = AccountInfo(
    login=12345678, trade_mode=0, leverage=500, limit_orders=200,
    margin_so_mode=0, trade_allowed=False, trade_expert=False,
    margin_mode=0, currency_digits=2, fifo_close=False,
    balance=0.0, credit=0.0, profit=0.0, equity=0.0,
    margin=0.0, margin_free=0.0, margin_level=0.0,
    margin_so_call=50.0, margin_so_so=20.0, margin_initial=0.0,
    margin_maintenance=0.0, assets=0.0, liabilities=0.0,
    commission_blocked=False, name="Uji", email="t@example.com",
    server="Exness-MT5Real", currency="USD", company="Exness",
)


def _deal(ticket, order, sec, dtype, entry, position_id, volume, price,
          profit=0.0, commission=0.0, swap=0.0, fee=0.0, symbol="XAUUSD",
          comment="", magic=777):
    return TradeDeal(
        ticket=ticket, order=order, time=sec, time_msc=sec * 1000,
        type=dtype, entry=entry, magic=magic, position_id=position_id,
        reason=0, volume=volume, price=price, commission=commission,
        swap=swap, profit=profit, symbol=symbol, comment=comment,
        external_id=0, fee=fee,
    )


def _simple_trade(tid, close_sec, net):
    """1 deal IN + 1 deal OUT; komisi 0 -> net = profit.
    Open = close - 1 jam (2026 UTC, di dalam window fetch)."""
    return [
        _deal(tid * 10 + 1, tid, close_sec - 3600, DEAL_TYPE_BUY,
              DEAL_ENTRY_IN, tid, 0.10, 2000.0),
        _deal(tid * 10 + 2, tid + 1, close_sec, DEAL_TYPE_SELL,
              DEAL_ENTRY_OUT, tid, 0.10, 2010.0, profit=net),
    ]


def _dataset(deals):
    return {"deals": deals, "orders": [], "positions": [],
            "symbols": SYMBOLS, "account": ACCOUNT}


def _sync(conn, cfg, dataset):
    """Isi DB via SyncService + mock dataset (mode full: dari 2000)."""
    import db as dbmod
    dbmod.close_db()
    svc = SyncService(cfg, lambda: MockMT5Client(dataset))
    summary = svc.sync(mode="full")
    assert summary["status"] == "ok", summary
    dbmod.close_db()
    return conn


# --------------------------------------------------------------- Temuan 6
def test_dd_pct_none_when_peak_never_positive(conn, cfg):
    """Semua trade rugi -> puncak kumulatif 0 -> persen TIDAK valid.

    Dahulu: max_drawdown_pct = 0.0 (menyesatkan — nominal DD ada).
    Sekarang: None -> UI tampil 'N/A'.
    """
    deals = _simple_trade(21, _sec(2026, 10, 1, 9), -500.0)
    deals += _simple_trade(22, _sec(2026, 10, 1, 11), -300.0)
    _sync(conn, cfg, _dataset(deals))

    c = equity_curve(conn, 1, cfg.symbol_config)
    assert c["final_net_cum"] == -800.0
    # nominal DD valid: terburuk -800 diukur dari puncak 0
    assert c["max_drawdown"] == 800.0
    assert c["max_drawdown_at"] is not None
    # INTI FIX: persen tanpa penyebut valid -> None (UI: N/A, bukan 0%)
    assert c["max_drawdown_pct"] is None


def test_dd_pct_kept_when_peak_valid(conn, cfg, svc, synced):
    """Puncak valid: +195 lalu -23 -> DD 23 = 11,79% (tetap angka)."""
    c = equity_curve(conn, 1, cfg.symbol_config)
    assert c["max_drawdown"] == 23.0
    assert c["max_drawdown_pct"] == 11.79  # 23/195*100
    assert c["max_drawdown_at"] is not None


def test_dd_pct_zero_is_valid_when_no_drawdown(conn, cfg):
    """Kurva untung terus: DD 0 dengan puncak valid -> 0% sah (bukan N/A)."""
    deals = _simple_trade(31, _sec(2026, 10, 1, 9), 100.0)
    deals += _simple_trade(32, _sec(2026, 10, 1, 10), 50.0)
    _sync(conn, cfg, _dataset(deals))
    c = equity_curve(conn, 1, cfg.symbol_config)
    assert c["final_net_cum"] == 150.0
    assert c["max_drawdown"] == 0.0
    assert c["max_drawdown_pct"] == 0.0


# ------------------------------------------- bug to_exclusive balance-ops
def test_balance_ops_honor_to_exclusive(conn, cfg):
    """Deal_type=2 TEPAT di batas `to` eksklusif: tidak boleh terhitung.

    Dahulu cabang balance-ops equity_curve SELALU memakai `<=` —
    deposit 11:00:00 ikut masuk padahal to_exclusive=1.
    """
    deals = _simple_trade(41, CLOSE_10, 100.0)  # close tepat 10:00:00
    deals.append(_deal(490, 0, DEPO_11, DEAL_TYPE_BALANCE, DEAL_ENTRY_IN, 0,
                       0.0, 0.0, profit=500.0, symbol="", comment="Deposit"))
    _sync(conn, cfg, _dataset(deals))

    c = equity_curve(conn, 1, cfg.symbol_config,
                     date_to="2026-10-01T11:00:00Z", to_exclusive=True)
    assert c["balance_ops_cum"] == 0.0, (
        "deposit tepat di batas eksklusif tidak boleh masuk")
    assert c["final_net_cum"] == 100.0
    assert [p["type"] for p in c["points"]] == ["trade"]


def test_balance_ops_inclusive_when_not_exclusive(conn, cfg):
    """Tanpa to_exclusive (mode legacy): perilaku inklusif dipertahankan."""
    deals = _simple_trade(41, CLOSE_10, 100.0)
    deals.append(_deal(490, 0, DEPO_11, DEAL_TYPE_BALANCE, DEAL_ENTRY_IN, 0,
                       0.0, 0.0, profit=500.0, symbol="", comment="Deposit"))
    _sync(conn, cfg, _dataset(deals))

    c = equity_curve(conn, 1, cfg.symbol_config,
                     date_to="2026-10-01T11:00:00Z", to_exclusive=False)
    assert c["balance_ops_cum"] == 500.0
    assert c["points"][-1]["type"] == "deposit"


def test_trades_branch_unchanged(conn, cfg):
    """Cabang trades tidak berubah: close TEPAT 10:00 ikut aturan sama."""
    deals = _simple_trade(41, CLOSE_10, 100.0)
    deals.append(_deal(490, 0, DEPO_11, DEAL_TYPE_BALANCE, DEAL_ENTRY_IN, 0,
                       0.0, 0.0, profit=500.0, symbol="", comment="Deposit"))
    _sync(conn, cfg, _dataset(deals))

    to = "2026-10-01T10:00:00Z"
    c_ex = equity_curve(conn, 1, cfg.symbol_config,
                        date_to=to, to_exclusive=True)
    assert c_ex["final_net_cum"] == 0.0
    assert c_ex["points"] == []
    c_in = equity_curve(conn, 1, cfg.symbol_config,
                        date_to=to, to_exclusive=False)
    assert c_in["final_net_cum"] == 100.0


def test_consistency_with_reconciliation(conn, cfg):
    """equity_curve & reconciliation kini SEPAKAT di batas eksklusif."""
    deals = _simple_trade(41, CLOSE_10, 100.0)
    deals.append(_deal(490, 0, DEPO_11, DEAL_TYPE_BALANCE, DEAL_ENTRY_IN, 0,
                       0.0, 0.0, profit=500.0, symbol="", comment="Deposit"))
    _sync(conn, cfg, _dataset(deals))

    rec = reconciliation(conn, 1, date_to="2026-10-01T11:00:00Z",
                         to_exclusive=True)
    assert rec["balance_ops"]["count"] == 0
    assert rec["balance_ops"]["total"] == 0.0
    c = equity_curve(conn, 1, cfg.symbol_config,
                     date_to="2026-10-01T11:00:00Z", to_exclusive=True)
    assert c["balance_ops_cum"] == 0.0
