"""Analitik — kasus #7: winrate, PF, expectancy, pip per simbol, RR N/A,
equity curve + drawdown (deposit tidak dihitung drawdown)."""
import copy

from mt5client.mock import MockMT5Client
from services.analytics import compute_stats, equity_curve, enrich_trade


def _stats(conn, cfg, **kw):
    return compute_stats(conn, 1, cfg.symbol_config, **kw)


def test_core_statistics(conn, cfg, svc, synced):
    s = _stats(conn, cfg)
    assert s["total_closed"] == 2
    assert s["open_trades"] == 1
    assert s["wins"] == 1 and s["losses"] == 1 and s["breakeven"] == 0
    assert s["win_rate"] == 50.0
    assert s["gross_profit"] == 195.0
    assert s["gross_loss"] == 23.0
    assert s["net_profit"] == 172.0
    assert s["profit_factor"] == 8.48            # 195/23
    assert s["average_win"] == 195.0
    assert s["average_loss"] == 23.0
    assert s["expectancy"] == 86.0               # 0.5*195 - 0.5*23
    assert s["total_volume"] == 0.7              # 0.2 + 0.5


def test_pip_per_symbol_from_config(conn, cfg, svc, synced):
    """pip_distance = |exit-entry|/pip_size — pip_size per simbol."""
    s = _stats(conn, cfg)
    meta = {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM mt5_symbols")}
    # XAUUSD: |2013.75-2002.50| / 0.1 = 112.5 pips
    t101 = enrich_trade({"symbol": "XAUUSD", "direction": "buy",
                         "entry_price": 2002.50, "exit_price": 2013.75,
                         "sl_initial": 1995.00, "net_profit": 195.0},
                        meta, cfg.symbol_config)
    assert t101["pip_size"] == 0.1
    assert t101["pip_distance"] == 112.5
    assert t101["pips"] == 112.5
    assert t101["rr"] == 1.5                     # risk 75 pips
    # EURUSD: |1.0996-1.1000| / 0.0001 = 4.0 pips, arah sell-of-buy -> -4.0
    t110 = enrich_trade({"symbol": "EURUSD", "direction": "buy",
                         "entry_price": 1.10000, "exit_price": 1.09960,
                         "sl_initial": 1.09500, "net_profit": -23.0},
                        meta, cfg.symbol_config)
    assert t110["pip_size"] == 0.0001
    assert t110["pips"] == -4.0
    assert t110["rr"] == 0.08


def test_rr_na_without_initial_sl(conn, cfg):
    """RR jujur: tanpa SL awal -> rr None (tampil N/A), bukan angka karangan."""
    t = enrich_trade({"symbol": "XAUUSD", "direction": "buy",
                      "entry_price": 2000.0, "exit_price": 2010.0,
                      "sl_initial": None, "net_profit": 100.0}, {}, {})
    assert t["rr"] is None


def test_symbol_override_pip(conn, cfg):
    cfg.symbol_config = {"XAUUSD": {"pip_size": 0.5}}
    t = enrich_trade({"symbol": "XAUUSD", "direction": "buy",
                      "entry_price": 2000.0, "exit_price": 2010.0,
                      "sl_initial": None, "net_profit": 0}, {}, cfg.symbol_config)
    assert t["pip_distance"] == 20.0             # 10 / 0.5


def test_period_buckets(conn, cfg, svc, synced):
    s = _stats(conn, cfg)
    # semua trade pada 2026-10-01 (UTC) — skenario deterministik
    assert list(s["daily"].keys()) == ["2026-10-01"]
    assert list(s["monthly"].keys()) == ["2026-10"]
    wk = list(s["weekly"].keys())[0]
    assert wk.startswith("2026-W")
    d = s["daily"]["2026-10-01"]
    assert d["trades"] == 2 and d["net_profit"] == 172.0


def test_by_symbol_breakdown(conn, cfg, svc, synced):
    s = _stats(conn, cfg)
    x = s["by_symbol"]["XAUUSD"]
    e = s["by_symbol"]["EURUSD"]
    assert x["trades"] == 1 and x["net_profit"] == 195.0
    assert e["trades"] == 1 and e["net_profit"] == -23.0


def test_equity_curve_and_drawdown(conn, cfg, svc, synced):
    c = equity_curve(conn, 1, cfg.symbol_config)
    types = [p["type"] for p in c["points"]]
    # urut: deposit 1000 -> trade +195 -> trade -23 -> withdrawal -250
    assert types == ["deposit", "trade", "trade", "withdrawal"]
    final = c["points"][-1]
    assert final["net_cum"] == 172.0
    assert final["balance_ops_cum"] == 750.0     # 1000 - 250
    # drawdown trading saja: puncak 1195 -> 1172 = 23 (withdrawal TIDAK dihitung)
    assert c["max_drawdown"] == 23.0
    assert c["max_drawdown_at"] is not None


def test_stats_filter_symbol(conn, cfg, svc, synced):
    s = _stats(conn, cfg, symbol="EURUSD")
    assert s["total_closed"] == 1
    assert s["net_profit"] == -23.0
