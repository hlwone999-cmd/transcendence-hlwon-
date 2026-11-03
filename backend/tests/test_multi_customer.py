"""Multi-customer — brief §11 #4 (1 akun/customer), #6 (A tidak bisa baca
B), #7 (manipulasi ID tidak memberi akses), #9 (gagal A tidak hentikan
B), #10 (sync berulang tidak duplikat), #11 (broker berbeda)."""
import tempfile

from cryptography.fernet import Fernet

from app import create_app
from config import Config
from mt5client.mock import (AccountInfo, MockMT5Client, SymbolInfo, _deal,
                            _order, t as mt)
from tests.conftest import bind_account, register_and_login


def _acc(login, server, currency="USD"):
    return AccountInfo(
        login=login, trade_mode=0, leverage=100, limit_orders=200,
        margin_so_mode=0, trade_allowed=True, trade_expert=False,
        margin_mode=0, currency_digits=2, fifo_close=False,
        balance=1000.0, credit=0.0, profit=0.0, equity=1000.0,
        margin=0.0, margin_free=1000.0, margin_level=0.0,
        margin_so_call=50.0, margin_so_so=20.0, margin_initial=0.0,
        margin_maintenance=0.0, assets=0.0, liabilities=0.0,
        commission_blocked=False, name="Uji", email="t@t.id",
        server=server, currency=currency, company=server.split("-")[0])


def _sym(symbol, point, digits):
    return SymbolInfo(name=symbol, point=point, digits=digits,
                      trade_tick_size=point, trade_tick_value=1.0,
                      trade_contract_size=100.0, description=symbol)


def _session(c, email):
    return register_and_login(c, email=email)


def _two_customers(c):
    hA = _session(c, "a@test.id")
    hB = _session(c, "b@test.id")
    bind_account(c, hA, login=11111111, server="Exness-MT5Real")
    bind_account(c, hB, login=22222222, server="BrokerLain-MT5Demo")
    return hA, hB


def _tiny_ds(login, server, symbol):
    return {"account": _acc(login, server), "deals": [], "orders": [],
            "positions": [], "symbols": {symbol: _sym(symbol, 0.01, 2)}}


def _ds_two_trades(login, server, symbol, point, digits, pid, profit):
    """1 posisi = 2 deal (in/out) dgn profit tertentu, komisi per deal 2."""
    deals = [
        _deal(pid * 10, pid * 10, 10, 0, 0, pid, 0.1, 2000.0,
              commission=-2.0, symbol=symbol),
        _deal(pid * 10 + 1, pid * 10 + 1, 20, 1, 1, pid, 0.1, 2010.0,
              profit=profit, commission=-2.0, symbol=symbol),
    ]
    return {"account": _acc(login, server), "deals": deals, "orders": [],
            "positions": [],
            "symbols": {symbol: _sym(symbol, point, digits)}}


def _mc_client(datasets, mt5_enabled=True):
    """Client dgn provider mock dipilih per login akun (multi-customer)."""
    cfg = Config()
    tmp = tempfile.mkdtemp(prefix="mc-")
    cfg.db_path = f"{tmp}/journal.db"
    cfg.api_token = "TESTTOKEN"
    cfg.mt5_enabled = mt5_enabled
    cfg.cred_key = Fernet.generate_key().decode("ascii")
    cfg.term_base_dir = f"{tmp}/terminals"
    cfg.terminal_provision_mode = "mock"
    cfg.max_customers = 4
    app = create_app(cfg, provider_factory=lambda acc_cfg=None: MockMT5Client(
        datasets[str(acc_cfg.mt5_login)] if acc_cfg else None))
    app.config["TESTING"] = True
    return app.test_client()


# ------------------------------------------------------------ #4 satu akun
def test_satu_akun_per_customer(client):
    hA = _session(client, "satu@test.id")
    bind_account(client, hA, login=11111111, server="Exness-MT5Real")
    r = client.post("/api/v1/accounts/bind", headers=hA,
                    json={"login": 99999999, "server": "Lain",
                          "password": "x" * 10})
    assert r.status_code == 409
    assert "1 akun" in r.get_json()["error"]["message"]


def test_akun_sama_tidak_bisa_dibind_dua_customer(client):
    hA = _session(client, "sama1@test.id")
    hB = _session(client, "sama2@test.id")
    bind_account(client, hA, login=77777777, server="Exness-MT5Real")
    r = client.post("/api/v1/accounts/bind", headers=hB,
                    json={"login": 77777777, "server": "Exness-MT5Real",
                          "password": "x" * 10})
    assert r.status_code == 409
    assert "customer lain" in r.get_json()["error"]["message"]


def test_unbind_lalu_bind_lagi(client):
    hA = _session(client, "ganti@test.id")
    bind_account(client, hA, login=55500001, server="Exness-MT5Real")
    r = client.post("/api/v1/accounts/unbind", headers=hA)
    assert r.status_code == 200
    bind_account(client, hA, login=55500002, server="BrokerBaru-MT5Demo")
    acc = client.get("/api/v1/mt5/account", headers=hA).get_json()
    assert acc["login"] == 55500002
    assert client.get("/api/v1/journal/trades",
                      headers=hA).get_json()["total"] == 0


# --------------------------------------------- #6 A tidak bisa membaca B
def test_isolasi_data_antar_customer():
    ds_a = _ds_two_trades(11111111, "Exness-MT5Real", "XAUUSD", 0.01, 2,
                          11, 100.0)
    ds_b = _ds_two_trades(22222222, "BrokerLain-MT5Demo", "EURUSD",
                          0.00001, 5, 55, 200.0)
    c = _mc_client({"11111111": ds_a, "22222222": ds_b})
    hA, hB = _two_customers(c)
    for h in (hA, hB):
        r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=h)
        assert r.status_code == 200 and r.get_json()["status"] == "ok"

    ta = c.get("/api/v1/journal/trades", headers=hA).get_json()
    tb = c.get("/api/v1/journal/trades", headers=hB).get_json()
    assert ta["total"] == 1 and tb["total"] == 1
    assert ta["items"][0]["symbol"] == "XAUUSD"
    assert tb["items"][0]["symbol"] == "EURUSD"
    sa = c.get("/api/v1/journal/stats", headers=hA).get_json()
    assert sa["net_profit"] == 96.0        # 100 - 2x2 komisi
    sb = c.get("/api/v1/journal/stats", headers=hB).get_json()
    assert sb["net_profit"] == 196.0       # 200 - 2x2 komisi


# ------------------------------------- #7 manipulasi ID tidak memberi akses
def test_manipulasi_account_id_tidak_berpengaruh(client):
    hA, hB = _two_customers(client)
    id_a = client.get("/api/v1/mt5/account", headers=hA).get_json()["id"]
    id_b = client.get("/api/v1/mt5/account", headers=hB).get_json()["id"]
    assert id_a != id_b
    # tipukan via query param: tetap mendapat akun milik sendiri
    r = client.get(f"/api/v1/mt5/account?account_id={id_b}&customer_id=999",
                   headers=hA)
    assert r.status_code == 200
    assert r.get_json()["id"] == id_a
    # tipukan via body pada sync: sync tetap pada akun milik A
    s = client.post("/api/v1/mt5/sync",
                    json={"mode": "incremental", "account_id": id_b,
                          "customer_id": 999}, headers=hA)
    assert s.status_code == 200
    assert s.get_json()["account_id"] == id_a


def test_detail_trade_customer_lain_404():
    ds_a = _ds_two_trades(11111111, "Exness-MT5Real", "XAUUSD", 0.01, 2,
                          11, 100.0)
    ds_b = _ds_two_trades(22222222, "BrokerLain-MT5Demo", "EURUSD",
                          0.00001, 5, 55, 200.0)
    c = _mc_client({"11111111": ds_a, "22222222": ds_b})
    hA, hB = _two_customers(c)
    for h in (hA, hB):
        c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=h)
    # position_id 55 milik B — A minta detail itu -> 404
    r = c.get("/api/v1/journal/trades/55", headers=hA)
    assert r.status_code == 404
    # milik sendiri tetap 200
    assert c.get("/api/v1/journal/trades/11", headers=hA).status_code == 200


# --------------------------- #9 gagal sync A tidak menghentikan B, C, D
def test_kegagalan_sinkron_satu_akun_terisolasi():
    conf = [(11111111, "Exness-MT5Real", "XAUUSD"),
            (22222222, "BrokerLain-MT5Demo", "EURUSD"),
            (33333333, "Exness-MT5Trial", "GBPUSD"),
            (44444444, "Exness-MT5Real8", "BTCUSD")]
    datasets = {str(l): _tiny_ds(l, s, sy) for l, s, sy in conf}
    c = _mc_client(datasets)
    heads = []
    for i, (login, server, _sym) in enumerate(conf):
        h = _session(c, f"iso{i}@test.id")
        bind_account(c, h, login=login, server=server)
        heads.append(h)
    # akun pertama dibuat gagal connect
    import db as dbmod
    app = c.application
    with app.app_context():
        conn = dbmod.get_db(app.config["APP_CFG"])
        acc0 = conn.execute("SELECT id FROM mt5_accounts ORDER BY id"
                            " LIMIT 1").fetchone()["id"]
        rt0 = app.extensions["mt5_registry"].runtime_for(conn, acc0)
        rt0.provider.fail_connect = True
    results = [c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=h)
               for h in heads]
    assert results[0].status_code == 200
    assert results[0].get_json()["status"] == "error"
    for r in results[1:]:
        assert r.status_code == 200
        assert r.get_json()["status"] == "ok", r.get_json()


# ------------------------------- #10 sync berulang tidak menggandakan data
def test_sync_berulang_tidak_duplikat():
    ds_a = _ds_two_trades(11111111, "Exness-MT5Real", "XAUUSD", 0.01, 2,
                          11, 100.0)
    c = _mc_client({"11111111": ds_a, "22222222": _tiny_ds(
        22222222, "BrokerLain-MT5Demo", "EURUSD")})
    hA, _ = _two_customers(c)
    for _ in range(3):
        r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=hA)
        assert r.status_code == 200 and r.get_json()["status"] == "ok"
    assert c.get("/api/v1/journal/trades",
                 headers=hA).get_json()["total"] == 1
    acc = c.get("/api/v1/mt5/account", headers=hA).get_json()
    import db as dbmod
    with c.application.app_context():
        conn = dbmod.get_db(c.application.config["APP_CFG"])
        deals = conn.execute(
            "SELECT COUNT(*) AS n FROM mt5_deals WHERE account_id=?",
            (acc["id"],)).fetchone()["n"]
        assert deals == 2                        # bukan 6


# -------------------------------------- #11 broker/server berbeda per customer
def test_broker_berbeda_dapat_dikonfigurasi(client):
    hA, hB = _two_customers(client)
    a = client.get("/api/v1/mt5/account", headers=hA).get_json()
    b = client.get("/api/v1/mt5/account", headers=hB).get_json()
    assert a["server"] == "Exness-MT5Real"
    assert b["server"] == "BrokerLain-MT5Demo"
    assert a["login"] != b["login"]
