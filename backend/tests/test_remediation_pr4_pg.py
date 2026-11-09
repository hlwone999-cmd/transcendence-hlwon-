"""PostgreSQL SUNGGUHAN dengan role aplikasi NON-OWNER `transcendence_app`
(Tahap 6) — basis PR #4.

Owner hanya dipakai untuk migrasi & untuk MEMERIKSA hasil (bypass RLS);
semua request backend berjalan sebagai transcendence_app (tunduk RLS,
tanpa CREATE pada skema public). RLS tidak dilemahkan.
"""
import tempfile
import threading
import uuid

import pytest

pgserver = pytest.importorskip("pgserver", reason="pgserver belum terpasang")
psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 belum terpasang")

from cryptography.fernet import Fernet  # noqa: E402

import db as dbmod  # noqa: E402
from mt5client.mock import scenario_dataset  # noqa: E402
from tests.test_remediation_pr4 import (_BrokerMock, run_workers,  # noqa: E402
                                        )

APP_ROLE = "transcendence_app"
SRV = "Exness-MT5Real"


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pg-rem4"))
    yield server
    server.cleanup()


@pytest.fixture()
def dbs(pg):
    """DB segar: (owner_uri, app_uri) — migrasi dijalankan owner."""
    name = f"rem4_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(pg.get_uri())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    owner_uri = pg.get_uri().replace("/postgres?", f"/{name}?")
    app_uri = owner_uri.replace("postgres:@", f"{APP_ROLE}:@")
    owner = dbmod.connect_postgres(owner_uri)
    applied = dbmod.migrate(owner)
    assert applied[-1] >= 3 and applied[:3] == [1, 2, 3]
    owner.close()
    yield owner_uri, app_uri
    dbmod.close_db()
    admin = psycopg2.connect(pg.get_uri())
    admin.autocommit = True
    admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


def _cfg(db_url, **kw):
    from config import Config
    c = Config()
    tmp = tempfile.mkdtemp(prefix="rem4pg-")
    c.db_url = db_url
    c.db_path = f"{tmp}/unused.db"
    c.api_token = "TESTTOKEN"
    c.mt5_enabled = True
    c.cred_key = Fernet.generate_key().decode("ascii")
    c.term_base_dir = f"{tmp}/terminals"
    c.terminal_provision_mode = "mock"
    c.max_customers = 4
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _app(cfg, broker=None):
    from app import create_app
    broker = broker if broker is not None else {"reject": False}

    def factory(acc_cfg=None):
        if acc_cfg is None:
            return _BrokerMock(None, broker)
        ds = scenario_dataset()
        ds["account"] = ds["account"]._replace(
            login=int(acc_cfg.mt5_login), server=acc_cfg.mt5_server,
            trade_allowed=False)
        return _BrokerMock(ds, broker)
    app = create_app(cfg, provider_factory=factory)
    app.config["TESTING"] = True
    return app


def _owner(owner_uri, sql, params=()):
    conn = psycopg2.connect(owner_uri)
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _reg(c, email, ip="198.51.100.10"):
    return c.post("/api/v1/auth/register",
                  json={"email": email, "password": "rahasia123"},
                  headers={"X-Real-IP": ip})


def _login(c, email, ip="198.51.100.10"):
    r = c.post("/api/v1/auth/login",
               json={"email": email, "password": "rahasia123"},
               headers={"X-Real-IP": ip})
    assert r.status_code == 200, r.get_json()
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def _bind(c, h, login, password="pw-investor-1"):
    return c.post("/api/v1/accounts/bind", headers=h,
                  json={"login": login, "server": SRV, "password": password})


# ------------------------------------------------- startup & migrasi
def test_role_app_benar_benar_non_owner_tanpa_create(dbs):
    _, app_uri = dbs
    conn = psycopg2.connect(app_uri)
    cur = conn.cursor()
    cur.execute("SELECT has_schema_privilege('public', 'CREATE'),"
                " rolbypassrls, rolsuper FROM pg_roles"
                " WHERE rolname=current_user")
    can_create, bypass, sup = cur.fetchone()
    conn.close()
    assert (can_create, bypass, sup) == (False, False, False)


def test_startup_migrasi_lengkap_tanpa_ddl(dbs):
    _, app_uri = dbs
    conn = dbmod.connect_postgres(app_uri)
    assert dbmod.migrate(conn) == []      # tanpa DDL apa pun
    conn.close()
    r = _app(_cfg(app_uri)).test_client().get("/api/v1/health")
    assert r.status_code == 200, r.get_json()


def test_migrasi_tertunda_dengan_role_app_gagal_jelas(dbs):
    owner_uri, app_uri = dbs
    owner = dbmod.connect_postgres(owner_uri)
    owner.execute("DELETE FROM schema_migrations WHERE version=3")
    owner.execute("DELETE FROM schema_migrations WHERE version>3")
    owner.commit()
    owner.close()
    conn = dbmod.connect_postgres(app_uri)
    with pytest.raises(dbmod.MigrationPrivilegeError) as ei:
        dbmod.migrate(conn)
    # semua migrasi tertunda tercantum TEPAT (PR #4: "[3]")
    assert "DSN OWNER" in str(ei.value) and "[3, 4, 5]" in str(ei.value)
    conn.close()


# ---------------------------------------------- Tahap 1: registrasi
def test_registrasi_normal_duplikat_dan_spoof_xff(dbs):
    owner_uri, app_uri = dbs
    c = _app(_cfg(app_uri)).test_client()
    assert _reg(c, "pg1@t.id").status_code == 201
    assert _reg(c, "PG1@t.id", ip="198.51.100.11").status_code == 409
    assert _owner(owner_uri, "SELECT COUNT(*) FROM users") == 1
    codes = [c.post("/api/v1/auth/register",
                    json={"email": f"x{i}@t.id", "password": "rahasia123"},
                    headers={"X-Forwarded-For": f"203.0.{i}.1"}).status_code
             for i in range(7)]
    assert codes == [201] * 5 + [429, 429], codes
    assert _owner(owner_uri, "SELECT COUNT(*) FROM customers") == 6
    assert _owner(owner_uri, "SELECT COUNT(*) FROM mt5_terminals") == 0


def test_registrasi_bersamaan_postgres_kode_unik(dbs):
    owner_uri, app_uri = dbs
    app = _app(_cfg(app_uri))
    res = []
    barrier = threading.Barrier(8)

    def w(i):
        cl = app.test_client()
        barrier.wait()
        res.append(_reg(cl, f"c{i}@t.id", ip=f"10.{i}.1.1").status_code)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert res == [201] * 8, res
    assert _owner(owner_uri,
                  "SELECT COUNT(DISTINCT code) FROM customers") == 8


# ---------------------------------------- Tahap 2: sesi & riwayat (PG)
def test_unbind_mempertahankan_riwayat_mencabut_sesi(dbs):
    owner_uri, app_uri = dbs
    c = _app(_cfg(app_uri)).test_client()
    assert _reg(c, "ub@t.id").status_code == 201
    h1, h2 = _login(c, "ub@t.id"), _login(c, "ub@t.id")
    assert _bind(c, h1, 81818181).status_code == 201
    assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                  headers=h1).get_json()["status"] == "ok"
    deals = _owner(owner_uri, "SELECT COUNT(*) FROM mt5_deals")
    assert deals > 0
    assert c.post("/api/v1/accounts/unbind", headers=h2).status_code == 200
    for h in (h1, h2):
        assert c.get("/api/v1/auth/me", headers=h).status_code == 401
    assert _owner(owner_uri, "SELECT COUNT(*) FROM mt5_deals") == deals
    assert _owner(owner_uri, "SELECT COUNT(*) FROM mt5_credentials") == 0
    assert _owner(owner_uri, "SELECT state FROM mt5_accounts") == "unbound"
    h = _login(c, "ub@t.id")
    assert _bind(c, h, 81818181, "pw-baru-2").status_code == 200
    assert _owner(owner_uri, "SELECT state FROM mt5_accounts") == "active"
    assert _owner(owner_uri, "SELECT COUNT(*) FROM mt5_deals") == deals


def test_broker_menolak_saat_sync_postgres(dbs):
    owner_uri, app_uri = dbs
    broker = {"reject": False}
    app = _app(_cfg(app_uri), broker)
    c = app.test_client()
    assert _reg(c, "bk@t.id").status_code == 201
    h1, h2 = _login(c, "bk@t.id"), _login(c, "bk@t.id")
    assert _bind(c, h1, 82828282).status_code == 201
    broker["reject"] = True
    app.extensions["mt5_registry"].evict(
        _owner(owner_uri, "SELECT id FROM mt5_accounts"))
    assert c.post("/api/v1/mt5/sync", json={},
                  headers=h1).get_json()["status"] == "error"
    for h in (h1, h2):
        assert c.get("/api/v1/auth/me", headers=h).status_code == 401
    assert _owner(owner_uri, "SELECT state FROM mt5_accounts") \
        == "credentials_rejected"
    assert _owner(owner_uri, "SELECT COUNT(*) FROM mt5_credentials") == 0


def test_admin_revoke_postgres(dbs):
    owner_uri, app_uri = dbs
    c = _app(_cfg(app_uri)).test_client()
    assert _reg(c, "ad@t.id").status_code == 201
    hs = [_login(c, "ad@t.id") for _ in range(3)]
    cid = _owner(owner_uri, "SELECT customer_id FROM users")
    r = c.post(f"/api/v1/admin/customers/{cid}/revoke-sessions",
               headers={"X-API-Token": "TESTTOKEN"})
    assert r.get_json()["revoked"] == 3
    for h in hs:
        assert c.get("/api/v1/auth/me", headers=h).status_code == 401


# --------------------------------- Tahap 5: rate limit lintas worker
def test_lockout_bertahan_setelah_restart_postgres(dbs):
    _, app_uri = dbs
    cfg = _cfg(app_uri)
    c = _app(cfg).test_client()
    assert _reg(c, "lk@t.id").status_code == 201
    for i in range(5):
        c.post("/api/v1/auth/login",
               json={"email": "lk@t.id", "password": "salah-salah"},
               headers={"X-Real-IP": f"10.7.{i}.1"})
    dbmod.close_db()
    c2 = _app(_cfg(app_uri)).test_client()
    r = c2.post("/api/v1/auth/login",
                json={"email": "lk@t.id", "password": "rahasia123"},
                headers={"X-Real-IP": "10.7.9.1"})
    assert r.status_code == 429


def test_rate_limit_lintas_proses_worker_postgres(dbs, tmp_path):
    _, app_uri = dbs
    codes = run_workers(str(tmp_path / "unused.db"), app_uri, 4, 5)
    assert len(codes) == 20
    assert codes.count(401) == 10 and codes.count(429) == 10, codes


# ---------------------------- Tahap 6: isolasi CRUD dua customer (RLS)
def test_isolasi_crud_dua_customer_role_app(dbs):
    owner_uri, app_uri = dbs
    c = _app(_cfg(app_uri)).test_client()
    ids = {}
    for email, login in (("ia@t.id", 91919191), ("ib@t.id", 92929292)):
        assert _reg(c, email).status_code == 201
        h = _login(c, email)
        assert _bind(c, h, login).status_code == 201
        assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                      headers=h).get_json()["status"] == "ok"
        ids[email] = (_owner(owner_uri, "SELECT customer_id FROM users"
                             " WHERE email=%s", (email,)),
                      _owner(owner_uri, "SELECT a.id FROM mt5_accounts a"
                             " JOIN users u ON u.customer_id=a.customer_id"
                             " WHERE u.email=%s", (email,)))
    (cid_a, acc_a), (_, acc_b) = ids["ia@t.id"], ids["ib@t.id"]
    app = dbmod.connect_postgres(app_uri)
    # tanpa context: default-deny
    assert app.execute("SELECT COUNT(*) AS n FROM mt5_deals"
                       ).fetchone()["n"] == 0
    app.set_customer_context(cid_a)
    # SELECT: hanya milik A
    accs = {r["account_id"] for r in app.execute(
        "SELECT DISTINCT account_id FROM mt5_deals")}
    assert accs == {acc_a}
    # UPDATE / DELETE lintas customer: 0 baris
    assert app.execute("UPDATE mt5_deals SET profit=999 WHERE account_id=?",
                       (acc_b,)).rowcount == 0
    assert app.execute("DELETE FROM mt5_deals WHERE account_id=?",
                       (acc_b,)).rowcount == 0
    # INSERT ke akun B ditolak policy RLS
    with pytest.raises(psycopg2.Error):
        app.execute("INSERT INTO mt5_credentials (account_id, enc_password,"
                    " key_version) VALUES (?, 'x', 1)", (acc_b,))
    app.rollback()
    # UPDATE milik sendiri tetap boleh
    assert app.execute("UPDATE mt5_deals SET profit=profit WHERE"
                       " account_id=?", (acc_a,)).rowcount > 0
    app.commit()
    app.close()
    assert _owner(owner_uri, "SELECT COUNT(*) FROM mt5_deals WHERE"
                  " account_id=%s AND profit=999", (acc_b,)) == 0
