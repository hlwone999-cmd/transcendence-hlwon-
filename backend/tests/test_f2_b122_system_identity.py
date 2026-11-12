"""B12.2 — validasi fail-closed identitas koneksi role sistem PostgreSQL.

H1 identitas koneksi (current_user/session_user, atribut, keanggotaan,
   kepemilikan) untuk get_system_db DAN get_audit_db;
H2 DATABASE_SYSTEM_URL wajib (tanpa fallback DSN aplikasi/owner);
H3 migrasi 008 dengan role transcendence_system yang sudah ada;
H4 hak minimum + default privileges (tabel baru tanpa RLS tidak terbuka).

PostgreSQL lokal (pgserver). Atribut role bersifat cluster-wide: setiap
tes yang mengubahnya MEMULIHKAN keadaan aman di finally. Bukan staging.
"""
import logging
import uuid

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402

import audit as auditmod  # noqa: E402
import db as dbmod  # noqa: E402
from tests import test_f2_b2_races as B2  # noqa: E402
from tests import test_f2_b11_customer_mapping as B11  # noqa: E402
from tests import test_f2_b12_rls_tenant as T12  # noqa: E402
from tests import test_remediation_pr5_pg as PG  # noqa: E402

env = PG.env
pg = PG.pg
_tanpa_chown = B2._tanpa_chown
SYS = "transcendence_system"
SAFE = (f"ALTER ROLE {SYS} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE"
        " NOCREATEDB NOREPLICATION NOINHERIT")


def _cfg(db_url, sys_url=""):
    from config import Config
    c = Config()
    c.db_url = db_url
    c.db_system_url = sys_url
    return c


def _owner_exec(owner_uri, sql):
    c = psycopg2.connect(owner_uri)
    c.autocommit = True
    try:
        c.cursor().execute(sql)
    finally:
        c.close()


def _as_user(owner_uri, user):
    return owner_uri.replace("postgres:@", f"{user}:@", 1)


def _slots_empty():
    return all(getattr(dbmod._local, s, None) is None
               for s in ("sysconn", "auditconn"))


@pytest.fixture()
def restore_role(pg):
    """Pulihkan role sistem ke keadaan aman + cabut keanggotaan uji."""
    yield
    root = pg.get_uri()
    for sql in (SAFE,
                f"REVOKE transcendence_app FROM {SYS}",
                f"REVOKE {SYS} FROM transcendence_app"):
        try:
            _owner_exec(root, sql)
        except psycopg2.Error:
            pass
    dbmod.close_db()


def _ditolak(cfg, *frag):
    for opener in (dbmod.get_system_db, dbmod.get_audit_db):
        with pytest.raises(dbmod.SystemDbConfigError) as ei:
            opener(cfg)
        for f in frag:
            assert f in str(ei.value), (opener.__name__, str(ei.value))
        assert _slots_empty(), opener.__name__
    return str(ei.value)


# ================================================================ H1/H2
def test_b122_1_dsn_sistem_benar_diterima(env):
    _, app_uri = env
    for cfg in (_cfg(app_uri, T12._sys_uri(app_uri)),   # eksplisit
                _cfg(app_uri)):                          # turunan lokal
        for opener in (dbmod.get_system_db, dbmod.get_audit_db):
            conn = opener(cfg)
            assert dbmod.verify_system_identity(conn) == []
            r = conn.execute("SELECT current_user AS u, session_user AS s")\
                .fetchone()
            conn.rollback()
            assert (r["u"], r["s"]) == (SYS, SYS)
        dbmod.close_db()


def test_b122_2_dsn_role_aplikasi_ditolak(env):
    _, app_uri = env
    dsn = app_uri + "&application_name=uji_b122"     # beda string, sama role
    msg = _ditolak(_cfg(app_uri, dsn), "current_user=transcendence_app")
    assert "DITOLAK" in msg
    _ditolak(_cfg(app_uri, app_uri), "sama dengan DATABASE_URL")


def test_b122_3_dsn_superuser_ditolak(env):
    owner_uri, app_uri = env
    _ditolak(_cfg(app_uri, owner_uri), "current_user=postgres", "SUPERUSER")


def test_b122_4_dsn_owner_tabel_ditolak(env, pg):
    owner_uri, app_uri = env
    role = "uji_owner_b122"
    _owner_exec(pg.get_uri(), f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM"
                f" pg_roles WHERE rolname='{role}') THEN CREATE ROLE {role}"
                " LOGIN NOSUPERUSER NOBYPASSRLS; END IF; END$$")
    _owner_exec(owner_uri, "CREATE TABLE uji_b122_milik (x int);"
                f" ALTER TABLE uji_b122_milik OWNER TO {role}")
    try:
        _ditolak(_cfg(app_uri, _as_user(owner_uri, role)),
                 f"current_user={role}", "memiliki 1 objek")
    finally:
        _owner_exec(owner_uri, "DROP TABLE uji_b122_milik")


def test_b122_4b_role_sistem_pemilik_objek_ditolak(env, restore_role):
    owner_uri, app_uri = env
    _owner_exec(owner_uri, "CREATE TABLE uji_b122_sys (x int);"
                f" ALTER TABLE uji_b122_sys OWNER TO {SYS}")
    try:
        _ditolak(_cfg(app_uri, T12._sys_uri(app_uri)), "memiliki 1 objek")
    finally:
        _owner_exec(owner_uri, "DROP TABLE uji_b122_sys")


def test_b122_5_dsn_bypassrls_ditolak(env, pg, restore_role):
    owner_uri, app_uri = env
    role = "uji_bypass_b122"
    _owner_exec(pg.get_uri(), f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM"
                f" pg_roles WHERE rolname='{role}') THEN CREATE ROLE {role}"
                " LOGIN BYPASSRLS; END IF; END$$")
    _ditolak(_cfg(app_uri, _as_user(owner_uri, role)), "BYPASSRLS")
    # role BERNAMA benar tetapi diberi BYPASSRLS / atribut lain -> ditolak
    for attr, frag in (("BYPASSRLS", "BYPASSRLS"),
                       ("CREATEROLE", "CREATEROLE"),
                       ("SUPERUSER", "SUPERUSER"),
                       ("INHERIT", "INHERIT"),
                       ("CREATEDB", "CREATEDB")):
        _owner_exec(pg.get_uri(), f"ALTER ROLE {SYS} {attr}")
        try:
            _ditolak(_cfg(app_uri, T12._sys_uri(app_uri)), frag)
        finally:
            _owner_exec(pg.get_uri(), SAFE)


def test_b122_5b_keanggotaan_silang_ditolak(env, pg, restore_role):
    _, app_uri = env
    sysdsn = T12._sys_uri(app_uri)
    _owner_exec(pg.get_uri(), f"GRANT transcendence_app TO {SYS}")
    _ditolak(_cfg(app_uri, sysdsn), "anggota dari ['transcendence_app']")
    _owner_exec(pg.get_uri(), f"REVOKE transcendence_app FROM {SYS}")
    _owner_exec(pg.get_uri(), f"GRANT {SYS} TO transcendence_app")
    _ditolak(_cfg(app_uri, sysdsn), "memiliki anggota")


def test_b122_5c_set_role_lewat_dsn_terdeteksi(env):
    """Login sebagai owner + opsi DSN role=transcendence_system ->
    current_user benar tetapi session_user berbeda -> DITOLAK."""
    owner_uri, app_uri = env
    dsn = owner_uri + "&options=-c%20role%3Dtranscendence_system"
    c = psycopg2.connect(dsn)
    cur = c.cursor()
    cur.execute("SELECT current_user, session_user")
    assert cur.fetchone() == (SYS, "postgres")         # prasyarat uji
    c.close()
    _ditolak(_cfg(app_uri, dsn), "session_user=postgres")


def test_b122_6_dsn_berpassword_tanpa_system_url_ditolak():
    for url in ("postgresql://transcendence_app:rahasia@127.0.0.1:1/x",
                "postgresql://transcendence:rahasia@127.0.0.1:1/x"):
        with pytest.raises(dbmod.SystemDbConfigError) as ei:
            dbmod.system_dsn(_cfg(url))
        assert "DATABASE_SYSTEM_URL wajib" in str(ei.value)
        _ditolak(_cfg(url), "DATABASE_SYSTEM_URL wajib")


def test_b122_6b_dsn_owner_tanpa_password_tidak_jadi_fallback(env):
    owner_uri, _ = env
    with pytest.raises(dbmod.SystemDbConfigError) as ei:
        dbmod.system_dsn(_cfg(owner_uri))
    assert "turunan otomatis hanya" in str(ei.value)
    _ditolak(_cfg(owner_uri), "DATABASE_SYSTEM_URL wajib")


# ======================================================== H3 migrasi 008
def _fresh_db(pg):
    name = f"b122_{uuid.uuid4().hex[:10]}"
    _owner_exec(pg.get_uri(), f'CREATE DATABASE "{name}"')
    return name, pg.get_uri().replace("/postgres?", f"/{name}?")


def _drop_db(pg, name):
    _owner_exec(pg.get_uri(), f'DROP DATABASE IF EXISTS "{name}" WITH'
                              " (FORCE)")


def _role(owner_uri):
    return B2._q(owner_uri, "SELECT rolsuper, rolbypassrls, rolcreaterole,"
                            " rolcreatedb, rolreplication, rolinherit,"
                            " rolcanlogin FROM pg_roles WHERE rolname=%s",
                 (SYS,))[0]


def test_b122_7_role_ada_atribut_salah_diperbaiki_migrasi(pg, restore_role):
    _owner_exec(pg.get_uri(), f"ALTER ROLE {SYS} BYPASSRLS CREATEROLE"
                              " CREATEDB INHERIT REPLICATION NOLOGIN")
    name, uri = _fresh_db(pg)
    try:
        owner = dbmod.connect_postgres(uri)
        assert 8 in dbmod.migrate(owner)
        owner.close()
        assert _role(uri) == {"rolsuper": False, "rolbypassrls": False,
                              "rolcreaterole": False, "rolcreatedb": False,
                              "rolreplication": False, "rolinherit": False,
                              "rolcanlogin": True}
        app = uri.replace("postgres:@", "transcendence_app:@", 1)
        conn = dbmod.get_system_db(_cfg(app))           # kini lolos
        assert dbmod.verify_system_identity(conn) == []
        dbmod.close_db()
    finally:
        _drop_db(pg, name)


@pytest.mark.parametrize("grant", ["transcendence_app TO transcendence_system",
                                   "transcendence_system TO transcendence_app"])
def test_b122_7b_keanggotaan_tidak_aman_migrasi_gagal_tertutup(pg, grant,
                                                              restore_role):
    name, uri = _fresh_db(pg)
    _owner_exec(pg.get_uri(), f"GRANT {grant}")
    try:
        owner = dbmod.connect_postgres(uri)
        with pytest.raises(psycopg2.Error) as ei:
            dbmod.migrate(owner)
        assert "keanggotaan" in str(ei.value) or \
            "dapat memakai role" in str(ei.value)
        owner.rollback()
        owner.close()
        ver = {r["version"] for r in B2._q(uri, "SELECT version FROM"
                                                " schema_migrations")}
        assert 7 in ver and 8 not in ver               # 008 TIDAK diterapkan
        pol = {r["policyname"] for r in B2._q(
            uri, "SELECT policyname FROM pg_policies")}
        assert not any(p.endswith(("_app", "_system")) for p in pol)
    finally:
        _drop_db(pg, name)


# ============================================= H1: audit + thread-local
def test_b122_8_get_audit_db_validasi_sama_audit_tidak_diam(env, tmp_path,
                                                            caplog):
    owner_uri, app_uri = env
    _ditolak(_cfg(app_uri, owner_uri), "SUPERUSER")
    w = B11._two(env, tmp_path)
    bad = _cfg(app_uri, owner_uri)
    with w["app"].app_context():
        conn = dbmod.connect_postgres(app_uri)       # pemanggil tanpa scope
        conn.config = bad                            # DSN sistem salah
        try:
            ok = auditmod.audit(conn, "uji_b122_audit")
            assert ok is False
            with pytest.raises(auditmod.AuditWriteError):
                auditmod.audit(conn, "uji_b122_wajib", required=True)
        finally:
            conn.close()
    # slot koneksi audit TIDAK menyimpan koneksi yang gagal validasi
    assert dbmod._local.auditconn is None and \
        dbmod._local.auditconn_key is None
    dbmod.close_db()
    assert any(auditmod.FAIL_MARKER in r.getMessage() and
               "uji_b122_audit" in r.getMessage() and
               r.levelno >= logging.ERROR for r in caplog.records)
    assert B2._q(owner_uri, "SELECT 1 FROM audit_logs WHERE action LIKE"
                            " 'uji_b122%%'") == []


def test_b122_9_koneksi_gagal_validasi_tidak_tertinggal(env, monkeypatch):
    owner_uri, app_uri = env
    closes = {"n": 0}
    real = dbmod.PostgresConn.close

    def spy(self):
        closes["n"] += 1
        return real(self)
    monkeypatch.setattr(dbmod.PostgresConn, "close", spy)
    good = _cfg(app_uri, T12._sys_uri(app_uri))
    sc = dbmod.get_system_db(good)                    # slot sah terisi
    assert dbmod._local.sysconn is sc
    n0 = closes["n"]
    with pytest.raises(dbmod.SystemDbConfigError):
        dbmod.get_system_db(_cfg(app_uri, owner_uri))  # konfigurasi salah
    assert dbmod._local.sysconn is None and dbmod._local.sysconn_key is None
    assert closes["n"] >= n0 + 2          # koneksi lama + koneksi ditolak
    with pytest.raises(Exception):
        sc.execute("SELECT 1")                         # lama sudah ditutup
    with pytest.raises(dbmod.SystemDbConfigError):
        dbmod.get_audit_db(_cfg(app_uri, owner_uri))
    assert dbmod._local.auditconn is None
    # operasi sistem tidak berlanjut: system_conn juga gagal tertutup
    with pytest.raises(dbmod.SystemDbConfigError):
        with dbmod.system_conn(_cfg(app_uri, owner_uri)):
            raise AssertionError("blok tidak boleh dijalankan")
    assert _slots_empty()
    dbmod.close_db()


# ================================================ H4: hak minimum + guard
EXPECT_SYS = {
    "mt5_terminals": "DELETE,INSERT,SELECT,UPDATE",
    "mt5_connect_jobs": "DELETE,INSERT,SELECT,UPDATE",
    "mt5_accounts": "DELETE,INSERT,SELECT,UPDATE",
    "customers": "DELETE,INSERT,SELECT,UPDATE",
    "users": "DELETE,INSERT,SELECT,UPDATE",
    "auth_tokens": "DELETE,INSERT,SELECT,UPDATE",
    "audit_logs": "INSERT,SELECT",
    "sync_logs": "INSERT,SELECT",
    "mt5_credentials": "DELETE,INSERT,SELECT,UPDATE",
    "mt5_deals": "DELETE,INSERT,SELECT,UPDATE",
    "mt5_orders": "DELETE,INSERT,SELECT,UPDATE",
    "mt5_trades": "DELETE,INSERT,SELECT,UPDATE",
    "mt5_symbols": "DELETE,INSERT,SELECT,UPDATE",
    "mt5_sync_state": "DELETE,INSERT,SELECT,UPDATE",
    "auth_lockouts": "DELETE,INSERT,SELECT,UPDATE",
}
NO_RLS_ALLOWED = {"auth_throttle", "auth_lockouts", "schema_migrations"}


def test_b122_10_hak_minimum_role_sistem_persis(env):
    owner_uri, _ = env
    rows = B2._q(owner_uri, "SELECT table_name, string_agg(privilege_type,"
                            " ',' ORDER BY privilege_type) AS p FROM"
                            " information_schema.role_table_grants WHERE"
                            " grantee=%s GROUP BY table_name", (SYS,))
    assert {r["table_name"]: r["p"] for r in rows} == EXPECT_SYS
    dacl = B2._q(owner_uri, "SELECT defaclrole::regrole::text AS owner,"
                            " defaclacl::text AS acl FROM pg_default_acl")
    for r in dacl:
        assert SYS not in r["acl"] and "transcendence_app=" not in r["acl"],\
            r


def test_b122_11_tabel_baru_tanpa_rls_tidak_terbuka(env, tmp_path):
    """Owner membuat tabel BARU (tanpa RLS, tanpa GRANT) berisi data dua
    customer: role sistem MAUPUN role aplikasi tidak dapat membacanya."""
    w = B11._two(env, tmp_path)
    owner_uri, app_uri = env
    _owner_exec(owner_uri, "CREATE TABLE uji_b122_baru (customer_id bigint,"
                           " rahasia text); INSERT INTO uji_b122_baru VALUES"
                           f" ({w['A']['customer_id']}, 'a'),"
                           f" ({w['B']['customer_id']}, 'b')")
    try:
        for uri in (T12._sys_uri(app_uri), app_uri):
            for scope in (None, int(w["A"]["customer_id"])):
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    T12._as_app(uri, scope, "SELECT * FROM uji_b122_baru")
    finally:
        _owner_exec(owner_uri, "DROP TABLE uji_b122_baru")


def test_b122_12_guard_tabel_tanpa_rls_hanya_pengecualian(env):
    owner_uri, _ = env
    rows = B2._q(owner_uri, "SELECT c.relname FROM pg_class c JOIN"
                            " pg_namespace n ON n.oid=c.relnamespace WHERE"
                            " n.nspname='public' AND c.relkind='r' AND NOT"
                            " c.relrowsecurity")
    assert {r["relname"] for r in rows} <= NO_RLS_ALLOWED, rows
    # role sistem tidak punya hak pada tabel tanpa RLS kecuali auth_lockouts
    # (key_hash saja, pengecualian beralasan B12.1)
    assert set(EXPECT_SYS) & NO_RLS_ALLOWED == {"auth_lockouts"}


def test_b122_13_rollback_008_memulihkan_default_privileges_002(pg):
    name, uri = _fresh_db(pg)
    try:
        owner = dbmod.connect_postgres(uri)
        dbmod.migrate(owner)
        owner.close()

        def app_default():
            return any("transcendence_app=" in r["acl"] for r in B2._q(
                uri, "SELECT defaclacl::text AS acl FROM pg_default_acl"))
        assert app_default() is False
        T12._run_owner_sql(uri, T12.DOWN8)
        assert app_default() is True                   # keadaan 002/007
        assert B2._q(uri, "SELECT 1 FROM information_schema."
                          "role_table_grants WHERE grantee=%s", (SYS,)) == []
        owner = dbmod.connect_postgres(uri)
        assert dbmod.migrate(owner) == [8]
        owner.close()
        assert app_default() is False
    finally:
        _drop_db(pg, name)
