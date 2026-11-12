"""B12.3 — validasi kepemilikan lengkap role sistem, DEFAULT PRIVILEGES
lintas creator, serta migrasi/rollback 008.

PostgreSQL lokal (pgserver 16). Role & default privileges bersifat
cluster/DB-wide: setiap tes membersihkan objek dan role ujinya di finally.
Bukan staging / produksi.
"""
import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402

import db as dbmod  # noqa: E402
from tests import test_f2_b2_races as B2  # noqa: E402
from tests import test_f2_b11_customer_mapping as B11  # noqa: E402
from tests import test_f2_b12_rls_tenant as T12  # noqa: E402
from tests import test_f2_b122_system_identity as T122  # noqa: E402
from tests import test_remediation_pr5_pg as PG  # noqa: E402

env = PG.env
pg = PG.pg
restore_role = T122.restore_role
_tanpa_chown = B2._tanpa_chown
SYS = T122.SYS
_cfg, _owner_exec, _ditolak = T122._cfg, T122._owner_exec, T122._ditolak
_fresh_db, _drop_db = T122._fresh_db, T122._drop_db


def _sys_cfg(app_uri):
    return _cfg(app_uri, T12._sys_uri(app_uri))


def _app_uri(owner_uri):
    return owner_uri.replace("postgres:@", "transcendence_app:@", 1)


def _dacl(uri):
    """pg_default_acl ternormalisasi: {(creator, schema|None, objtype):
    sorted[(grantee, privilege, grantable)]}."""
    rows = B2._q(uri, """
        SELECT pg_get_userbyid(a.defaclrole) AS creator, n.nspname AS nsp,
               a.defaclobjtype::text AS t,
               CASE x.grantee WHEN 0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(x.grantee) END AS grantee,
               x.privilege_type AS priv, x.is_grantable AS g
          FROM pg_default_acl a
          LEFT JOIN pg_namespace n ON n.oid = a.defaclnamespace,
               aclexplode(a.defaclacl) x""")
    out = {}
    for r in rows:
        out.setdefault((r["creator"], r["nsp"], r["t"]), []).append(
            (r["grantee"], r["priv"], r["g"]))
    return {k: sorted(v) for k, v in out.items()}


def _bocor(dacl):
    """Entri default yang memberi hak otomatis kepada role aplikasi/sistem
    atau PUBLIC pada tabel/sequence."""
    return {k: [x for x in v if x[0] in ("transcendence_app", SYS) or
                (x[0] == "PUBLIC" and k[2] in ("r", "S"))]
            for k, v in dacl.items()
            if any(x[0] in ("transcendence_app", SYS) or
                   (x[0] == "PUBLIC" and k[2] in ("r", "S")) for x in v)}


# ============================================== H1 kepemilikan (runtime)
@pytest.mark.parametrize("kasus", [
    ("schema", "CREATE SCHEMA uji_b123_s AUTHORIZATION transcendence_system",
     "DROP SCHEMA uji_b123_s", "schema ['uji_b123_s']"),
    ("function", "CREATE FUNCTION public.uji_b123_f() RETURNS int LANGUAGE"
     " sql AS 'SELECT 1'; ALTER FUNCTION public.uji_b123_f() OWNER TO"
     " transcendence_system", "DROP FUNCTION public.uji_b123_f()",
     "function uji_b123_f()"),
    ("procedure", "CREATE PROCEDURE public.uji_b123_p() LANGUAGE sql AS"
     " 'SELECT 1'; ALTER PROCEDURE public.uji_b123_p() OWNER TO"
     " transcendence_system", "DROP PROCEDURE public.uji_b123_p()",
     "procedure uji_b123_p()"),
    ("enum", "CREATE TYPE public.uji_b123_e AS ENUM ('a'); ALTER TYPE"
     " public.uji_b123_e OWNER TO transcendence_system",
     "DROP TYPE public.uji_b123_e", "enum uji_b123_e"),
    ("domain", "CREATE DOMAIN public.uji_b123_d AS int; ALTER DOMAIN"
     " public.uji_b123_d OWNER TO transcendence_system",
     "DROP DOMAIN public.uji_b123_d", "domain uji_b123_d"),
    ("composite", "CREATE TYPE public.uji_b123_c AS (a int); ALTER TYPE"
     " public.uji_b123_c OWNER TO transcendence_system",
     "DROP TYPE public.uji_b123_c", "composite uji_b123_c"),
    ("collation", "CREATE COLLATION public.uji_b123_col (locale = 'C');"
     " ALTER COLLATION public.uji_b123_col OWNER TO transcendence_system",
     "DROP COLLATION public.uji_b123_col", "collation"),
    ("default_acl", "ALTER DEFAULT PRIVILEGES FOR ROLE transcendence_system"
     " GRANT SELECT ON TABLES TO transcendence_app",
     "ALTER DEFAULT PRIVILEGES FOR ROLE transcendence_system REVOKE SELECT"
     " ON TABLES FROM transcendence_app", "default privileges"),
], ids=lambda k: k[0])
def test_b123_kepemilikan_objek_aplikasi_ditolak(env, kasus):
    owner_uri, app_uri = env
    _, create, drop, frag = kasus
    _owner_exec(owner_uri, create)
    try:
        msg = _ditolak(_sys_cfg(app_uri), frag)
        assert "DITOLAK" in msg
    finally:
        _owner_exec(owner_uri, drop)
    conn = dbmod.get_system_db(_sys_cfg(app_uri))       # bersih -> diterima
    assert dbmod.verify_system_identity(conn) == []
    dbmod.close_db()


def test_b123_kepemilikan_di_database_lain_ditolak(env, pg):
    _, app_uri = env
    name, uri = _fresh_db(pg)
    try:
        _owner_exec(uri, "CREATE SCHEMA uji_b123_lain AUTHORIZATION"
                         " transcendence_system")
        _ditolak(_sys_cfg(app_uri), "objek di database lain")
    finally:
        _drop_db(pg, name)
    conn = dbmod.get_system_db(_sys_cfg(app_uri))
    assert dbmod.verify_system_identity(conn) == []
    dbmod.close_db()


def test_b123_tanpa_kepemilikan_diterima_dan_objek_internal_tidak_dihitung(
        env):
    """Role sistem tanpa kepemilikan objek aplikasi -> diterima, padahal DB
    berisi objek internal PostgreSQL (pg_catalog, information_schema) dan
    schema meta 008 milik owner."""
    owner_uri, app_uri = env
    assert B2._q(owner_uri, "SELECT 1 FROM pg_namespace WHERE nspname ="
                            " 'transcendence_meta'")       # ada (milik owner)
    n_internal = B2._q(owner_uri, "SELECT count(*) AS n FROM pg_proc p JOIN"
                                  " pg_namespace n ON n.oid=p.pronamespace"
                                  " WHERE n.nspname='pg_catalog'")[0]["n"]
    assert n_internal > 1000
    for opener in (dbmod.get_system_db, dbmod.get_audit_db):
        conn = opener(_sys_cfg(app_uri))
        assert dbmod.verify_system_identity(conn) == []
    dbmod.close_db()


@pytest.mark.parametrize("kasus", [
    ("schema", "CREATE SCHEMA uji_b123_m AUTHORIZATION transcendence_system"),
    ("function", "CREATE FUNCTION public.uji_b123_mf() RETURNS int LANGUAGE"
     " sql AS 'SELECT 1'; ALTER FUNCTION public.uji_b123_mf() OWNER TO"
     " transcendence_system"),
    ("type", "CREATE TYPE public.uji_b123_mt AS ENUM ('x'); ALTER TYPE"
     " public.uji_b123_mt OWNER TO transcendence_system"),
], ids=lambda k: k[0])
def test_b123_migrasi_008_berhenti_bila_kepemilikan_tidak_aman(pg, kasus):
    """Objek milik role sistem sudah ada sebelum 008 -> migrasi BERHENTI,
    versi 8 TIDAK tercatat, tidak ada artefak 008 (policy, fungsi, schema
    meta), dan kepemilikan TIDAK dipindahkan otomatis."""
    name, uri = _fresh_db(pg)
    try:
        _owner_exec(uri, kasus[1])
        owner = dbmod.connect_postgres(uri)
        with pytest.raises(psycopg2.Error) as ei:
            dbmod.migrate(owner)
        assert "memiliki objek" in str(ei.value) or \
            "memiliki schema" in str(ei.value), str(ei.value)
        owner.rollback()
        owner.close()
        ver = {r["version"] for r in B2._q(uri, "SELECT version FROM"
                                                " schema_migrations")}
        assert 7 in ver and 8 not in ver
        assert B2._q(uri, "SELECT 1 FROM pg_namespace WHERE nspname ="
                          " 'transcendence_meta'") == []
        assert B2._q(uri, "SELECT 1 FROM pg_proc WHERE proname IN"
                          " ('app_customer_allows','app_system_allows')") \
            == []
        owned = B2._q(uri, "SELECT count(*) AS n FROM pg_shdepend d WHERE"
                           " d.refobjid = %s::regrole AND d.deptype='o'",
                      (SYS,))[0]["n"]
        assert owned >= 1                          # tidak dipindahkan diam
    finally:
        _drop_db(pg, name)


# ====================================== H2 DEFAULT PRIVILEGES lintas creator
CREATORS = ("uji_creator_a_b123", "uji_creator_b_b123")


def _mk_creators(pg, uri):
    for r in CREATORS:
        _owner_exec(pg.get_uri(), f"DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM"
                    f" pg_roles WHERE rolname='{r}') THEN CREATE ROLE {r}"
                    " NOLOGIN; END IF; END$$")
        _owner_exec(uri, f"GRANT USAGE, CREATE ON SCHEMA public TO {r}")


def _drop_creators(pg):
    for r in CREATORS:
        try:
            _owner_exec(pg.get_uri(), f"DROP ROLE IF EXISTS {r}")
        except psycopg2.Error:
            pass


def test_b123_default_privileges_lintas_creator(pg):
    a, b = CREATORS
    name, uri = _fresh_db(pg)
    try:
        owner = dbmod.connect_postgres(uri)
        dbmod.migrate(owner)
        owner.close()
        T12._run_owner_sql(uri, T12.DOWN8)              # keadaan pra-008
        _mk_creators(pg, uri)
        _owner_exec(uri, f"""
          ALTER DEFAULT PRIVILEGES FOR ROLE {a} IN SCHEMA public
            GRANT SELECT, INSERT ON TABLES TO transcendence_app;
          ALTER DEFAULT PRIVILEGES FOR ROLE {a}
            GRANT USAGE ON SEQUENCES TO transcendence_system;
          ALTER DEFAULT PRIVILEGES FOR ROLE {b}
            GRANT SELECT ON TABLES TO transcendence_system;
          ALTER DEFAULT PRIVILEGES FOR ROLE {b} IN SCHEMA public
            GRANT SELECT ON TABLES TO PUBLIC;
          ALTER DEFAULT PRIVILEGES FOR ROLE {b}
            GRANT EXECUTE ON FUNCTIONS TO transcendence_app""")
        pre = _dacl(uri)
        creators_pre = {k[0] for k in _bocor(pre)}
        assert {a, b, "postgres"} <= creators_pre   # 002 + dua creator
        owner = dbmod.connect_postgres(uri)
        assert dbmod.migrate(owner) == [8]
        owner.close()
        assert _bocor(_dacl(uri)) == {}             # SETIAP creator dicabut
        # tabel baru oleh creator A dan B: tidak terbuka bagi kedua role
        _owner_exec(uri, f"""
          SET ROLE {a}; CREATE TABLE public.uji_tab_a (cid bigint, x text);
          INSERT INTO public.uji_tab_a VALUES (1, 'rahasia-a'); RESET ROLE;
          SET ROLE {b}; CREATE TABLE public.uji_tab_b (cid bigint, x text);
          INSERT INTO public.uji_tab_b VALUES (2, 'rahasia-b'); RESET ROLE""")
        app = _app_uri(uri)
        for u in (app, T12._sys_uri(app)):
            for t in ("uji_tab_a", "uji_tab_b"):
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    T12._as_app(u, None, f"SELECT * FROM public.{t}")
        acl = B2._q(uri, "SELECT relname, relacl::text AS acl FROM pg_class"
                         " WHERE relname IN ('uji_tab_a','uji_tab_b')")
        assert all("transcendence" not in (r["acl"] or "") and
                   "=r/" not in (r["acl"] or "") for r in acl), acl
        # migrasi berikutnya tetap dapat GRANT eksplisit
        _owner_exec(uri, "GRANT SELECT ON public.uji_tab_a TO"
                         " transcendence_app")
        rows, _ = T12._as_app(app, None, "SELECT x FROM public.uji_tab_a")
        assert rows == [{"x": "rahasia-a"}]
        # ---- H3: rollback memulihkan default privileges PERSIS pra-008
        _owner_exec(uri, "DROP TABLE public.uji_tab_a, public.uji_tab_b")
        T12._run_owner_sql(uri, T12.DOWN8)
        assert _dacl(uri) == pre
        # ---- migrasi ulang konsisten
        owner = dbmod.connect_postgres(uri)
        assert dbmod.migrate(owner) == [8]
        assert dbmod.migrate(owner) == []
        owner.close()
        assert _bocor(_dacl(uri)) == {}
    finally:
        _drop_db(pg, name)
        _drop_creators(pg)


# ================================================ H3 rollback tanpa hapus
def _inventaris(uri):
    """Objek & data di luar artefak 008 sendiri."""
    tabs = [r["relname"] for r in B2._q(
        uri, "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON"
             " n.oid=c.relnamespace WHERE n.nspname='public' AND"
             " c.relkind IN ('r','S','v','i') ORDER BY 1")]
    # schema_migrations = ledger (rollback 008 memang menghapus versi 8);
    # diperiksa terpisah di tes.
    counts = {t: B2._q(uri, f'SELECT count(*) AS n FROM public."{t}"')[0]
              ["n"] for t in tabs if t != "schema_migrations" and
              B2._q(uri, "SELECT relkind FROM pg_class WHERE relname=%s",
                    (t,))[0]["relkind"] == "r"}
    procs = sorted(r["p"] for r in B2._q(
        uri, "SELECT p.oid::regprocedure::text AS p FROM pg_proc p JOIN"
             " pg_namespace n ON n.oid=p.pronamespace WHERE"
             " n.nspname='public' AND p.proname NOT IN"
             " ('app_customer_allows','app_system_allows','app_rls_allows')"))
    schemas = sorted(r["nspname"] for r in B2._q(
        uri, "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%%'"
             " AND nspname NOT IN ('information_schema','transcendence_meta')"))
    return tabs, counts, procs, schemas


def test_b123_rollback_tidak_menghapus_objek_atau_data_dan_ulang_konsisten(
        env, tmp_path):
    w = B11._two(env, tmp_path)
    T12._ensure_rows(w)
    owner_uri = env[0]
    before = _inventaris(owner_uri)
    assert sum(before[1].values()) > 0
    dacl_008 = _dacl(owner_uri)
    ver = lambda: {r["version"] for r in B2._q(  # noqa: E731
        owner_uri, "SELECT version FROM schema_migrations")}
    assert 8 in ver()
    T12._run_owner_sql(owner_uri, T12.DOWN8)
    assert _inventaris(owner_uri) == before          # objek & data utuh
    assert 8 not in ver() and {1, 2, 3, 4, 5, 6, 7} <= ver()
    assert B2._q(owner_uri, "SELECT 1 FROM pg_namespace WHERE nspname ="
                            " 'transcendence_meta'") == []
    assert any(x[0] == "transcendence_app" for v in _dacl(owner_uri).values()
               for x in v)                           # 002 dipulihkan
    owner = dbmod.connect_postgres(owner_uri)
    assert dbmod.migrate(owner) == [8]
    owner.close()
    assert _inventaris(owner_uri) == before
    assert 8 in ver()
    assert _dacl(owner_uri) == dacl_008              # konsisten
    conn = dbmod.get_system_db(_sys_cfg(env[1]))
    assert dbmod.verify_system_identity(conn) == []
    dbmod.close_db()


def test_b123_rollback_tanpa_snapshot_berhenti(env):
    owner_uri, _ = env
    _owner_exec(owner_uri, "ALTER TABLE transcendence_meta."
                           "default_acl_pre_008 RENAME TO cadangan_uji")
    try:
        with pytest.raises(psycopg2.Error) as ei:
            T12._run_owner_sql(owner_uri, T12.DOWN8)
        assert "snapshot default privileges" in str(ei.value)
        ver = {r["version"] for r in B2._q(owner_uri, "SELECT version FROM"
                                                      " schema_migrations")}
        assert 8 in ver                              # tidak setengah jalan
        assert B2._q(owner_uri, "SELECT 1 FROM pg_policies WHERE policyname"
                                " = 'customers_app'")
    finally:
        _owner_exec(owner_uri, "ALTER TABLE transcendence_meta.cadangan_uji"
                               " RENAME TO default_acl_pre_008")
