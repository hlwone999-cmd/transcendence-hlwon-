"""API — validasi (kasus #9), auth, status disconnected, pagination,
detail, stats, equity curve, dan proteksi sync ganda."""
import copy

from mt5client.mock import MockMT5Client, scenario_dataset


H = {"X-API-Token": "TESTTOKEN"}


def test_health_open(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_auth_required_when_token_set(make_client):
    c = make_client()
    for path in ("/api/v1/mt5/status", "/api/v1/journal/stats",
                 "/api/v1/journal/trades"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"X-API-Token": "SALAH"}).status_code == 401
        assert c.get(path, headers=H).status_code in (200, 404)


def test_status_disabled_when_mt5_off(client):
    """Backend tanpa terminal MT5: status disconnected, TIDAK crash."""
    r = client.get("/api/v1/mt5/status", headers=H)
    assert r.status_code == 200
    j = r.get_json()
    assert j["connected"] is False
    assert j["reason"] == "disabled"
    assert j["enabled"] is False


def test_status_connected_via_mock(cfg, make_client):
    cfg.mt5_enabled = True
    c = make_client()
    r = c.get("/api/v1/mt5/status", headers=H)
    j = r.get_json()
    assert r.status_code == 200
    assert j["connected"] is True
    assert j["live_account"]["login"] == 12345678


def test_account_404_before_sync(client):
    r = client.get("/api/v1/mt5/account", headers=H)
    assert r.status_code == 404
    assert "tersinkron" in r.get_json()["error"]["message"]


def test_full_api_flow_after_sync(client, svc):
    assert svc.sync(mode="initial")["status"] == "ok"

    # account
    acc = client.get("/api/v1/mt5/account", headers=H).get_json()
    assert acc["login"] == 12345678
    assert acc["server"] == "Exness-MT5Real"

    # trades pagination
    r = client.get("/api/v1/journal/trades?page=1&page_size=2", headers=H)
    j = r.get_json()
    assert r.status_code == 200
    assert j["total"] == 3
    assert j["pages"] == 2
    assert len(j["items"]) == 2

    # filter status open
    j = client.get("/api/v1/journal/trades?status=open", headers=H).get_json()
    assert j["total"] == 1
    assert j["items"][0]["position_id"] == 105

    # filter symbol + direction
    j = client.get(
        "/api/v1/journal/trades?symbol=EURUSD&direction=buy", headers=H).get_json()
    assert j["total"] == 1

    # detail
    d = client.get("/api/v1/journal/trades/101", headers=H).get_json()
    assert d["trade"]["net_profit"] == 195.0
    assert len(d["deals"]) == 4
    assert len(d["orders"]) == 2
    assert d["trade"]["rr"] == 1.5
    assert client.get("/api/v1/journal/trades/999", headers=H).status_code == 404

    # stats & equity
    s = client.get("/api/v1/journal/stats", headers=H).get_json()
    assert s["total_closed"] == 2 and s["win_rate"] == 50.0
    e = client.get("/api/v1/journal/equity-curve", headers=H).get_json()
    assert len(e["points"]) == 4
    assert e["max_drawdown"] == 23.0

    # sync status
    ss = client.get("/api/v1/mt5/sync/status", headers=H).get_json()
    assert ss["state"]["last_sync_status"] == "ok"
    assert len(ss["recent_logs"]) == 1

    # symbols
    sy = client.get("/api/v1/symbols", headers=H).get_json()
    assert {x["symbol"] for x in sy["items"]} == {"XAUUSD", "EURUSD"}


def test_api_validation_errors(client, svc):
    svc.sync(mode="initial")
    base = "/api/v1/journal/trades"
    assert client.get(base + "?page=0", headers=H).status_code == 400
    assert client.get(base + "?page=abc", headers=H).status_code == 400
    assert client.get(base + "?page_size=500", headers=H).status_code == 400
    assert client.get(base + "?status=bukan", headers=H).status_code == 400
    assert client.get(base + "?direction=BELI", headers=H).status_code == 400
    assert client.get(base + "?sort=hahaha", headers=H).status_code == 400
    assert client.get(base + "?from=bukan-tanggal", headers=H).status_code == 400
    assert client.get("/api/v1/journal/stats?to=31/12/2026",
                      headers=H).status_code == 400
    body = {"mode": "hacked"}
    assert client.post("/api/v1/mt5/sync", json=body, headers=H).status_code == 400
    # valid dates tetap lolos
    assert client.get(base + "?from=2026-09-01&to=2026-10-31",
                      headers=H).status_code == 200


def test_sync_conflict_returns_409(client):
    """Sync ganda ditolak — bukan crash."""
    app_svc = client.application.extensions["mt5_sync"]
    app_svc._lock.acquire()  # simulasikan sync lain berjalan
    try:
        r = client.post("/api/v1/mt5/sync", json={}, headers=H)
        assert r.status_code == 409
    finally:
        app_svc._lock.release()


def test_manual_sync_endpoint(client):
    r = client.post("/api/v1/mt5/sync", json={"mode": "initial"}, headers=H)
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_sync_error_reported_via_api(make_client):
    """MT5 gagal -> API tetap 200 dengan status error, tanpa crash."""
    c = make_client(fail_connect=True)
    r = c.post("/api/v1/mt5/sync", json={}, headers=H)
    j = r.get_json()
    assert r.status_code == 200
    assert j["status"] == "error"
    assert "SUPER_SECRET_PASSWORD" not in (j.get("error") or "")


def test_error_never_leaks_password(cfg, make_client):
    """Kasus #8 + #10: pesan error bebas kredensial."""
    c = make_client(fail_connect=True)
    c.get("/api/v1/mt5/status", headers=H)
    c.post("/api/v1/mt5/sync", json={}, headers=H)
    ss = c.get("/api/v1/mt5/sync/status", headers=H).get_json()
    blob = str(ss)
    assert "SUPER_SECRET_PASSWORD" not in blob
