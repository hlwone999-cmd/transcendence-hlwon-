"""Sinkronisasi — kasus #1, #2, #8: deals masuk, sync berulang tanpa
duplikat, overlap incremental, dan error koneksi tidak crash."""
import copy

from mt5client.mock import MockMT5Client, scenario_dataset


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def test_initial_sync_inserts_everything(svc, conn, synced):
    summary = synced
    assert summary["deals_upserted"] == 10
    assert summary["orders_upserted"] == 4
    assert summary["trades_updated"] == 3      # 101, 105 (open), 110
    assert summary["open_positions"] == 1
    assert _count(conn, "mt5_accounts") == 1
    assert _count(conn, "mt5_deals") == 10     # termasuk deposit+withdrawal
    assert _count(conn, "mt5_orders") == 4
    assert _count(conn, "trades") == 3
    # symbol cache terisi -> definisi pip tersedia
    syms = {r["symbol"]: r["pip_size"] for r in
            conn.execute("SELECT symbol, pip_size FROM mt5_symbols")}
    assert syms["XAUUSD"] == 0.1               # point 0.01 -> pip 0.1 (bukan hardcode)
    assert syms["EURUSD"] == 0.0001            # point 0.00001 -> pip 0.0001


def test_repeated_sync_no_duplicates(svc, conn, synced):
    for _ in range(3):
        s = svc.sync(mode="incremental")
        assert s["status"] == "ok", s
    assert _count(conn, "mt5_deals") == 10
    assert _count(conn, "mt5_orders") == 4
    assert _count(conn, "trades") == 3
    n_tickets = conn.execute(
        "SELECT COUNT(DISTINCT ticket) AS n FROM mt5_deals").fetchone()["n"]
    assert n_tickets == _count(conn, "mt5_deals")
    # data historis tidak berubah
    t = conn.execute(
        "SELECT net_profit FROM trades WHERE position_id=101").fetchone()
    assert t["net_profit"] == 195.0


def test_incremental_picks_up_new_deals(svc, conn, cfg, synced):
    """Deal baru datang -> sync berikutnya menambah, tanpa duplikat."""
    ds = scenario_dataset()
    # posisi baru 120: BUY 0.10@2030 (IN) lalu OUT 0.10@2035 profit +45
    extra_in = ds["deals"][2]._replace(ticket=120, order=520, position_id=120,
                                       volume=0.10, price=2030.0,
                                       commission=0.0)
    extra_out = ds["deals"][4]._replace(ticket=121, order=521, position_id=120,
                                        volume=0.10, price=2035.0, profit=45.0)
    ds2 = copy.deepcopy(scenario_dataset())
    ds2["deals"] = ds2["deals"] + [extra_in, extra_out]

    svc2 = _swap_provider(svc, MockMT5Client(ds2))
    s = svc2.sync(mode="incremental")
    assert s["status"] == "ok"
    assert _count(conn, "mt5_deals") == 12
    assert _count(conn, "trades") == 4
    row = conn.execute(
        "SELECT net_profit, status FROM trades WHERE position_id=120").fetchone()
    assert row["net_profit"] == 45.0 and row["status"] == "closed"
    n_tickets = conn.execute(
        "SELECT COUNT(DISTINCT ticket) AS n FROM mt5_deals").fetchone()["n"]
    assert n_tickets == 12


def _swap_provider(svc, mock):
    svc._factory = lambda: mock
    return svc


def test_connection_error_is_logged_not_crash(svc, conn, cfg):
    """Kasus #8: mt5server mati -> status error, backend tetap hidup."""
    bad = MockMT5Client(fail_connect=True)
    svc2 = _swap_provider(svc, bad)
    summary = svc2.sync(mode="initial")
    assert summary["status"] == "error"
    assert "tidak terjangkau" in summary["error"]
    # password TIDAK PERNAH bocor ke error/log
    assert "SUPER_SECRET_PASSWORD" not in (summary["error"] or "")
    log = conn.execute(
        "SELECT error, status FROM sync_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert log["status"] == "error"
    assert "SUPER_SECRET_PASSWORD" not in (log["error"] or "")
    # DB tetap kosong-trade, tapi struktur siap
    assert _count(conn, "trades") == 0


def test_history_error_is_logged(svc, conn):
    bad = MockMT5Client(fail_history=True)
    svc2 = _swap_provider(svc, bad)
    summary = svc2.sync(mode="initial")
    assert summary["status"] == "error"
    assert _count(conn, "trades") == 0


def test_sync_state_tracks_last_deal(svc, conn, synced):
    state = conn.execute("SELECT * FROM sync_state").fetchone()
    assert state["initial_done"] == 1
    assert state["last_sync_status"] == "ok"
    # deal trading terakhir = ticket 112 @ t(90)
    deals = conn.execute(
        "SELECT MAX(time_utc) AS m FROM mt5_deals WHERE deal_type IN (0,1)"
    ).fetchone()
    assert state["last_deal_time_utc"] == deals["m"]


def test_overlap_recaptures_boundary(svc, conn, cfg):
    """Deal yang 'muncul terlambat' dekat batas rentang tetap tertangkap
    pada sync berikutnya karena overlap — tanpa duplikat."""
    ds = scenario_dataset()
    svc2 = _swap_provider(svc, MockMT5Client(ds))
    assert svc2.sync(mode="initial")["status"] == "ok"
    # simulasi deal telat: ticket baru yang waktunya lama
    late = ds["deals"][2]._replace(ticket=900, order=590, position_id=900,
                                   volume=0.01, price=2000.0)
    ds2 = copy.deepcopy(scenario_dataset())
    ds2["deals"] = ds2["deals"] + [late, ds["deals"][4]._replace(
        ticket=901, order=591, position_id=900, volume=0.01, price=2002.0,
        profit=2.0)]
    _swap_provider(svc2, MockMT5Client(ds2))
    s = svc2.sync(mode="incremental")
    assert s["status"] == "ok", s
    n = conn.execute("SELECT COUNT(*) AS n FROM mt5_deals").fetchone()["n"]
    assert n == 12
