"""PostgreSQL — brief §11 #1 (migrasi dari DB kosong), E2E multi-customer
di PG nyata (pgserver, tanpa root), data migration SQLite->PG, dan RLS.

Catatan jujur: PG dijalankan via pgserver (binary resmi PostgreSQL 16)
dalam sandbox; bukan instalasi produksi — perilaku SQL/migration identik.
"""
import sqlite3
import uuid

import pytest

pgserver = pytest.importorskip("pgserver", reason="pgserver belum terpasang")
psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 belum terpasang")

import db as dbmod  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from tests.conftest import bind_account, register_and_login  # noqa: E402

WAJIB_TABLES = ["customers", "users", "mt5_accounts", "mt5_terminals",
                "mt5_sync_state", "mt5_deals", "mt5_trades", "audit_logs",
                "auth_tokens", "mt5_credentials", "mt5_orders", "mt5_symbols",
                "sync_logs"]


@pytest.fixture(scope="module")
def pg_server():
    import pgserver
    import tempfile
    d = tempfile.mkdtemp(prefix="pgmc-mod-")
    return pgserver.get_server(d)


@pytest.fixture()
def pg_cfg(pg_server, tmp_path):
    """Config dgn PostgreSQL nyata; database segar per test."""
    from config import Config
    name = f"mc_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(pg_server.get_uri())
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()
    uri = pg_server.get_uri().replace("/postgres?", f"/{name}?")

    cfg = Config()
    cfg.db_url = uri
    cfg.db_path = str(tmp_path / "unused.db")
    cfg.api_token = "TESTTOKEN"
    cfg.mt5_enabled = True
    cfg.cred_key = Fernet.generate_key().decode("ascii")
    cfg.term_base_dir = str(tmp_path / "terminals")
    cfg.terminal_provision_mode = "mock"
    cfg.max_customers = 4
    yield cfg
    dbmod.close_db()
    admin = psycopg2.connect(pg_server.get_uri())
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin.close()


def _pg_client(cfg, datasets):
    from app import create_app
    from mt5client.mock import MockMT5Client
    app = create_app(cfg, provider_factory=lambda acc_cfg=None:
                     MockMT5Client(
                         datasets[str(acc_cfg.mt5_login)] if acc_cfg
                         else None))
    app.config["TESTING"] = True
    return app.test_client()


# ------------------------------------------------- #1 migrasi DB kosong
def test_migrasi_pg_dari_db_kosong(pg_server):
    name = f"empty_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(pg_server.get_uri())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    uri = pg_server.get_uri().replace("/postgres?", f"/{name}?")
    conn = dbmod.connect_postgres(uri)
    applied = dbmod.migrate(conn)
    assert applied == [1]
    tables = {r["tablename"] for r in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    for t in WAJIB_TABLES:
        assert t in tables, f"tabel wajib {t} hilang"
    # idempoten: jalan ulang tidak error, tidak menambah versi
    assert dbmod.migrate(conn) == []
    conn.close()
    admin = psycopg2.connect(pg_server.get_uri())
    admin.autocommit = True
    admin.cursor().execute(f'DROP DATABASE "{name}"')
    admin.close()


def test_semua_tabel_punya_rls_policy(pg_cfg):
    import db as dbmod
    conn = dbmod.get_db(pg_cfg)
    dbmod.migrate(conn)
    pol = {r["tablename"] for r in conn.execute(
        "SELECT DISTINCT tablename FROM pg_policies WHERE"
        " schemaname='public'")}
    for t in ("mt5_deals", "mt5_orders", "mt5_trades", "mt5_symbols",
              "mt5_sync_state", "mt5_credentials"):
        assert t in pol, f"RLS policy {t} tidak ada"


# --------------------------------- E2E multi-customer di PostgreSQL nyata
def test_e2e_dua_customer_di_postgresql(pg_cfg):
    from tests.test_multi_customer import _ds_two_trades
    datasets = {
        "11111111": _ds_two_trades(11111111, "Exness-MT5Real", "XAUUSD",
                                   0.01, 2, 11, 100.0),
        "22222222": _ds_two_trades(22222222, "BrokerLain-MT5Demo", "EURUSD",
                                   0.00001, 5, 55, 200.0),
    }
    c = _pg_client(pg_cfg, datasets)
    hA = register_and_login(c, email="pga@test.id")
    hB = register_and_login(c, email="pgb@test.id")
    bind_account(c, hA, login=11111111, server="Exness-MT5Real")
    bind_account(c, hB, login=22222222, server="BrokerLain-MT5Demo")
    for h in (hA, hB):
        r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=h)
        assert r.status_code == 200 and r.get_json()["status"] == "ok", \
            r.get_json()
    ta = c.get("/api/v1/journal/trades", headers=hA).get_json()
    tb = c.get("/api/v1/journal/trades", headers=hB).get_json()
    assert ta["total"] == 1 and tb["total"] == 1
    assert ta["items"][0]["symbol"] == "XAUUSD"
    assert tb["items"][0]["symbol"] == "EURUSD"
    # stats di PG konsisten dengan SQLite test suite
    sa = c.get("/api/v1/journal/stats", headers=hA).get_json()
    assert sa["net_profit"] == 96.0


def test_sync_idempoten_di_postgresql(pg_cfg):
    """#10 di PG: 3x sync — jumlah baris tetap."""
    from tests.test_multi_customer import _ds_two_trades
    datasets = {"11111111": _ds_two_trades(
        11111111, "Exness-MT5Real", "XAUUSD", 0.01, 2, 11, 100.0)}
    c = _pg_client(pg_cfg, datasets)
    hA = register_and_login(c, email="idem@test.id")
    bind_account(c, hA, login=11111111, server="Exness-MT5Real")
    for _ in range(3):
        r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=hA)
        assert r.get_json()["status"] == "ok"
    with c.application.app_context():
        import db as dbmod
        conn = dbmod.get_db(c.application.config["APP_CFG"])
        n_deals = conn.execute("SELECT COUNT(*) AS n FROM mt5_deals"
                               ).fetchone()["n"]
        n_trades = conn.execute("SELECT COUNT(*) AS n FROM mt5_trades"
                                ).fetchone()["n"]
    assert (n_deals, n_trades) == (2, 1)


def test_bind_unbind_di_postgresql(pg_cfg):
    datasets = {"11111111": {"account": None, "deals": [], "orders": [],
                             "positions": [], "symbols": {}}}
    c = _pg_client(pg_cfg, datasets)
    h = register_and_login(c, email="bu@test.id")
    r = c.post("/api/v1/accounts/bind", headers=h,
               json={"login": 11111111, "server": "Exness-MT5Real",
                     "password": "pw12345678"})
    assert r.status_code == 201
    # kredensial tersimpan terenkripsi — bukan plaintext
    with c.application.app_context():
        import db as dbmod
        conn = dbmod.get_db(c.application.config["APP_CFG"])
        row = conn.execute("SELECT enc_password FROM mt5_credentials"
                           ).fetchone()
        assert row is not None
        assert "pw12345678" not in row["enc_password"]
        assert row["enc_password"].startswith("gAAAA")  # token Fernet
    r = c.post("/api/v1/accounts/unbind", headers=h)
    assert r.status_code == 200
    with c.application.app_context():
        import db as dbmod
        conn = dbmod.get_db(c.application.config["APP_CFG"])
        n = conn.execute("SELECT COUNT(*) AS n FROM mt5_accounts"
                         ).fetchone()["n"]
        ncred = conn.execute("SELECT COUNT(*) AS n FROM mt5_credentials"
                             ).fetchone()["n"]
    assert (n, ncred) == (0, 0)   # CASCADE


# ---------------------- data migration SQLite (lama) -> PostgreSQL
def test_migrasi_data_sqlite_ke_postgresql(pg_cfg, tmp_path):
    """Jalur migrasi data: DB SQLite lama (skema 001) dipindah ke PG."""
    from tests.test_multi_customer import _ds_two_trades
    # 1) bangun SQLite "lama" (hanya skema 001) berisi 1 akun + data
    sq_path = tmp_path / "legacy.db"
    old = sqlite3.connect(str(sq_path))
    old.executescript((dbmod.MIGRATIONS_DIR / "sqlite" / "001_init.sql")
                      .read_text(encoding="utf-8"))
    ds = _ds_two_trades(12345678, "Exness-MT5Real", "XAUUSD", 0.01, 2, 101,
                        195.0)
    old.execute("INSERT INTO mt5_accounts (login, server, currency) VALUES"
                " (?,?,?)", (12345678, "Exness-MT5Real", "USD"))
    acc_id = old.execute("SELECT id FROM mt5_accounts").fetchone()[0]
    for d in ds["deals"]:
        import datetime as _dt
        tiso = _dt.datetime.fromtimestamp(d.time, _dt.timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        old.execute(
            "INSERT INTO mt5_deals (account_id, ticket, order_ticket,"
            " position_id, symbol, deal_type, type_name, entry, entry_name,"
            " volume, price, profit, commission, swap, fee, comment, magic,"
            " reason, time_utc, synced_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (acc_id, d.ticket, d.order, d.position_id, d.symbol, d.type,
             "buy" if d.type == 0 else "sell", d.entry,
             "in" if d.entry == 0 else "out", d.volume, d.price, d.profit,
             d.commission, d.swap, d.fee, d.comment, d.magic, d.reason,
             tiso, tiso))
    import datetime as _dtmod
    tiso0 = _dt.datetime.fromtimestamp(ds["deals"][0].time,
                                       _dt.timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    tiso1 = _dt.datetime.fromtimestamp(ds["deals"][1].time,
                                       _dt.timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    old.execute(
        "INSERT INTO trades (account_id, position_id, symbol, direction,"
        " status, volume_in, volume_out, entry_price, exit_price,"
        " open_time_utc, close_time_utc, profit, commission, swap, fee,"
        " net_profit, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (acc_id, 101, "XAUUSD", "buy", "closed", 0.1, 0.1, 2000.0, 2010.0,
         tiso0, tiso1, 100.0, -2.0, 0.0, 0.0, 96.0, tiso1))
    old.commit()
    old.close()

    # 2) jalankan tool migrasi data
    import subprocess
    import config as configmod
    r = subprocess.run(
        ["python3", str(configmod.BASE_DIR / "tools" /
                        "migrate_sqlite_to_postgres.py"),
         "--sqlite", str(sq_path), "--pg", pg_cfg.db_url, "--apply"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK" in r.stdout

    # 3) verifikasi di PG: baris utuh, id terjaga, milik customer legacy
    conn = dbmod.connect_postgres(pg_cfg.db_url)
    n_acc = conn.execute("SELECT COUNT(*) AS n FROM mt5_accounts"
                         ).fetchone()["n"]
    n_deals = conn.execute("SELECT COUNT(*) AS n FROM mt5_deals"
                           ).fetchone()["n"]
    n_trades = conn.execute("SELECT COUNT(*) AS n FROM mt5_trades"
                            ).fetchone()["n"]
    acc = conn.execute("SELECT id, customer_id, login, server FROM"
                       " mt5_accounts").fetchone()
    conn.close()
    assert (n_acc, n_deals, n_trades) == (1, 2, 1)
    assert acc["id"] == acc_id              # id lama dipertahankan
    assert acc["customer_id"] is not None   # terhubung customer legacy
