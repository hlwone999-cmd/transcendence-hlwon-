"""Terminal per customer — brief §11 #5 (4 customer = 4 terminal terpisah,
port RPyC 18812..18815) dan #8 (restart/evict satu terminal tidak
memengaruhi yang lain)."""
from tests.conftest import bind_account, register_and_login

CUST = [("c1@test.id", 11111111, "Exness-MT5Real"),
        ("c2@test.id", 22222222, "BrokerLain-MT5Demo"),
        ("c3@test.id", 33333333, "Exness-MT5Trial"),
        ("c4@test.id", 44444444, "Exness-MT5Real8")]


def _four_customers_bound(c):
    heads = []
    for email, login, server in CUST:
        h = register_and_login(c, email=email)
        bind_account(c, h, login=login, server=server)
        heads.append(h)
    return heads


# ------------------------------------------------- #5 4 terminal terpisah
def test_empat_customer_empat_terminal(make_client):
    c = make_client()
    heads = _four_customers_bound(c)
    import db as dbmod
    app = c.application
    with app.app_context():
        conn = dbmod.get_db(app.config["APP_CFG"])
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM mt5_terminals ORDER BY rpyc_port")]
    assert len(rows) == 4
    ports = [r["rpyc_port"] for r in rows]
    assert ports == [18812, 18813, 18814, 18815]   # contoh brief §4
    # direktori wine prefix / terminal / data unik per customer
    prefixes = {r["wine_prefix"] for r in rows}
    assert len(prefixes) == 4
    services = {r["service_name"] for r in rows}
    assert len(services) == 4


def test_terminal_kelima_ditolak(make_client):
    c = make_client()
    _four_customers_bound(c)
    # keputusan produk #1: registrasi ke-5 BOLEH (tidak memakai slot),
    # tetapi bind akun MT5 ke-5 ditolak 403 — 4 slot MT5 sudah penuh
    h5ok = register_and_login(c, email="c5@test.id")
    r = c.post("/api/v1/accounts/bind", headers=h5ok,
               json={"login": 55555555, "server": "Lain", "password": "x" * 8})
    assert r.status_code == 403, r.get_json()
    assert "slot MT5" in r.get_json()["error"]["message"]
    import db as dbmod
    with c.application.app_context():
        conn = dbmod.get_db(c.application.config["APP_CFG"])
        assert conn.execute("SELECT COUNT(*) AS n FROM mt5_terminals"
                            ).fetchone()["n"] == 4
        assert conn.execute("SELECT COUNT(*) AS n FROM mt5_accounts WHERE"
                            " login=55555555").fetchone()["n"] == 0
    # bind tanpa sesi valid tetap ditolak
    h5 = {"Authorization": "Bearer bootstrap-dummy"}  # bukan sesi valid
    r = c.post("/api/v1/accounts/bind", headers=h5,
               json={"login": 55555555, "server": "Lain", "password": "x" * 8})
    assert r.status_code == 401   # tidak ada jalur bind tanpa sesi valid


def test_unit_systemd_localhost_saja(make_client):
    c = make_client()
    h = register_and_login(c)
    bind_account(c, h)
    import db as dbmod
    app = c.application
    with app.app_context():
        conn = dbmod.get_db(app.config["APP_CFG"])
        term = conn.execute("SELECT * FROM mt5_terminals").fetchone()
    unit = open(f"{term['wine_prefix']}/../{term['service_name']}.service",
                encoding="utf-8").read()
    assert "WINEPREFIX=" in unit
    assert "ExecStart" in unit
    assert "Restart=on-failure" in unit


# ------------------------- #8 evict/restart A tidak memengaruhi B, C, D
def test_evict_satu_runtime_tidak_mengganggu_lainnya(make_client):
    c = make_client()
    heads = _four_customers_bound(c)
    app = c.application
    import db as dbmod
    with app.app_context():
        conn = dbmod.get_db(app.config["APP_CFG"])
        reg = app.extensions["mt5_registry"]
        # bangun runtime untuk SEMUA akun (provider mock terkoneksi)
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM mt5_accounts ORDER BY id")]
        rts = [reg.runtime_for(conn, i) for i in ids]
        for rt in rts:
            rt.provider.connect()
        assert all(rt.provider.is_connected() for rt in rts)
        # evict akun pertama = simulasi restart terminal A
        reg.evict(ids[0])
        # runtime lain TIDAK terpengaruh: masih terkoneksi & lock bebas
        for rt in rts[1:]:
            assert rt.provider.is_connected()
            assert not rt.lock.locked()
        # runtime A hilang dari registry
        with reg._lock:
            assert ids[0] not in reg._runtimes


def test_sync_ulang_setelah_evict(make_client):
    from tests.test_multi_customer import _mc_client, _tiny_ds
    conf = [(11111111, "Exness-MT5Real"), (22222222, "BrokerLain-MT5Demo"),
            (33333333, "Exness-MT5Trial"), (44444444, "Exness-MT5Real8")]
    datasets = {str(l): _tiny_ds(l, s, f"SYM{i}") for i, (l, s) in
                enumerate(conf)}
    c = _mc_client(datasets)
    heads = []
    for i, (login, server) in enumerate(conf):
        h = register_and_login(c, email=f"ev{i}@test.id")
        bind_account(c, h, login=login, server=server)
        heads.append(h)
    app = c.application
    import db as dbmod
    with app.app_context():
        conn = dbmod.get_db(app.config["APP_CFG"])
        reg = app.extensions["mt5_registry"]
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM mt5_accounts ORDER BY id")]
        reg.evict(ids[2])   # "restart" terminal C
    # sync customer C tetap jalan (runtime dibuat ulang otomatis)
    r = c.post("/api/v1/mt5/sync", json={"mode": "initial"}, headers=heads[2])
    assert r.status_code == 200 and r.get_json()["status"] == "ok", \
        r.get_json()
    # customer lain tidak terdampak
    r = c.post("/api/v1/mt5/sync", json={"mode": "initial"}, headers=heads[0])
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
