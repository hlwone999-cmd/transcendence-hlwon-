"""B12.4 — final hardening B12.3.

H1 diagnostik kepemilikan: nama objek asli tampil (bukan "%s %s"), tidak
   bergantung pada interpolasi parameter psycopg2.
H2 migrasi 008 oleh role migrasi NON-superuser (owner DB) dengan creator
   default privileges anggota / bukan anggota, global / per schema.
H3 kesetaraan snapshot & rollback default privileges: creator, schema,
   objtype, grantee, privilege, grantable, grantor, ACL mentah.

PostgreSQL lokal (pgserver 16). Superuser HANYA untuk persiapan (role, DB,
default privileges awal) dan pemeriksaan; migrasi pada tes utama H2
dijalankan oleh role non-superuser. Bukan staging / produksi.
"""
import contextlib
import re
import threading
import uuid

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402

import db as dbmod  # noqa: E402
from tests import test_f2_b2_races as B2  # noqa: E402
from tests import test_f2_b12_rls_tenant as T12  # noqa: E402
from tests import test_f2_b122_system_identity as T122  # noqa: E402
from tests import test_remediation_pr5_pg as PG  # noqa: E402

env = PG.env
pg = PG.pg
restore_role = T122.restore_role
SYS, APP = "transcendence_system", "transcendence_app"
_cfg, _owner_exec, _ditolak = T122._cfg, T122._owner_exec, T122._ditolak
# B12.4.1: TIDAK ada nama role tetap — setiap eksekusi fixture memakai nama
# unik (lihat _migr_env); konstanta global sengaja dihapus agar tes tidak
# dapat memakai/menghapus role milik pihak lain.


def _sys_cfg(app_uri):
    return _cfg(app_uri, T12._sys_uri(app_uri))


def _as(uri, user):
    return uri.replace("postgres:@", f"{user}:@", 1)


# ======================================================= H1 diagnostik
def test_b124_sql_identitas_tanpa_format_dan_tanpa_persen_ganda():
    sql = dbmod._IDENTITY_SQL
    assert "format(" not in sql
    assert re.findall(r"%.", sql) == ["%s"]         # hanya placeholder param


@pytest.mark.parametrize("kasus", [
    ("CREATE FUNCTION public.uji_b124_f(integer, text) RETURNS int"
     " LANGUAGE sql AS 'SELECT 1'; ALTER FUNCTION public.uji_b124_f(integer,"
     " text) OWNER TO transcendence_system",
     "DROP FUNCTION public.uji_b124_f(integer, text)",
     "function uji_b124_f(integer,text)"),
    ("CREATE PROCEDURE public.uji_b124_p(integer) LANGUAGE sql AS"
     " 'SELECT 1'; ALTER PROCEDURE public.uji_b124_p(integer) OWNER TO"
     " transcendence_system", "DROP PROCEDURE public.uji_b124_p(integer)",
     "procedure uji_b124_p(integer)"),
    ("CREATE AGGREGATE public.uji_b124_a(integer) (sfunc = int4pl, stype ="
     " integer); ALTER AGGREGATE public.uji_b124_a(integer) OWNER TO"
     " transcendence_system", "DROP AGGREGATE public.uji_b124_a(integer)",
     "aggregate uji_b124_a(integer)"),
    ("CREATE TYPE public.uji_b124_e AS ENUM ('x'); ALTER TYPE"
     " public.uji_b124_e OWNER TO transcendence_system",
     "DROP TYPE public.uji_b124_e", "enum uji_b124_e"),
    ("CREATE DOMAIN public.uji_b124_d AS text; ALTER DOMAIN"
     " public.uji_b124_d OWNER TO transcendence_system",
     "DROP DOMAIN public.uji_b124_d", "domain uji_b124_d"),
    ("CREATE TYPE public.uji_b124_c AS (a int, b text); ALTER TYPE"
     " public.uji_b124_c OWNER TO transcendence_system",
     "DROP TYPE public.uji_b124_c", "composite uji_b124_c"),
], ids=["function", "procedure", "aggregate", "enum", "domain", "composite"])
def test_b124_pesan_menampilkan_nama_objek_asli(env, kasus):
    owner_uri, app_uri = env
    create, drop, nama = kasus
    _owner_exec(owner_uri, create)
    try:
        msg = _ditolak(_sys_cfg(app_uri), nama)    # sistem & audit
        assert "%s" not in msg and "%%" not in msg, msg
    finally:
        _owner_exec(owner_uri, drop)


def test_b124_diagnostik_tetap_benar_tanpa_interpolasi_parameter(env):
    """Eksekusi SQL diagnostik dengan role aplikasi di-literal-kan (tanpa
    parameter psycopg2): label + nama objek tetap asli."""
    owner_uri, app_uri = env
    _owner_exec(owner_uri, "CREATE PROCEDURE public.uji_b124_q(integer)"
                           " LANGUAGE sql AS 'SELECT 1'; ALTER PROCEDURE"
                           " public.uji_b124_q(integer) OWNER TO"
                           " transcendence_system")
    try:
        sql = dbmod._IDENTITY_SQL.replace("%s", f"'{APP}'")
        c = psycopg2.connect(T12._sys_uri(app_uri))
        cur = c.cursor()
        cur.execute(sql)                            # TANPA parameter
        cols = [d[0] for d in cur.description]
        r = dict(zip(cols, cur.fetchone()))
        c.close()
        assert r["owned_procs"] == ["procedure uji_b124_q(integer)"]
    finally:
        _owner_exec(owner_uri, "DROP PROCEDURE public.uji_b124_q(integer)")


# ======================================== H2 migrasi oleh non-superuser
def _dacl_full(uri):
    """Keadaan pg_default_acl LENGKAP: per (creator, schema, objtype):
    ACL mentah (teks aclitem[] persis) + item terurai termasuk GRANTOR."""
    rows = B2._q(uri, """
        SELECT pg_get_userbyid(a.defaclrole) AS creator, n.nspname AS nsp,
               a.defaclobjtype::text AS t, a.defaclacl::text AS acl
          FROM pg_default_acl a
          LEFT JOIN pg_namespace n ON n.oid = a.defaclnamespace""")
    items = B2._q(uri, """
        SELECT pg_get_userbyid(a.defaclrole) AS creator, n.nspname AS nsp,
               a.defaclobjtype::text AS t,
               CASE x.grantee WHEN 0 THEN 'PUBLIC'
                    ELSE pg_get_userbyid(x.grantee) END AS grantee,
               pg_get_userbyid(x.grantor) AS grantor,
               x.privilege_type AS priv, x.is_grantable AS g
          FROM pg_default_acl a
          LEFT JOIN pg_namespace n ON n.oid = a.defaclnamespace,
               aclexplode(a.defaclacl) x""")
    out = {(r["creator"], r["nsp"], r["t"]): {"acl": r["acl"], "items": []}
           for r in rows}
    for i in items:
        out[(i["creator"], i["nsp"], i["t"])]["items"].append(
            (i["grantee"], i["grantor"], i["priv"], i["g"]))
    for v in out.values():
        v["items"].sort()
    return out


def _bocor(full):
    return {k for k, v in full.items() for (gr, _, _, _) in v["items"]
            if gr in (APP, SYS) or (gr == "PUBLIC" and k[2] in ("r", "S"))}


def _artefak_008(uri):
    return {
        "ver8": bool(B2._q(uri, "SELECT 1 FROM schema_migrations WHERE"
                                " version=8")),
        "pol": sorted(r["p"] for r in B2._q(
            uri, "SELECT tablename||'.'||policyname AS p FROM pg_policies"
                 " WHERE policyname LIKE '%%\\_app' OR policyname LIKE"
                 " '%%\\_system'")),
        "fn": sorted(r["proname"] for r in B2._q(
            uri, "SELECT proname FROM pg_proc WHERE proname IN"
                 " ('app_customer_allows','app_system_allows')")),
        "meta": bool(B2._q(uri, "SELECT 1 FROM pg_namespace WHERE nspname ="
                                " 'transcendence_meta'")),
        "sysgrants": sorted((r["table_name"], r["privilege_type"]) for r in
                            B2._q(uri, "SELECT table_name, privilege_type"
                                       " FROM information_schema."
                                       "role_table_grants WHERE grantee=%s",
                                  (SYS,))),
        "tabacl": sorted((r["relname"], r["acl"]) for r in B2._q(
            uri, "SELECT c.relname, c.relacl::text AS acl FROM pg_class c"
                 " JOIN pg_namespace n ON n.oid=c.relnamespace WHERE"
                 " n.nspname='public' AND c.relkind IN ('r','S')")),
    }


_PREP_LOCK = threading.Lock()


def _ensure_product_roles(root):
    """Role PRODUK (transcendence_app/_system) dibuat aman lewat migrasi
    superuser pada DB sementara milik fixture. Role produk adalah
    infrastruktur bersama cluster uji -> TIDAK dihapus fixture. Diserialkan
    agar dua fixture paralel tidak berlomba CREATE ROLE."""
    with _PREP_LOCK:
        if len(B2._q(root, "SELECT 1 FROM pg_roles WHERE rolname IN"
                           " (%s, %s)", (APP, SYS))) == 2:
            return
        tmp = f"b1241_prep_{uuid.uuid4().hex[:10]}"
        _owner_exec(root, f'CREATE DATABASE "{tmp}"')
        try:
            o = dbmod.connect_postgres(root.replace("/postgres?", f"/{tmp}?"))
            try:
                dbmod.migrate(o)
            finally:
                o.close()
        finally:
            _owner_exec(root, f'DROP DATABASE IF EXISTS "{tmp}" WITH (FORCE)')


@contextlib.contextmanager
def _migr_env(pg, fail_at=None):
    """B12.4.1: lingkungan migrator non-superuser TERISOLASI.

    - Nama role unik per eksekusi (migr/anggota/bukan/lain).
    - Hanya objek yang DIBUAT di sini (tercatat di `made`) yang dihapus;
      role dibuat dengan CREATE ROLE biasa (bukan IF NOT EXISTS) sehingga
      role yang sudah ada tidak pernah "diadopsi" lalu dihapus.
    - Cleanup di finally mencakup kegagalan di tengah setup.
    - Teardown memverifikasi tidak ada sisa role/DB fixture.
    fail_at: nama langkah untuk menyuntik kegagalan setup (uji)."""
    root = pg.get_uri()
    u = uuid.uuid4().hex[:10]
    n = {"migr": f"uji_migr_{u}", "anggota": f"uji_c_anggota_{u}",
         "bukan": f"uji_c_bukan_{u}", "lain": f"uji_lain_{u}"}
    made = {"roles": [], "dbs": []}

    def step(name):
        if fail_at == name:
            raise RuntimeError(f"uji: kegagalan setup disuntik di {name}")
    try:
        _ensure_product_roles(root)
        for key, attr in (("migr", "LOGIN"), ("anggota", "NOLOGIN"),
                          ("bukan", "NOLOGIN"), ("lain", "NOLOGIN")):
            _owner_exec(root, f"CREATE ROLE {n[key]} {attr} NOSUPERUSER"
                              " NOCREATEROLE NOCREATEDB NOBYPASSRLS")
            made["roles"].append(n[key])
            step(f"role:{key}")
        _owner_exec(root, f"GRANT {n['anggota']} TO {n['migr']}")
        step("membership")
        name = f"b1241_{u}"
        _owner_exec(root, f'CREATE DATABASE "{name}" OWNER {n["migr"]}')
        made["dbs"].append(name)
        step("database")
        su = root.replace("/postgres?", f"/{name}?")
        mig = _as(su, n["migr"])
        r = B2._q(mig, "SELECT rolsuper FROM pg_roles WHERE rolname ="
                       " current_user")
        assert r == [{"rolsuper": False}]          # prasyarat: NON-superuser
        yield {"su": su, "mig": mig, "root": root, "name": name, "n": n,
               "made": made}
    finally:
        _cleanup_env(root, made)


def _cleanup_env(root, made):
    """Hapus HANYA objek tercatat. Keanggotaan & default privileges milik
    role fixture ikut hilang bersama role/DB fixture; tidak ada REVOKE atau
    DROP terhadap objek di luar `made`."""
    errs = []
    for db in reversed(made["dbs"]):
        try:
            _owner_exec(root, f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        except psycopg2.Error as e:
            errs.append(f"db {db}: {e}")
    for role in reversed(made["roles"]):
        try:
            _owner_exec(root, f"DROP ROLE IF EXISTS {role}")
        except psycopg2.Error as e:
            errs.append(f"role {role}: {e}")
    sisa = B2._q(root, "SELECT rolname FROM pg_roles WHERE rolname ="
                       " ANY(%s)", (made["roles"],)) if made["roles"] else []
    sisa_db = B2._q(root, "SELECT datname FROM pg_database WHERE datname ="
                          " ANY(%s)", (made["dbs"],)) if made["dbs"] else []
    dbmod.close_db()
    if errs or sisa or sisa_db:
        raise AssertionError(f"cleanup fixture tidak tuntas: {errs} {sisa}"
                             f" {sisa_db}")


@pytest.fixture()
def migr_db(pg):
    with _migr_env(pg) as e:
        yield e


def _migrate_upto7(mig_uri, monkeypatch):
    real = dbmod._migration_files

    def upto7(conn):
        return [(v, f) for v, f in real(conn) if v <= 7]
    monkeypatch.setattr(dbmod, "_migration_files", upto7)
    c = dbmod.connect_postgres(mig_uri)
    assert dbmod.migrate(c) == [1, 2, 3, 4, 5, 6, 7]
    c.close()
    monkeypatch.undo()


def _defaults_anggota(su, n):
    _owner_exec(su, f"""
      ALTER DEFAULT PRIVILEGES FOR ROLE {n['anggota']}
        GRANT SELECT ON TABLES TO {APP};
      ALTER DEFAULT PRIVILEGES FOR ROLE {n['anggota']} IN SCHEMA public
        GRANT SELECT, INSERT ON TABLES TO {SYS}""")


def test_b124_h2_migrator_non_superuser_berhak_berhasil(migr_db,
                                                        monkeypatch):
    """(1) Role migrasi non-superuser, anggota creator (global + per schema)
    -> 008 berhasil; default privileges creator dicabut."""
    MIGR, C_ANGGOTA, C_BUKAN, C_LAIN = (migr_db["n"][k] for k in
        ("migr", "anggota", "bukan", "lain"))
    _migrate_upto7(migr_db["mig"], monkeypatch)
    _defaults_anggota(migr_db["su"], migr_db["n"])
    assert _bocor(_dacl_full(migr_db["su"]))         # ada yang harus dicabut
    c = dbmod.connect_postgres(migr_db["mig"])
    assert dbmod.migrate(c) == [8]
    c.close()
    assert _bocor(_dacl_full(migr_db["su"])) == set()
    art = _artefak_008(migr_db["su"])
    assert art["ver8"] and art["meta"] and len(art["fn"]) == 2 and art["pol"]
    app = _as(migr_db["su"], APP)
    conn = dbmod.get_system_db(_cfg(app))           # identitas sistem sah
    assert dbmod.verify_system_identity(conn) == []
    dbmod.close_db()


def test_b124_h2_migrator_tanpa_hak_berhenti_atomik_lalu_ulang(migr_db,
                                                              monkeypatch):
    """(2) creator BUKAN anggota role migrasi -> 008 berhenti eksplisit;
    (3) versi 8 tidak tercatat; (4) tanpa policy/fungsi/schema meta dan
    TANPA perubahan ACL parsial (default privileges creator anggota yang
    dapat dicabut pun utuh); (5) setelah hak diberikan, migrasi ulang
    berhasil."""
    MIGR, C_ANGGOTA, C_BUKAN, C_LAIN = (migr_db["n"][k] for k in
        ("migr", "anggota", "bukan", "lain"))
    _migrate_upto7(migr_db["mig"], monkeypatch)
    _defaults_anggota(migr_db["su"], migr_db["n"])
    _owner_exec(migr_db["su"], f"""
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_BUKAN} IN SCHEMA public
        GRANT SELECT ON TABLES TO {APP};
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_BUKAN}
        GRANT USAGE ON SEQUENCES TO {SYS}""")
    dacl0, art0 = _dacl_full(migr_db["su"]), _artefak_008(migr_db["su"])
    c = dbmod.connect_postgres(migr_db["mig"])
    with pytest.raises(psycopg2.Error) as ei:
        dbmod.migrate(c)
    # B12.4: pesan eksplisit migrasi TIDAK diganti MigrationPrivilegeError
    # generik ("tanpa hak DDL") — operator melihat creator & penyebabnya
    assert isinstance(ei.value, psycopg2.errors.RaiseException)
    msg = str(ei.value)
    assert "B12.3: default privileges creator" in msg and C_BUKAN in msg \
        and MIGR in msg and "tidak berhak mencabutnya" in msg, msg
    c.rollback()
    c.close()
    assert _artefak_008(migr_db["su"]) == art0       # (3)(4)
    assert art0["ver8"] is False and art0["meta"] is False and \
        art0["fn"] == [] and art0["pol"] == []
    assert _dacl_full(migr_db["su"]) == dacl0        # tanpa ACL parsial
    # (5) hak diberikan -> migrasi ulang berhasil
    _owner_exec(migr_db["root"], f"GRANT {C_BUKAN} TO {MIGR}")
    c = dbmod.connect_postgres(migr_db["mig"])
    assert dbmod.migrate(c) == [8]
    assert dbmod.migrate(c) == []
    c.close()
    assert _bocor(_dacl_full(migr_db["su"])) == set()


# =============================================== H3 kesetaraan snapshot
def test_b124_h3_snapshot_rollback_setara_penuh(migr_db, monkeypatch):
    """Banyak creator; grant ke app, system, PUBLIC, role lain; WITH GRANT
    OPTION; global & per schema. Setelah 008 + rollback, pg_default_acl
    SAMA PERSIS: creator, schema, objtype, grantee, grantor, privilege,
    grantable, dan teks ACL mentah. Migrasi oleh role non-superuser."""
    MIGR, C_ANGGOTA, C_BUKAN, C_LAIN = (migr_db["n"][k] for k in
        ("migr", "anggota", "bukan", "lain"))
    su, mig = migr_db["su"], migr_db["mig"]
    _migrate_upto7(mig, monkeypatch)
    _owner_exec(migr_db["root"], f"GRANT {C_BUKAN} TO {MIGR}")
    _owner_exec(su, f"""
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_ANGGOTA}
        GRANT SELECT, INSERT ON TABLES TO {APP} WITH GRANT OPTION;
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_ANGGOTA}
        GRANT USAGE ON SEQUENCES TO {SYS};
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_ANGGOTA} IN SCHEMA public
        GRANT SELECT ON TABLES TO PUBLIC;
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_ANGGOTA} IN SCHEMA public
        GRANT UPDATE ON TABLES TO {SYS} WITH GRANT OPTION;
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_ANGGOTA} IN SCHEMA public
        GRANT SELECT ON TABLES TO {C_LAIN} WITH GRANT OPTION;
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_BUKAN}
        GRANT SELECT ON TABLES TO PUBLIC;
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_BUKAN}
        GRANT EXECUTE ON FUNCTIONS TO {APP} WITH GRANT OPTION;
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_BUKAN}
        GRANT USAGE ON TYPES TO {SYS};
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_BUKAN} IN SCHEMA public
        GRANT DELETE ON TABLES TO {APP};
      ALTER DEFAULT PRIVILEGES FOR ROLE {C_BUKAN} IN SCHEMA public
        GRANT USAGE ON SEQUENCES TO {C_LAIN}""")
    pre = _dacl_full(su)
    creators = {k[0] for k in pre}
    assert {C_ANGGOTA, C_BUKAN, MIGR} <= creators
    levels = {(k[0], k[1] is None) for k in pre}
    assert (C_ANGGOTA, True) in levels and (C_ANGGOTA, False) in levels
    grantable = [i for v in pre.values() for i in v["items"] if i[3]]
    assert {i[0] for i in grantable} >= {APP, SYS, C_LAIN}
    # keterbatasan PostgreSQL: grantor aclitem default ACL SELALU = creator
    # (tidak mungkin beberapa grantor) -> dibuktikan, bukan diasumsikan
    assert all(i[1] == k[0] for k, v in pre.items() for i in v["items"])
    c = dbmod.connect_postgres(mig)
    assert dbmod.migrate(c) == [8]
    c.close()
    mid = _dacl_full(su)
    assert _bocor(mid) == set()
    # grant kepada role LAIN (bukan app/system/PUBLIC-data) tidak disentuh
    lain = lambda d: sorted((k, i) for k, v in d.items()  # noqa: E731
                            for i in v["items"] if i[0] == C_LAIN)
    assert lain(mid) == lain(pre)
    # snapshot = keadaan pra-008 apa adanya (ACL mentah)
    snap = {(r["creator"], r["nspname"], r["objtype"]): r["acl"] for r in
            B2._q(su, "SELECT creator, nspname, objtype::text, acl::text"
                      " FROM transcendence_meta.default_acl_pre_008")}
    assert snap == {k: v["acl"] for k, v in pre.items()}
    T12._run_owner_sql(mig, T12.DOWN8)              # rollback oleh migrator
    post = _dacl_full(su)
    assert set(post) == set(pre)
    for k in pre:
        assert post[k]["items"] == pre[k]["items"], k      # termasuk grantor
        assert post[k]["acl"] == pre[k]["acl"], (k, pre[k]["acl"],
                                                 post[k]["acl"])
    c = dbmod.connect_postgres(mig)
    assert dbmod.migrate(c) == [8]                  # ulang konsisten
    c.close()
    assert _dacl_full(su) == mid


# ================================ B12.4.1 isolasi fixture (bukti harness)
_NAMA_LAMA = ("uji_migr_b124", "uji_c_anggota_b124", "uji_c_bukan_b124",
              "uji_lain_b124")                # nama tetap fixture B12.4


def _roles_like(root, pat):
    return sorted(r["rolname"] for r in B2._q(
        root, "SELECT rolname FROM pg_roles WHERE rolname LIKE %s", (pat,)))


def _memberships(root, names):
    return sorted((r["role"], r["member"], r["admin_option"],
                   r["inherit_option"], r["set_option"]) for r in B2._q(
        root, "SELECT g.rolname AS role, m.rolname AS member,"
              " am.admin_option, am.inherit_option, am.set_option"
              " FROM pg_auth_members am JOIN pg_roles g ON g.oid=am.roleid"
              " JOIN pg_roles m ON m.oid=am.member"
              " WHERE g.rolname = ANY(%s) OR m.rolname = ANY(%s)",
        (list(names), list(names))))


@pytest.fixture()
def sentinel(pg):
    """Role, membership, database & default privileges milik 'pihak lain'
    yang sudah ada SEBELUM fixture — termasuk role dengan nama tetap lama
    B12.4 (kasus yang dulu dapat terhapus oleh cleanup)."""
    root = pg.get_uri()
    u = uuid.uuid4().hex[:8]
    s = {"a": f"uji_sentinel_a_{u}", "b": f"uji_sentinel_b_{u}",
         "db": f"b1241_sentinel_{u}"}
    lama_dibuat = []
    for r in (s["a"], s["b"]):
        _owner_exec(root, f"CREATE ROLE {r} NOLOGIN")
    for r in _NAMA_LAMA:
        if not B2._q(root, "SELECT 1 FROM pg_roles WHERE rolname=%s", (r,)):
            _owner_exec(root, f"CREATE ROLE {r} NOLOGIN")
            lama_dibuat.append(r)
    _owner_exec(root, f"GRANT {s['a']} TO {s['b']} WITH ADMIN OPTION")
    # membership antar nama lama HANYA bila keduanya dibuat sentinel ini
    # (tidak pernah mengubah membership role yang sudah ada sebelumnya)
    if {_NAMA_LAMA[0], _NAMA_LAMA[1]} <= set(lama_dibuat):
        _owner_exec(root, f"GRANT {_NAMA_LAMA[1]} TO {_NAMA_LAMA[0]}")
    _owner_exec(root, f'CREATE DATABASE "{s["db"]}"')
    sdb = root.replace("/postgres?", f"/{s['db']}?")
    _owner_exec(sdb, f"ALTER DEFAULT PRIVILEGES FOR ROLE {s['a']} GRANT"
                     f" SELECT ON TABLES TO {s['b']} WITH GRANT OPTION")
    s.update(root=root, sdb=sdb)
    yield s
    _owner_exec(root, f'DROP DATABASE IF EXISTS "{s["db"]}" WITH (FORCE)')
    for r in (s["b"], s["a"], *reversed(lama_dibuat)):
        _owner_exec(root, f"DROP ROLE IF EXISTS {r}")


def _sentinel_state(s):
    names = (s["a"], s["b"], *_NAMA_LAMA)
    return {
        "roles": B2._q(s["root"], "SELECT rolname, rolsuper, rolcanlogin,"
                                  " rolinherit FROM pg_roles WHERE rolname ="
                                  " ANY(%s) ORDER BY 1", (list(names),)),
        "member": _memberships(s["root"], names),
        "db": B2._q(s["root"], "SELECT datname FROM pg_database WHERE"
                               " datname=%s", (s["db"],)),
        "dacl": _dacl_full(s["sdb"]),
    }


def test_b1241_fixture_tidak_menyentuh_sentinel_dan_bersih(pg, sentinel):
    before = _sentinel_state(sentinel)
    assert len(before["roles"]) == 2 + len(_NAMA_LAMA)
    with _migr_env(pg) as e:
        n = e["n"]
        assert not set(n.values()) & set(_NAMA_LAMA)   # nama unik
        assert set(_roles_like(e["root"], f"%{n['migr'][9:]}")) == \
            set(n.values())
        c = dbmod.connect_postgres(e["mig"])
        dbmod.migrate(c)                              # pakai fixture sungguh
        c.close()
        made = dict(e["made"])
    assert _sentinel_state(sentinel) == before        # sentinel utuh persis
    assert B2._q(sentinel["root"], "SELECT rolname FROM pg_roles WHERE"
                                   " rolname = ANY(%s)",
                 (made["roles"],)) == []               # semua role fixture
    assert B2._q(sentinel["root"], "SELECT 1 FROM pg_database WHERE datname"
                                   " = ANY(%s)", (made["dbs"],)) == []


@pytest.mark.parametrize("fail_at", ["role:migr", "role:bukan",
                                     "membership", "database"])
def test_b1241_kegagalan_setup_tetap_dibersihkan(pg, sentinel, fail_at):
    before = _sentinel_state(sentinel)
    pre_roles = set(_roles_like(sentinel["root"], "uji_%"))
    pre_dbs = {r["datname"] for r in B2._q(sentinel["root"],
                                           "SELECT datname FROM pg_database")}
    with pytest.raises(RuntimeError, match="disuntik"):
        with _migr_env(pg, fail_at=fail_at):
            pass
    assert set(_roles_like(sentinel["root"], "uji_%")) == pre_roles
    assert {r["datname"] for r in B2._q(sentinel["root"], "SELECT datname"
                                        " FROM pg_database")} == pre_dbs
    assert _sentinel_state(sentinel) == before


def test_b1241_dua_fixture_paralel_tidak_berbenturan(pg, sentinel):
    before = _sentinel_state(sentinel)
    hasil, err = {}, []
    barrier = threading.Barrier(2)

    def run(i):
        try:
            with _migr_env(pg) as e:
                barrier.wait(timeout=60)              # benar-benar bersamaan
                c = dbmod.connect_postgres(e["mig"])
                try:
                    hasil[i] = {"ver": dbmod.migrate(c), "n": dict(e["n"]),
                                "made": dict(e["made"])}
                finally:
                    c.close()
                _defaults_anggota(e["su"], e["n"])
                hasil[i]["dacl_keys"] = sorted(
                    k[0] for k in _dacl_full(e["su"]))
        except Exception as ex:  # noqa: BLE001
            err.append(repr(ex))
    th = [threading.Thread(target=run, args=(i,)) for i in (0, 1)]
    [t.start() for t in th]
    [t.join(300) for t in th]
    assert err == [], err
    a, b = hasil[0], hasil[1]
    assert a["ver"] == b["ver"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert set(a["n"].values()).isdisjoint(b["n"].values())
    assert set(a["made"]["dbs"]).isdisjoint(b["made"]["dbs"])
    # default privileges tiap fixture hanya milik role fixture itu sendiri
    assert b["n"]["anggota"] not in a["dacl_keys"]
    assert a["n"]["anggota"] not in b["dacl_keys"]
    for h in (a, b):
        assert B2._q(sentinel["root"], "SELECT 1 FROM pg_roles WHERE rolname"
                                       " = ANY(%s)", (h["made"]["roles"],)) \
            == []
    assert _sentinel_state(sentinel) == before
