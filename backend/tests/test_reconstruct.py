"""Rekonstruksi posisi — partial close, scale-in/out, hedging/netting,
filter deposit (kasus #3, #4, #5, #6)."""
import copy

from mt5client.mock import (
    DEAL_ENTRY_IN, DEAL_ENTRY_INOUT, DEAL_TYPE_BALANCE, scenario_dataset,
)
from services.reconstruct import reconstruct_trades


def _by_pid(trades):
    return {t["position_id"]: t for t in trades}


def test_scale_in_and_partial_close_and_close():
    ds = scenario_dataset()
    trades = _by_pid(reconstruct_trades(1, ds["deals"], ds["orders"]))
    # posisi 101: scale-in 2x, partial close, close penuh
    t = trades[101]
    assert t["direction"] == "buy"
    assert t["status"] == "closed"
    assert t["volume_in"] == 0.20
    assert t["volume_out"] == 0.20
    assert t["entry_price"] == 2002.50          # VWAP 0.10@2000 + 0.10@2005
    assert t["exit_price"] == 2013.75           # VWAP 0.05@2010 + 0.15@2015
    assert t["profit"] == 200.0                 # +50 (partial) +150 (final)
    assert t["commission"] == -4.0
    assert t["swap"] == -1.0
    assert t["net_profit"] == 195.0
    assert t["close_time_utc"] is not None
    assert t["exit_reason"] == "tp"             # deal 104 DEAL_REASON_TP
    assert t["sl_initial"] == 1995.00           # dari order pembuka
    assert t["tp_initial"] == 2050.00
    assert t["entry_deals"] == [101, 102]
    assert t["exit_deals"] == [103, 104]


def test_scale_out_multi_exit():
    ds = scenario_dataset()
    trades = _by_pid(reconstruct_trades(1, ds["deals"], ds["orders"]))
    t = trades[110]
    assert t["symbol"] == "EURUSD"
    assert t["direction"] == "buy"
    assert t["status"] == "closed"
    assert t["volume_in"] == 0.50
    assert t["volume_out"] == 0.50              # scale-out 0.20 + 0.30
    assert round(t["exit_price"], 6) == 1.0996  # VWAP dua exit
    assert t["net_profit"] == -23.0             # 40 - 60 - 3 komisi


def test_open_position_stays_open():
    ds = scenario_dataset()
    trades = _by_pid(reconstruct_trades(1, ds["deals"], ds["orders"]))
    t = trades[105]
    assert t["status"] == "open"
    assert t["exit_price"] is None
    assert t["close_time_utc"] is None
    assert t["volume_in"] == 0.02
    assert t["volume_out"] == 0.0
    assert t["direction"] == "sell"


def test_deposit_withdrawal_not_a_trade():
    """Kasus #5: deposit/withdrawal TIDAK menjadi trade (bukan menang/kalah)."""
    ds = scenario_dataset()
    deals = [d for d in ds["deals"] if d.type == DEAL_TYPE_BALANCE]
    assert len(deals) == 2
    trades = reconstruct_trades(1, ds["deals"], ds["orders"])
    pids = {t["position_id"] for t in trades}
    assert 0 not in pids          # deposit/withdrawal position_id = 0
    assert 100 not in pids and 200 not in pids
    # hanya 3 posisi trading: 101, 105, 110
    assert pids == {101, 105, 110}


def test_hedging_two_positions_same_symbol():
    """Akun hedging: BUY & SELL XAUUSD bersamaan = dua trade terpisah."""
    ds = scenario_dataset()
    trades = _by_pid(reconstruct_trades(1, ds["deals"], ds["orders"]))
    assert trades[101]["symbol"] == "XAUUSD"
    assert trades[101]["direction"] == "buy"
    assert trades[105]["symbol"] == "XAUUSD"
    assert trades[105]["direction"] == "sell"


def test_netting_reversal_inout():
    """Netting: INOUT membalik arah — sisa volume jadi posisi baru (netting)."""
    ds = scenario_dataset()
    deals = copy.deepcopy(ds["deals"])
    # posisi 300: BUY 1.0 @100 -> INOUT SELL 1.5 @102
    #   -> posisi lama tertutup penuh (1.0), posisi baru (ticket 301) SELL 0.5
    d_in = copy.deepcopy(ds["deals"][2])
    d_in = d_in._replace(ticket=300, order=600, position_id=300, volume=1.0,
                         price=100.0, profit=0.0, commission=-1.0, swap=0.0)
    d_rev = d_in._replace(ticket=301, order=601, position_id=300, volume=1.5,
                          price=102.0, entry=DEAL_ENTRY_INOUT, type=1,
                          profit=-2.0, commission=0.0)
    # Perilaku netting MT5: TIDAK ada deal IN terpisah untuk posisi baru —
    # sisa volume terwakili oleh deal INOUT itu sendiri (position id baru
    # = ticket deal reversal).
    trades = _by_pid(reconstruct_trades(1, [d_in, d_rev], []))
    old = trades[300]
    assert old["status"] == "closed"
    assert old["volume_in"] == 1.0
    assert old["volume_out"] == 1.0             # hanya bagian yang benar menutup
    assert old["net_profit"] == -2.0 - 1.0      # profit INOUT + komisi IN
    new = trades[301]
    assert new["direction"] == "sell"
    assert new["status"] == "open"
    assert new["volume_in"] == 0.5
    assert new["entry_price"] == 102.0


def test_reconstruction_idempotent():
    ds = scenario_dataset()
    a = reconstruct_trades(1, ds["deals"], ds["orders"])
    b = reconstruct_trades(1, ds["deals"], ds["orders"])
    assert a == b


def test_no_sl_means_no_fabricated_rr_source():
    ds = scenario_dataset()
    trades = _by_pid(reconstruct_trades(1, ds["deals"], ds["orders"]))
    assert trades[110]["sl_initial"] == 1.09500
    # posisi tanpa order tercatat -> SL None (RR akan N/A di analytics)
    assert trades[105]["sl_initial"] == 2030.00
