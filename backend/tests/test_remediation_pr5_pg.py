"""PR #5 di PostgreSQL SUNGGUHAN dgn role NON-OWNER `transcendence_app`
(K1 + Tahap 3/6): /mt5/connect, job provisioning, disconnect, unbind,
cleanup, broker menolak, restart, dan isolasi dua customer.

Owner hanya untuk migrasi & memeriksa hasil (bypass RLS). RLS tidak
dilemahkan; tidak ada superuser di jalur aplikasi.
"""
import os
import tempfile
import threading
import time
import uuid

import pytest

pgserver = pytest.importorskip("pgserver", reason="pgserver belum terpasang")
psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 belum terpasang")

from cryptography.fernet import Fernet  # noqa: E402

import db as dbmod  # noqa: E402
from tests.conftest import investor_dataset  # noqa: E402
from tests.test_remediation_pr5 import _DynBroker  # noqa: E402

APP_ROLE = "transcendence_app"
SRV = "Exness-MT5Real"


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pg-rem5"))
    yield server
    server.cleanup()


@pytest.fixture()
def env(pg):
    name = f"rem5_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(pg.get_uri())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    owner_uri = pg.get_uri().replace("/postgres?", f"/{name}?")
    app_uri = owner_uri.replace("postgres:@", f"{APP_ROLE}:@")
    owner = dbmod.connect_postgres(owner_uri)
    assert dbmod.migrate(owner) == [1, 2, 3, 4, 5, 6, 7, 8]
    owner.close()
    yield owner_uri, app_uri
    dbmod.close_db()
    admin = psycopg2.connect(pg.get_uri())
    admin.autocommit = True
    admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


def _cfg(app_uri, **kw):
    from config import Config
    c = Config()
    tmp = tempfile.mkdtemp(prefix="rem5pg-")
    c.db_url = app_uri
    c.db_path = f"{tmp}/unused.db"
    c.api_token = "TESTTOKEN"
    c.mt5_enabled = True
    c.cred_key = Fernet.generate_key().decode("ascii")
    c.term_base_dir = f"{tmp}/terminals"
    c.terminal_provision_mode = "mock"
    c.max_customers = 4
    c.mt5_verify_timeout_seconds = 2.0
    c.mt5_connect_sync_wait_seconds = 8.0
    c.mt5_provision_timeout_seconds = 15.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _app(cfg, broker=None, delay=0.1):
    from app import create_app
    broker = broker if broker is not None else {"pw": "good"}

    def factory(acc_cfg=None):
        if acc_cfg is None:
            return _DynBroker(None, broker, "")
        return _DynBroker(investor_dataset(acc_cfg.mt5_login,
                                           acc_cfg.mt5_server),
                          broker, acc_cfg.mt5_password, connect_delay=delay)
    app = create_app(cfg, provider_factory=factory)
    app.config["TESTING"] = True
    return app


def _post(app, login, password="good", ip="203.0.113.1"):
    with app.test_client() as c:
        return c.post("/api/v1/mt5/connect",
                      json={"login": login, "server": SRV,
                            "password": password},
                      headers={"X-Real-IP": ip})


def _own(owner_uri, sql, params=()):
    conn = psycopg2.connect(owner_uri)
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_k1_connect_baru_reconnect_sync_disconnect(env):
    owner_uri, app_uri = env
    app = _app(_cfg(app_uri))
    r = _post(app, 11111111)
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["connection_state"] == "connected"
    assert _own(owner_uri, "SELECT COUNT(*) FROM mt5_credentials") == 1
    assert _own(owner_uri, "SELECT lifecycle FROM mt5_terminals") \
        == "connected"
    r2 = _post(app, 11111111, ip="203.0.113.2")
    assert r2.status_code == 200 and r2.get_json()["mode"] == "login"
    h = _h(r2.get_json()["token"])
    with app.test_client() as c:
        assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                      headers=h).get_json()["status"] == "ok"
        assert c.post("/api/v1/mt5/disconnect", headers=h).status_code == 200
        assert c.get("/api/v1/auth/me", headers=h).status_code == 401
    assert _own(owner_uri, "SELECT COUNT(*) FROM mt5_deals") > 0


def test_k1_kegagalan_dibersihkan_penuh(env):
    owner_uri, app_uri = env
    cfg = _cfg(app_uri)
    r = _post(_app(cfg), 22222222, password="salah")
    assert r.status_code == 401, r.get_json()
    for t in ("customers", "mt5_accounts", "mt5_credentials",
              "mt5_terminals", "auth_tokens"):
        assert _own(owner_uri, f"SELECT COUNT(*) FROM {t}") == 0, t
    assert not os.path.isdir(cfg.term_base_dir) or \
        os.listdir(cfg.term_base_dir) == []
    assert _own(owner_uri, "SELECT error_class FROM mt5_connect_jobs") \
        == "auth_failed"


def test_unbind_riwayat_tetap_terminal_dilepas_sesi_dicabut(env):
    owner_uri, app_uri = env
    app = _app(_cfg(app_uri))
    tok = _post(app, 33333333).get_json()["token"]
    tok2 = _post(app, 33333333, ip="203.0.113.3").get_json()["token"]
    with app.test_client() as c:
        assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                      headers=_h(tok)).get_json()["status"] == "ok"
        deals = _own(owner_uri, "SELECT COUNT(*) FROM mt5_deals")
        r = c.post("/api/v1/accounts/unbind", headers=_h(tok))
        assert r.get_json()["terminal_released"] is True
        for t in (tok, tok2):
            assert c.get("/api/v1/auth/me", headers=_h(t)).status_code == 401
    assert _own(owner_uri, "SELECT COUNT(*) FROM mt5_deals") == deals
    assert _own(owner_uri, "SELECT state FROM mt5_accounts") == "unbound"
    assert _own(owner_uri, "SELECT COUNT(*) FROM mt5_terminals") == 0
    assert _own(owner_uri, "SELECT COUNT(*) FROM mt5_credentials") == 0
    r = _post(app, 33333333, ip="203.0.113.4")
    assert r.status_code == 200 and r.get_json()["mode"] == "reactivate"
    assert _own(owner_uri, "SELECT COUNT(*) FROM mt5_deals") == deals


def test_broker_menolak_lalu_password_baru_postgres(env):
    owner_uri, app_uri = env
    broker = {"pw": "good"}
    app = _app(_cfg(app_uri), broker)
    tok = _post(app, 44444444).get_json()["token"]
    broker["pw"] = "baru-2"
    r = _post(app, 44444444, ip="203.0.113.5")
    assert r.status_code == 401
    with app.test_client() as c:
        assert c.get("/api/v1/auth/me", headers=_h(tok)).status_code == 401
    assert _own(owner_uri, "SELECT state FROM mt5_accounts") \
        == "credentials_rejected"
    r = _post(app, 44444444, password="baru-2", ip="203.0.113.6")
    assert r.status_code == 200, r.get_json()
    assert _own(owner_uri, "SELECT state FROM mt5_accounts") == "active"


def test_restart_pemulihan_job_postgres(env, monkeypatch):
    owner_uri, app_uri = env
    cfg = _cfg(app_uri, mt5_connect_sync_wait_seconds=0.0)
    app = _app(cfg)
    monkeypatch.setattr(app.extensions["mt5_coordinator"], "start",
                        lambda job_id: None)
    assert _post(app, 55555555).status_code == 202
    conn = psycopg2.connect(owner_uri)
    conn.cursor().execute("UPDATE mt5_connect_jobs SET deadline_at=%s",
                          (time.time() - 1,))
    conn.commit()
    conn.close()
    dbmod.close_db()
    _app(_cfg(app_uri))                       # restart -> pemulihan
    for t in ("customers", "mt5_accounts", "mt5_terminals",
              "mt5_credentials"):
        assert _own(owner_uri, f"SELECT COUNT(*) FROM {t}") == 0, t
    assert _own(owner_uri, "SELECT error_class FROM mt5_connect_jobs") \
        == "interrupted"


def test_concurrent_connect_postgres_slot_dan_kegagalan(env):
    owner_uri, app_uri = env
    app = _app(_cfg(app_uri, max_customers=2), delay=0.2)
    res = {}

    def go(key, login, pw, ip):
        res[key] = _post(app, login, password=pw, ip=ip).status_code

    ts = [threading.Thread(target=go, args=(f"k{i}", 66666660 + i,
                                            "salah" if i == 0 else "good",
                                            f"198.51.100.{i}"))
          for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    vals = sorted(res.values())
    # INVARIANT keamanan (tidak bergantung urutan/timing):
    # - password salah TIDAK PERNAH 201; tidak ada 5xx/kode lain;
    # - jumlah sukses <= slot (2) dan >= 1;
    # - terminal tersisa == jumlah sukses (tanpa resource yatim).
    # Catatan: percobaan gagal menahan slot SELAMA verifikasi, sehingga
    # user sah bisa mendapat 403 sementara (residual terdokumentasi).
    assert res["k0"] in (401, 403), res
    assert set(vals) <= {201, 401, 403}, res
    ok = vals.count(201)
    assert 1 <= ok <= 2, res
    assert _own(owner_uri, "SELECT COUNT(*) FROM mt5_terminals") == ok
    assert _own(owner_uri, "SELECT COUNT(*) FROM mt5_credentials") == ok
    assert _own(owner_uri, "SELECT COUNT(DISTINCT code) FROM customers") \
        == _own(owner_uri, "SELECT COUNT(*) FROM customers") == ok


def test_isolasi_dua_customer_lewat_api(env):
    owner_uri, app_uri = env
    app = _app(_cfg(app_uri))
    ta = _post(app, 77777771).get_json()["token"]
    tb = _post(app, 77777772, ip="203.0.113.7").get_json()["token"]
    with app.test_client() as c:
        for t in (ta, tb):
            assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                          headers=_h(t)).get_json()["status"] == "ok"
        a = c.get("/api/v1/mt5/account", headers=_h(ta)).get_json()
        b = c.get("/api/v1/mt5/account", headers=_h(tb)).get_json()
        assert a["login"] == 77777771 and b["login"] == 77777772
        tra = c.get("/api/v1/journal/trades", headers=_h(ta)).get_json()
        assert all(int(t.get("account_id", a["id"])) == a["id"]
                   for t in tra.get("items", []))
