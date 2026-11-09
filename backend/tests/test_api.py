"""API — validasi (kasus #9), auth, status disconnected, pagination,
detail, stats, equity curve, dan proteksi sync ganda.

Diubah utk model multi-customer: token sesi customer (bukan token
global), akun via bind — alur endpoint nyata (brief §6/§7).
Semua asersi data/filter/statistik dari versi single-tenant
DIPERTAHANKAN (regresi perilaku).
"""
from tests.conftest import bind_account, register_and_login


def test_health_open(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_auth_required_without_session(make_client):
    """Token global statis TIDAK lagi memberi akses data (brief §6)."""
    c = make_client()
    for path in ("/api/v1/mt5/status", "/api/v1/journal/stats",
                 "/api/v1/journal/trades", "/api/v1/mt5/account"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"X-API-Token": "SALAH"}).status_code == 401
        # header lama dgn token acak bukan sesi -> tetap 401
        assert c.get(path, headers={"X-API-Token": "TESTTOKEN"}).status_code \
            == 401
    # sesi valid -> terotorisasi (200/404 sesuai data)
    hdrs = register_and_login(c)
    for path in ("/api/v1/mt5/status", "/api/v1/journal/stats",
                 "/api/v1/journal/trades", "/api/v1/mt5/account"):
        assert c.get(path, headers=hdrs).status_code in (200, 404)


def test_status_disabled_when_mt5_off(client):
    """Backend tanpa terminal MT5: status disconnected, TIDAK crash."""
    hdrs = register_and_login(client)
    r = client.get("/api/v1/mt5/status", headers=hdrs)
    assert r.status_code == 200
    j = r.get_json()
    assert j["connected"] is False
    assert j["reason"] == "disabled"
    assert j["enabled"] is False


def test_status_connected_via_mock(cfg, make_client):
    cfg.mt5_enabled = True
    c = make_client()
    hdrs = register_and_login(c)
    bind_account(c, hdrs)
    r = c.get("/api/v1/mt5/status", headers=hdrs)
    j = r.get_json()
    assert r.status_code == 200
    assert j["connected"] is True
    assert j["live_account"]["login"] == 12345678


def test_account_404_before_bind(client):
    hdrs = register_and_login(client)
    r = client.get("/api/v1/mt5/account", headers=hdrs)
    assert r.status_code == 404
    assert "ter-bind" in r.get_json()["error"]["message"]


def test_full_api_flow_after_sync(client):
    hdrs = register_and_login(client)
    bind_account(client, hdrs)
    r = client.post("/api/v1/mt5/sync", json={"mode": "initial"},
                    headers=hdrs)
    assert r.status_code == 200 and r.get_json()["status"] == "ok"

    # account
    acc = client.get("/api/v1/mt5/account", headers=hdrs).get_json()
    assert acc["login"] == 12345678
    assert acc["server"] == "Exness-MT5Real"

    # trades pagination
    r = client.get("/api/v1/journal/trades?page=1&page_size=2", headers=hdrs)
    j = r.get_json()
    assert r.status_code == 200
    assert j["total"] == 3
    assert j["pages"] == 2
    assert len(j["items"]) == 2

    # filter status open
    j = client.get("/api/v1/journal/trades?status=open",
                   headers=hdrs).get_json()
    assert j["total"] == 1
    assert j["items"][0]["position_id"] == 105

    # filter symbol + direction
    j = client.get(
        "/api/v1/journal/trades?symbol=EURUSD&direction=buy",
        headers=hdrs).get_json()
    assert j["total"] == 1

    # detail
    d = client.get("/api/v1/journal/trades/101", headers=hdrs).get_json()
    assert d["trade"]["net_profit"] == 195.0
    assert len(d["deals"]) == 4
    assert len(d["orders"]) == 2
    assert d["trade"]["rr"] == 1.5
    assert client.get("/api/v1/journal/trades/999",
                      headers=hdrs).status_code == 404

    # stats & equity
    s = client.get("/api/v1/journal/stats", headers=hdrs).get_json()
    assert s["total_closed"] == 2 and s["win_rate"] == 50.0
    e = client.get("/api/v1/journal/equity-curve", headers=hdrs).get_json()
    assert len(e["points"]) == 4
    assert e["max_drawdown"] == 23.0

    # sync status
    ss = client.get("/api/v1/mt5/sync/status", headers=hdrs).get_json()
    assert ss["state"]["last_sync_status"] == "ok"
    assert len(ss["recent_logs"]) == 1

    # symbols
    sy = client.get("/api/v1/symbols", headers=hdrs).get_json()
    assert {x["symbol"] for x in sy["items"]} == {"XAUUSD", "EURUSD"}


def test_api_validation_errors(client):
    hdrs = register_and_login(client)
    bind_account(client, hdrs)
    client.post("/api/v1/mt5/sync", json={"mode": "initial"}, headers=hdrs)
    base = "/api/v1/journal/trades"
    assert client.get(base + "?page=0", headers=hdrs).status_code == 400
    assert client.get(base + "?page=abc", headers=hdrs).status_code == 400
    assert client.get(base + "?page_size=500", headers=hdrs).status_code == 400
    assert client.get(base + "?status=bukan", headers=hdrs).status_code == 400
    assert client.get(base + "?direction=BELI", headers=hdrs).status_code == 400
    assert client.get(base + "?sort=hahaha", headers=hdrs).status_code == 400
    assert client.get(base + "?from=bukan-tanggal",
                      headers=hdrs).status_code == 400
    assert client.get("/api/v1/journal/stats?to=31/12/2026",
                      headers=hdrs).status_code == 400
    body = {"mode": "hacked"}
    assert client.post("/api/v1/mt5/sync", json=body,
                       headers=hdrs).status_code == 400
    # valid dates tetap lolos
    assert client.get(base + "?from=2026-09-01&to=2026-10-31",
                      headers=hdrs).status_code == 200


def test_sync_conflict_returns_409(client):
    """Sync ganda pada akun yang sama ditolak — bukan crash."""
    import db as dbmod
    hdrs = register_and_login(client)
    bind_account(client, hdrs)
    app = client.application
    with app.app_context():
        conn = dbmod.get_db(app.config["APP_CFG"])
        acc_id = conn.execute(
            "SELECT id FROM mt5_accounts").fetchone()["id"]
        rt = app.extensions["mt5_registry"].runtime_for(conn, acc_id)
        rt.lock.acquire()  # simulasikan sync akun ini sedang berjalan
    try:
        r = client.post("/api/v1/mt5/sync", json={}, headers=hdrs)
        assert r.status_code == 409
    finally:
        rt.lock.release()


def test_manual_sync_endpoint(client):
    hdrs = register_and_login(client)
    bind_account(client, hdrs)
    r = client.post("/api/v1/mt5/sync", json={"mode": "initial"},
                    headers=hdrs)
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_sync_error_reported_via_api(make_client):
    """MT5 gagal -> API tetap 200 dengan status error, tanpa crash."""
    c = make_client(fail_connect=True)
    hdrs = register_and_login(c)
    bind_account(c, hdrs, password="MT5_PASSWORD_X")
    r = c.post("/api/v1/mt5/sync", json={}, headers=hdrs)
    j = r.get_json()
    assert r.status_code == 200
    assert j["status"] == "error"
    assert "MT5_PASSWORD_X" not in (j.get("error") or "")


def test_error_never_leaks_password(cfg, make_client):
    """Kasus #8 + #10: pesan error & status bebas kredensial."""
    c = make_client(fail_connect=True)
    hdrs = register_and_login(c)
    bind_account(c, hdrs, password="MT5_PASSWORD_X")
    c.get("/api/v1/mt5/status", headers=hdrs)
    c.post("/api/v1/mt5/sync", json={}, headers=hdrs)
    ss = c.get("/api/v1/mt5/sync/status", headers=hdrs).get_json()
    blob = str(ss) + str(c.get("/api/v1/accounts",
                               headers=hdrs).get_json())
    assert "MT5_PASSWORD_X" not in blob
