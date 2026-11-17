"""B12 — (A) pemulihan tahap 2 dengan identitas lengkap job <-> terminal;
(B) T3-3: RLS PostgreSQL pada mt5_terminals, mt5_connect_jobs,
mt5_accounts, customers, auth_tokens, audit_logs.

PostgreSQL lokal (pgserver), role aplikasi NON-OWNER `transcendence_app`,
RLS aktif. Uji lintas-customer dijalankan LANGSUNG dengan SQL sebagai role
aplikasi (bukan lewat validasi aplikasi) sehingga penolakan terbukti
berasal dari PostgreSQL. Owner hanya untuk menyiapkan & memeriksa data.
Bukan staging / produksi.
"""
from pathlib import Path

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402
import psycopg2.extras  # noqa: E402

import db as dbmod  # noqa: E402
from provisioner import mt5_provision_helper as H  # noqa: E402
from services import provisioning as PV  # noqa: E402
from tests import test_f2_b2_races as B2  # noqa: E402
from tests import test_f2_b11_customer_mapping as B11  # noqa: E402
from tests import test_remediation_pr5_pg as PG  # noqa: E402

env = PG.env
pg = PG.pg
_tanpa_chown = B2._tanpa_chown

TABLES = {                     # tabel -> kolom customer
    "mt5_terminals": "customer_id",
    "mt5_connect_jobs": "customer_id",
    "mt5_accounts": "customer_id",
    "customers": "id",
    "auth_tokens": "customer_id",
    "audit_logs": "customer_id",
}
DOWN = (Path(dbmod.MIGRATIONS_DIR) / "postgres" / "rollback" /
        "007_rls_tenant_tables.down.sql")
DOWN8 = (Path(dbmod.MIGRATIONS_DIR) / "postgres" / "rollback" /
         "008_system_role_rls.down.sql")


def _sys_uri(app_uri):
    """B12.1: DSN role SISTEM (kredensial terpisah; lokal: trust)."""
    return app_uri.replace("transcendence_app:@", "transcendence_system:@")


# ------------------------------------------------------------ pembantu
def _scope_sql(scope):
    if scope is None:
        return "SELECT set_config('app.customer_id','',true)," \
               " set_config('app.scope','',true)"
    if scope == "system":
        return "SELECT set_config('app.customer_id','',true)," \
               " set_config('app.scope','system',true)"
    if isinstance(scope, tuple):         # (customer, 'system') bersamaan
        return ("SELECT set_config('app.customer_id','%d',true),"
                " set_config('app.scope','system',true)" % int(scope[0]))
    return ("SELECT set_config('app.customer_id','%d',true),"
            " set_config('app.scope','',true)" % int(scope))


def _as_app(app_uri, scope, sql, params=()):
    """Satu transaksi sebagai ROLE APLIKASI dengan scope tertentu.
    Return (rows, rowcount). Galat PostgreSQL diteruskan apa adanya."""
    c = psycopg2.connect(app_uri)
    try:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(_scope_sql(scope))
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        n = cur.rowcount
        c.commit()
        return rows, n
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _ensure_rows(w):
    """Pastikan SETIAP tabel punya baris untuk A dan B (via owner)."""
    q = w["owner"]
    for T in (w["A"], w["B"]):
        cid = T["customer_id"]
        if not B2._q(q, "SELECT 1 FROM auth_tokens WHERE customer_id=%s",
                     (cid,)):
            uid = B2._q(q, "SELECT id FROM users WHERE customer_id=%s",
                        (cid,))[0]["id"]
            B2._q(q, "INSERT INTO auth_tokens (user_id, customer_id,"
                     " token_hash, expires_at) VALUES (%s,%s,%s,"
                     " '2099-01-01T00:00:00Z') RETURNING id",
                  (uid, cid, "uji-b12-%d" % cid))
        if not B2._q(q, "SELECT 1 FROM audit_logs WHERE customer_id=%s",
                     (cid,)):
            B2._q(q, "INSERT INTO audit_logs (customer_id, action) VALUES"
                     " (%s,'uji_b12') RETURNING id", (cid,))
    for t, col in TABLES.items():
        for T in (w["A"], w["B"]):
            assert B2._q(q, f"SELECT 1 FROM {t} WHERE {col}=%s",
                         (T["customer_id"],)), (t, T["customer_id"])


def _owner_rows(w, t, col, cid):
    return B2._q(w["owner"], f"SELECT * FROM {t} WHERE {col}=%s ORDER BY 1",
                 (cid,))


def _ditolak_rls(e):
    return isinstance(e, psycopg2.errors.InsufficientPrivilege) and \
        "row-level security" in str(e)


# ====================================================== B. RLS (T3-3)
@pytest.mark.parametrize("t", list(TABLES))
def test_b12_customer_a_tidak_dapat_membaca_baris_b(env, tmp_path, t):
    w = B11._two(env, tmp_path)
    _ensure_rows(w)
    col, a, b = TABLES[t], w["A"]["customer_id"], w["B"]["customer_id"]
    app_uri = env[1]
    rows, _ = _as_app(app_uri, a, f"SELECT {col} AS c FROM {t}")
    assert rows and {int(r["c"]) for r in rows} == {int(a)}
    rows, _ = _as_app(app_uri, a, f"SELECT * FROM {t} WHERE {col}=%s",
                      (b,))
    assert rows == []                       # baris B tidak terlihat
    rows, _ = _as_app(app_uri, b, f"SELECT {col} AS c FROM {t}")
    assert {int(r["c"]) for r in rows} == {int(b)}


@pytest.mark.parametrize("t", list(TABLES))
def test_b12_customer_a_tidak_dapat_update_delete_baris_b(env, tmp_path, t):
    w = B11._two(env, tmp_path)
    _ensure_rows(w)
    col, a, b = TABLES[t], w["A"]["customer_id"], w["B"]["customer_id"]
    app_uri = env[1]
    b0 = _owner_rows(w, t, col, b)
    # UPDATE tanpa filter customer (bug aplikasi disimulasikan): hanya baris
    # A yang tersentuh, baris B TIDAK
    _, n = _as_app(app_uri, a, f"UPDATE {t} SET {col}={col}")
    assert n == len(_owner_rows(w, t, col, a))
    _, n = _as_app(app_uri, a, f"UPDATE {t} SET {col}={col} WHERE {col}=%s",
                   (b,))
    assert n == 0
    if t != "customers":     # customers: DELETE milik sendiri tak diuji
        _, n = _as_app(app_uri, a, f"DELETE FROM {t} WHERE {col}=%s", (b,))
        assert n == 0
    else:
        _, n = _as_app(app_uri, a, "DELETE FROM customers WHERE id=%s", (b,))
        assert n == 0
    assert _owner_rows(w, t, col, b) == b0  # B utuh


@pytest.mark.parametrize("t", [t for t, c in TABLES.items()
                               if c == "customer_id"])
def test_b12_manipulasi_customer_id_ditolak_postgresql(env, tmp_path, t):
    """Memindahkan baris A ke customer B (UPDATE customer_id) ditolak
    WITH CHECK PostgreSQL, bukan oleh aplikasi."""
    w = B11._two(env, tmp_path)
    _ensure_rows(w)
    a, b = w["A"]["customer_id"], w["B"]["customer_id"]
    a0 = _owner_rows(w, t, "customer_id", a)
    with pytest.raises(Exception) as ei:
        _as_app(env[1], a, f"UPDATE {t} SET customer_id=%s WHERE"
                           " customer_id=%s", (b, a))
    assert _ditolak_rls(ei.value), repr(ei.value)
    assert _owner_rows(w, t, "customer_id", a) == a0


@pytest.mark.parametrize("t,sql", [
    ("audit_logs", "INSERT INTO audit_logs (customer_id, action) VALUES"
                   " (%(b)s, 'palsu')"),
    ("mt5_connect_jobs",
     "INSERT INTO mt5_connect_jobs (id, secret_hash, kind, state,"
     " customer_id, deadline_at, created_at, updated_at) VALUES"
     " ('palsu-b12','x','bind','pending', %(b)s, 0, 'a', 'a')"),
    ("auth_tokens",
     "INSERT INTO auth_tokens (user_id, customer_id, token_hash, expires_at)"
     " SELECT u.id, %(b)s, 'palsu-b12', '2099' FROM users u"
     " WHERE u.customer_id = %(a)s"),
    ("customers", "INSERT INTO customers (code) VALUES ('C777')"),
])
def test_b12_insert_untuk_customer_lain_ditolak_postgresql(env, tmp_path, t,
                                                           sql):
    w = B11._two(env, tmp_path)
    _ensure_rows(w)
    a, b = w["A"]["customer_id"], w["B"]["customer_id"]
    before = B2._q(w["owner"], f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
    with pytest.raises(Exception) as ei:
        _as_app(env[1], a, sql, {"a": a, "b": b})
    assert _ditolak_rls(ei.value), repr(ei.value)
    assert B2._q(w["owner"], f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] == \
        before


def test_b12_tanpa_scope_default_deny_dan_system_scope(env, tmp_path):
    w = B11._two(env, tmp_path)
    _ensure_rows(w)
    a, b = int(w["A"]["customer_id"]), int(w["B"]["customer_id"])
    for t, col in TABLES.items():
        rows, _ = _as_app(env[1], None, f"SELECT * FROM {t}")
        assert rows == [], t                        # tanpa scope: kosong
        _, n = _as_app(env[1], None, f"UPDATE {t} SET {col}={col}")
        assert n == 0, t
        # B12.1: app.scope='system' di role APLIKASI = upaya pemalsuan ->
        # tetap tidak melihat apa pun (GUC tidak lagi dibaca policy)
        rows, _ = _as_app(env[1], "system", f"SELECT {col} AS c FROM {t}")
        assert rows == [], t
        # scope sistem = ROLE SISTEM (koneksi terpisah): lintas customer
        rows, _ = _as_app(_sys_uri(env[1]), None,
                          f"SELECT {col} AS c FROM {t}")
        assert {a, b} <= {int(r["c"]) for r in rows if r["c"] is not None}
        # role sistem + customer context: DIBATASI ke customer itu
        rows, _ = _as_app(_sys_uri(env[1]), a,
                          f"SELECT {col} AS c FROM {t}")
        assert {int(r["c"]) for r in rows} == {a}, t
        # customer + app.scope='system' di role aplikasi: hanya customer
        rows, _ = _as_app(env[1], (a, "system"),
                          f"SELECT {col} AS c FROM {t}")
        assert {int(r["c"]) for r in rows} == {a}, t
    with pytest.raises(Exception) as ei:
        _as_app(env[1], None, "INSERT INTO audit_logs (customer_id, action)"
                              " VALUES (%s, 'x')", (a,))
    assert _ditolak_rls(ei.value)


def test_b12_api_token_a_hanya_data_a(env, tmp_path):
    """Jalur API nyata: request ber-token A di-scope customer A oleh
    require_customer; data B tidak bocor ke respons."""
    w = B11._two(env, tmp_path)
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])   # B12.1: pemanggil tepercaya = role sistem
        import auth as authmod
        uid = conn.execute("SELECT id FROM users WHERE customer_id=?",
                           (w["A"]["customer_id"],)).fetchone()["id"]
        tok = authmod.issue_token(conn, w["cfg"], user_id=int(uid),
                                  customer_id=int(w["A"]["customer_id"]),
                                  ip="203.0.113.41")
        dbmod.close_db()
    with w["app"].test_client() as c:
        h = {"Authorization": f"Bearer {tok}"}
        me = c.get("/api/v1/auth/me", headers=h)
        acc = c.get("/api/v1/accounts", headers=h)
        mt5 = c.get("/api/v1/mt5/account", headers=h)
    assert (me.status_code, acc.status_code, mt5.status_code) == \
        (200, 200, 200)
    assert int(mt5.get_json()["customer_id"]) == int(w["A"]["customer_id"])
    blob = "".join(r.get_data(as_text=True) for r in (me, acc, mt5))
    assert w["B"]["code"] not in blob
    assert "91000002" not in blob                 # login MT5 milik B


def test_b12_remove_terminal_scope_customer_lain_tidak_sukses_palsu(
        env, tmp_path):
    """Pemanggil ber-scope customer A meminta hapus terminal B atas nama A:
    RLS menyembunyikan B, tetapi hasilnya BUKAN 'sudah tidak ada' (True)
    — mapping dibaca di scope sistem -> False, B utuh."""
    w = B11._two(env, tmp_path)
    b0 = B11._snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    with w["app"].app_context():
        conn = dbmod.get_db(w["cfg"])
        dbmod.set_customer_context(conn, int(w["A"]["customer_id"]))
        r = w["coord"].remove_terminal(conn, int(w["B"]["id"]),
                                       w["B"]["provision_id"],
                                       customer_id=w["A"]["customer_id"])
        assert conn.current_customer_context() == int(w["A"]["customer_id"])
        dbmod.close_db()
    assert r is False
    assert B11._snap(w, w["B"]) == b0
    assert "remove" not in w["bridge"].calls[calls0:]


# --------------------------------- migrasi 007/008 + rollback berantai
def _rls_state(owner_uri):
    tabs = list(TABLES) + ["users", "sync_logs"]
    rows = B2._q(owner_uri, "SELECT relname, relrowsecurity FROM pg_class"
                            " WHERE relname = ANY(%s) AND relkind='r'",
                 (tabs,))
    pol = {(r["tablename"], r["policyname"]) for r in B2._q(
        owner_uri, "SELECT tablename, policyname FROM pg_policies WHERE"
                   " schemaname='public'")}
    fns = {r["proname"] for r in B2._q(
        owner_uri, "SELECT proname FROM pg_proc WHERE proname LIKE"
                   " 'app_%%allows'")}
    ver = {r["version"] for r in B2._q(owner_uri, "SELECT version FROM"
                                                  " schema_migrations")}
    return {r["relname"]: r["relrowsecurity"] for r in rows}, pol, fns, ver


def _run_owner_sql(owner_uri, path):
    c = psycopg2.connect(owner_uri)
    c.autocommit = True
    try:
        c.cursor().execute(path.read_text(encoding="utf-8"))
    finally:
        c.close()


def test_b12_migrasi_rollback_berantai_008_007_lalu_ulang(env, tmp_path):
    """B12.1: 008 aktif -> rollback 008 (= keadaan B12/007) -> rollback 007
    (= keadaan pra-007) -> migrate() menerapkan [7, 8] lagi. Data utuh di
    setiap langkah; RLS 001/002 tidak pernah tersentuh."""
    owner_uri, app_uri = env
    B2._q(owner_uri, "INSERT INTO customers (code) VALUES ('C501'),"
                     " ('C502') RETURNING id")
    n0 = B2._q(owner_uri, "SELECT COUNT(*) AS n FROM customers")[0]["n"]
    old = {(t, t + "_customer_isolation") for t in (
        "mt5_deals", "mt5_orders", "mt5_trades", "mt5_symbols",
        "mt5_sync_state", "mt5_credentials")}
    tabs7 = list(TABLES) + ["users"]

    def cek_008():
        rls, pol, fns, ver = _rls_state(owner_uri)
        assert all(rls[t] for t in tabs7 + ["sync_logs"])
        assert {(t, t + "_app") for t in tabs7} <= pol
        assert {(t, t + "_system") for t in tabs7} <= pol
        assert not any(p.endswith("_tenant_isolation") for _, p in pol)
        assert fns == {"app_customer_allows", "app_system_allows"}
        assert {7, 8} <= ver and old <= pol
        assert _as_app(app_uri, None, "SELECT id FROM customers")[0] == []
        assert _as_app(app_uri, "system",
                       "SELECT id FROM customers")[0] == []
    cek_008()
    # ---- rollback 008 -> keadaan 007 (B12)
    _run_owner_sql(owner_uri, DOWN8)
    rls, pol, fns, ver = _rls_state(owner_uri)
    assert all(rls[t] for t in TABLES) and not rls["users"] \
        and not rls["sync_logs"]
    assert {(t, t + "_tenant_isolation") for t in TABLES} <= pol
    assert not any(p.endswith(("_app", "_system")) for _, p in pol)
    assert fns == {"app_rls_allows"} and 8 not in ver and 7 in ver
    assert old <= pol
    # keadaan B12 terpulihkan apa adanya — termasuk kelemahan app.scope
    # yang diperbaiki B12.1 (bukti bahwa rollback = kembali ke B12)
    assert len(_as_app(app_uri, "system",
                       "SELECT id FROM customers")[0]) == n0
    # ---- rollback 007 -> keadaan pra-007
    _run_owner_sql(owner_uri, DOWN)
    rls, pol, fns, ver = _rls_state(owner_uri)
    assert not any(rls[t] for t in TABLES) and fns == set()
    assert {7, 8}.isdisjoint(ver) and old <= pol
    assert len(_as_app(app_uri, None, "SELECT id FROM customers")[0]) == n0
    assert B2._q(owner_uri, "SELECT COUNT(*) AS n FROM customers")[0]["n"] \
        == n0
    # ---- terapkan ulang (idempoten)
    owner = dbmod.connect_postgres(owner_uri)
    assert dbmod.migrate(owner) == [7, 8]
    assert dbmod.migrate(owner) == []
    owner.close()
    cek_008()
    assert B2._q(owner_uri, "SELECT COUNT(*) AS n FROM customers")[0]["n"] \
        == n0


def test_b12_role_aplikasi_tidak_dapat_mematikan_rls(env):
    _, app_uri = env
    for uri in (app_uri, _sys_uri(app_uri)):
        for sql in ("ALTER TABLE mt5_terminals DISABLE ROW LEVEL SECURITY",
                    "DROP POLICY mt5_terminals_app ON mt5_terminals",
                    "DROP POLICY mt5_terminals_system ON mt5_terminals",
                    "CREATE OR REPLACE FUNCTION public.app_customer_allows("
                    "bigint) RETURNS boolean LANGUAGE sql AS 'SELECT true'",
                    "CREATE OR REPLACE FUNCTION public.app_system_allows("
                    "bigint) RETURNS boolean LANGUAGE sql AS 'SELECT true'"):
            with pytest.raises(psycopg2.Error):
                _as_app(uri, None, sql)


# ============================================ A. pemulihan tahap 2
def _pending_with_job(w, T, jid, **kw):
    B11._pending(w, T)
    kw.setdefault("customer_id", T["customer_id"])
    kw.setdefault("terminal", T)
    B11._forge_job(w, jid, **kw)


def _stage2_ditolak(w, T, caplog, calls0, alasan):
    assert "remove" not in w["bridge"].calls[calls0:]
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (T["id"],)) != []
    assert H.phase_of(H.paths(w["hcfg"], T["code"])) != "absent"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("pemulihan DITOLAK" in m and alasan in m for m in msgs), msgs
    B11._tanpa_error_kode(caplog)


@pytest.mark.parametrize("kasus", ["provision_lain", "provision_null"])
def test_b12_tahap2_job_provision_tidak_cocok_ditolak(env, tmp_path, caplog,
                                                     kasus):
    w = B11._two(env, tmp_path)
    _pending_with_job(w, w["A"], "t2-prov")
    B2._q(w["owner"], "UPDATE mt5_connect_jobs SET provision_id=%s WHERE"
                      " id='t2-prov' RETURNING id",
          ("e" * 32 if kasus == "provision_lain" else None,))
    a0, b0 = B11._snap(w, w["A"]), B11._snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    assert B11._retry(w) == 0
    assert B11._snap(w, w["A"]) == a0 and B11._snap(w, w["B"]) == b0
    _stage2_ditolak(w, w["A"], caplog, calls0, "provision_id berbeda")


def test_b12_tahap2_job_membawa_provision_terminal_lain_ditolak(
        env, tmp_path, caplog):
    """Job customer A membawa provision_id terminal A tetapi terminal_id
    lain: mapping korup -> ditolak."""
    w = B11._two(env, tmp_path)
    B11._pending(w, w["A"])
    B11._forge_job(w, "t2-tid", customer_id=w["A"]["customer_id"],
                   terminal={"id": 987654,
                             "provision_id": w["A"]["provision_id"]})
    a0, b0 = B11._snap(w, w["A"]), B11._snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    assert B11._retry(w) == 0
    assert B11._snap(w, w["A"]) == a0 and B11._snap(w, w["B"]) == b0
    _stage2_ditolak(w, w["A"], caplog, calls0, "terminal_id berbeda")


def test_b12_tahap2_job_customer_lain_tak_terlihat_customer_tetap_ditolak(
        env, tmp_path, caplog):
    """Job korup milik customer B merujuk terminal A. Di scope customer A
    job itu TERSEMBUNYI RLS — validasi tahap 2 harus tetap melihatnya
    (scope sistem) dan menolak."""
    w = B11._two(env, tmp_path)
    _pending_with_job(w, w["A"], "t2-b", customer_id=w["B"]["customer_id"])
    rows, _ = _as_app(env[1], int(w["A"]["customer_id"]),
                      "SELECT id FROM mt5_connect_jobs WHERE id='t2-b'")
    assert rows == []                              # memang tersembunyi
    a0, b0 = B11._snap(w, w["A"]), B11._snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    assert B11._retry(w) == 0
    assert B11._snap(w, w["A"]) == a0 and B11._snap(w, w["B"]) == b0
    _stage2_ditolak(w, w["A"], caplog, calls0, "customer berbeda")


def test_b12_tahap2_mapping_sah_dipulihkan(env, tmp_path, caplog):
    w = B11._two(env, tmp_path)
    _pending_with_job(w, w["A"], "t2-sah")
    b0 = B11._snap(w, w["B"])
    assert B11._retry(w) == 1
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == []
    assert H.phase_of(H.paths(w["hcfg"], w["A"]["code"])) == "absent"
    assert B11._snap(w, w["B"]) == b0
    assert not any("DITOLAK" in r.getMessage() for r in caplog.records)
    B11._tanpa_error_kode(caplog)


def test_b12_tahap2_job_korup_muncul_setelah_validasi_klaim_atomik_menolak(
        env, tmp_path, monkeypatch, caplog):
    w = B11._two(env, tmp_path)
    B11._pending(w, w["A"])
    a0 = B11._snap(w, w["A"])
    asli = PV.ConnectCoordinator._recovery_mapping_problem

    def balap(conn, r):
        hasil = asli(conn, r)          # lolos validasi ...
        B11._forge_job(w, "t2-balap", customer_id=w["B"]["customer_id"],
                       terminal=w["A"])   # ... lalu job korup muncul
        return hasil
    monkeypatch.setattr(PV.ConnectCoordinator, "_recovery_mapping_problem",
                        staticmethod(balap))
    calls0 = len(w["bridge"].calls)
    assert B11._retry(w) == 0
    assert B11._snap(w, w["A"]) == a0
    assert "remove" not in w["bridge"].calls[calls0:]
    assert any("klaim pemulihan ditolak" in r.getMessage()
               for r in caplog.records)
    B11._tanpa_error_kode(caplog)


def test_b12_tahap2_mapping_berubah_setelah_klaim_dikembalikan(
        env, tmp_path, monkeypatch, caplog):
    """Klaim sah, lalu customer terminal diubah sebelum remove: remove
    menolak (mismatch), klaim DIKEMBALIKAN persis (lifecycle, op_owner,
    last_error); helper tidak dipanggil; akun & kredensial utuh."""
    w = B11._two(env, tmp_path)
    B11._pending(w, w["A"], why="retry:2:uji")
    c3 = B2._q(w["owner"], "INSERT INTO customers (code) VALUES ('C097')"
                           " RETURNING id")[0]["id"]
    t0 = B2._q(w["owner"], "SELECT lifecycle, op_owner, last_error FROM"
                           " mt5_terminals WHERE id=%s", (w["A"]["id"],))
    acc0 = B2._q(w["owner"], "SELECT a.id, a.state, k.enc_password FROM"
                             " mt5_accounts a LEFT JOIN mt5_credentials k ON"
                             " k.account_id=a.id WHERE a.customer_id=%s",
                 (w["A"]["customer_id"],))
    b0 = B11._snap(w, w["B"])
    asli = w["coord"].remove_terminal

    def pindah_lalu_remove(*a, **kw):
        B2._q(w["owner"], "UPDATE mt5_terminals SET customer_id=%s WHERE"
                          " id=%s RETURNING id", (c3, w["A"]["id"]))
        return asli(*a, **kw)
    monkeypatch.setattr(w["coord"], "remove_terminal", pindah_lalu_remove)
    calls0 = len(w["bridge"].calls)
    assert B11._retry(w) == 0
    assert "remove" not in w["bridge"].calls[calls0:]
    assert B2._q(w["owner"], "SELECT lifecycle, op_owner, last_error FROM"
                             " mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == t0
    assert H.phase_of(H.paths(w["hcfg"], w["A"]["code"])) != "absent"
    assert B2._q(w["owner"], "SELECT a.id, a.state, k.enc_password FROM"
                             " mt5_accounts a LEFT JOIN mt5_credentials k ON"
                             " k.account_id=a.id WHERE a.customer_id=%s",
                 (w["A"]["customer_id"],)) == acc0
    assert B11._snap(w, w["B"]) == b0
    assert any("dibatalkan setelah klaim" in r.getMessage()
               for r in caplog.records)
    B11._tanpa_error_kode(caplog)


# ------------------------------------- audit tetap tersimpan di bawah RLS
def test_b12_audit_connect_gagal_tetap_tersimpan(env, caplog):
    """Self-link gagal (password salah): customer baru dibersihkan, audit
    tanpa customer ditulis di scope SISTEM — tidak ditolak RLS dan tidak
    hilang diam-diam (audit() menelan galat)."""
    owner_uri, app_uri = env
    from tests.test_remediation_pr5 import _post
    cfg = PG._cfg(app_uri)
    app = PG._app(cfg)
    r = B2._final(app, _post(app, 92000001, password="salah",
                             ip="203.0.113.77"))
    assert r.status_code in (401, 403), r.get_json()
    rows = B2._q(owner_uri, "SELECT action, customer_id, detail FROM"
                            " audit_logs WHERE action LIKE 'mt5_connect%%'")
    assert any(x["action"] == "mt5_connect_job" and x["customer_id"] is None
               and x["detail"].get("state") == "failed" for x in rows), rows
    assert not any("gagal menulis audit_logs" in rec.getMessage()
                   for rec in caplog.records)
    assert B2._q(owner_uri, "SELECT COUNT(*) AS n FROM customers")[0]["n"] \
        == 0                                     # customer baru dibersihkan


def test_b12_bind_akun_milik_customer_lain_ditolak_tanpa_efek_samping(
        env, tmp_path):
    """Customer C (portal, ber-token) mencoba bind akun MT5 milik B.
    Cek keberadaan lintas customer berjalan di scope sistem -> 409 SEBELUM
    alokasi terminal; tidak ada terminal/akun untuk C; B utuh."""
    w = B11._two(env, tmp_path)
    b0 = B11._snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    seq = "SELECT last_value FROM mt5_terminals_id_seq"
    s0 = B2._q(w["owner"], seq)
    with w["app"].test_client() as c:
        h = {"X-Real-IP": "203.0.113.90"}
        r = c.post("/api/v1/auth/register", headers=h,
                   json={"email": "c-b12@contoh.test",
                         "password": "uji-rahasia-b12"})
        assert r.status_code in (200, 201), r.get_json()
        tok = c.post("/api/v1/auth/login", headers=h,
                     json={"email": "c-b12@contoh.test",
                           "password": "uji-rahasia-b12"}).get_json()["token"]
        r = c.post("/api/v1/accounts/bind",
                   headers={**h, "Authorization": f"Bearer {tok}"},
                   json={"login": 91000002, "server": PG.SRV,
                         "password": "good"})
    assert r.status_code == 409
    assert "customer lain" in str(r.get_json())
    cid = B2._q(w["owner"], "SELECT customer_id FROM users WHERE"
                            " email=%s", ("c-b12@contoh.test",))[0][
        "customer_id"]
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE"
                             " customer_id=%s", (cid,)) == []
    assert B2._q(w["owner"], "SELECT id FROM mt5_accounts WHERE"
                             " customer_id=%s", (cid,)) == []
    assert w["bridge"].calls[calls0:] == []
    assert B2._q(w["owner"], seq) == s0      # tak ada INSERT terminal sesaat
    assert B11._snap(w, w["B"]) == b0
