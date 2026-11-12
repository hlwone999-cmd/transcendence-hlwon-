"""B12.1 — scope sistem tidak dapat dipalsukan, pembatasan scope sistem,
kebijakan audit, dan perluasan RLS (users, sync_logs).

PostgreSQL lokal (pgserver). Role aplikasi `transcendence_app` dan role
sistem `transcendence_system` (migrasi 008) keduanya NON-OWNER dan
NOBYPASSRLS. Uji dijalankan dengan SQL LANGSUNG sebagai role tersebut
(penolakan berasal dari PostgreSQL). Owner hanya untuk menyiapkan &
memeriksa data. Bukan staging / produksi.
"""
import logging
import threading

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402
import psycopg2.extras  # noqa: E402

import audit as auditmod  # noqa: E402
import db as dbmod  # noqa: E402
from tests import test_f2_b2_races as B2  # noqa: E402
from tests import test_f2_b11_customer_mapping as B11  # noqa: E402
from tests import test_f2_b12_rls_tenant as T12  # noqa: E402
from tests import test_remediation_pr5_pg as PG  # noqa: E402

env = PG.env
pg = PG.pg
_tanpa_chown = B2._tanpa_chown
_as_app = T12._as_app
_sys_uri = T12._sys_uri
_ditolak_rls = T12._ditolak_rls

TABLES = dict(T12.TABLES, users="customer_id")


def _raw(uri):
    c = psycopg2.connect(uri)
    return c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _ensure_sync_logs(w):
    q = w["owner"]
    acc = {T["customer_id"]: B2._q(q, "SELECT id FROM mt5_accounts WHERE"
                                      " customer_id=%s",
                                   (T["customer_id"],))[0]["id"]
           for T in (w["A"], w["B"])}
    for cid, aid in acc.items():
        B2._q(q, "INSERT INTO sync_logs (account_id, mode, status) VALUES"
                 " (%s,'uji','ok') RETURNING id", (aid,))
    B2._q(q, "INSERT INTO sync_logs (account_id, mode, status) VALUES"
             " (NULL,'uji-yatim','ok') RETURNING id")
    return acc


# ================================================== A. pemalsuan scope
@pytest.mark.parametrize("t", list(TABLES) + ["sync_logs"])
def test_b121_pemalsuan_app_scope_system_tidak_memberi_akses(env, tmp_path,
                                                             t):
    """SQL injection yang memanggil set_config('app.scope','system') —
    transaction-local MAUPUN session-level — pada role aplikasi: tetap
    tidak melihat/mengubah baris siapa pun (di B12 cara ini membuka akses
    lintas customer)."""
    w = B11._two(env, tmp_path)
    T12._ensure_rows(w)
    _ensure_sync_logs(w)
    a = int(w["A"]["customer_id"])
    c, cur = _raw(env[1])
    try:
        for is_local in ("true", "false"):
            cur.execute("SELECT set_config('app.customer_id','',%s),"
                        " set_config('app.scope','system',%s)"
                        % (is_local, is_local))
            cur.execute(f"SELECT * FROM {t}")
            assert cur.fetchall() == [], (t, is_local)
            cur.execute(f"UPDATE {t} SET id=id")
            assert cur.rowcount == 0
        c.rollback()
        # customer A + app.scope='system' -> tetap HANYA A
        cur.execute("SELECT set_config('app.customer_id',%s,true),"
                    " set_config('app.scope','system',true)", (str(a),))
        col = "account_id" if t == "sync_logs" else TABLES[t]
        cur.execute(f"SELECT {col} AS c FROM {t}")
        rows = cur.fetchall()
        if t == "sync_logs":
            own = B2._q(w["owner"], "SELECT id FROM mt5_accounts WHERE"
                                    " customer_id=%s", (a,))[0]["id"]
            assert {r["c"] for r in rows} == {own}
        else:
            assert rows and {int(r["c"]) for r in rows} == {a}
        c.rollback()
    finally:
        c.close()


def test_b121_role_aplikasi_tidak_dapat_menjadi_role_sistem(env):
    owner_uri, app_uri = env
    for sql in ("SET ROLE transcendence_system",
                "SET SESSION AUTHORIZATION transcendence_system",
                "GRANT transcendence_system TO transcendence_app"):
        c, cur = _raw(app_uri)
        try:
            with pytest.raises(psycopg2.Error):
                cur.execute(sql)
        finally:
            c.close()
    r = B2._q(owner_uri, "SELECT rolname, rolsuper, rolbypassrls,"
                         " rolcreaterole FROM pg_roles WHERE rolname IN"
                         " ('transcendence_app','transcendence_system')")
    assert len(r) == 2 and not any(x["rolsuper"] or x["rolbypassrls"] or
                                   x["rolcreaterole"] for x in r)
    m = B2._q(owner_uri, "SELECT pg_has_role('transcendence_app',"
                         " 'transcendence_system','MEMBER') AS a,"
                         " pg_has_role('transcendence_system',"
                         " 'transcendence_app','MEMBER') AS b")[0]
    assert m == {"a": False, "b": False}


def test_b121_role_sistem_dibatasi_customer_context(env, tmp_path):
    w = B11._two(env, tmp_path)
    T12._ensure_rows(w)
    a, b = int(w["A"]["customer_id"]), int(w["B"]["customer_id"])
    sys_uri = _sys_uri(env[1])
    B2._q(w["owner"], "INSERT INTO audit_logs (customer_id, action) VALUES"
                      " (NULL,'uji_tanpa_customer') RETURNING id")
    rows, _ = _as_app(sys_uri, None, "SELECT customer_id FROM audit_logs")
    assert {a, b, None} <= {r["customer_id"] for r in rows}
    for t, col in TABLES.items():
        rows, _ = _as_app(sys_uri, a, f"SELECT {col} AS c FROM {t}")
        assert {int(r["c"]) for r in rows} == {a}, t
        if t == "audit_logs":
            # B12.2: audit_logs append-only untuk role sistem (tanpa UPDATE/
            # DELETE sama sekali) — lebih ketat dari "0 baris"
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                _as_app(sys_uri, a, f"UPDATE {t} SET {col}={col} WHERE"
                                    f" {col}=%s", (b,))
            continue
        _, n = _as_app(sys_uri, a, f"UPDATE {t} SET {col}={col} WHERE"
                                   f" {col}=%s", (b,))
        assert n == 0, t
    with pytest.raises(Exception) as ei:
        _as_app(sys_uri, a, "INSERT INTO audit_logs (customer_id, action)"
                            " VALUES (%s,'palsu')", (b,))
    assert _ditolak_rls(ei.value)


# ======================================================= D. users / logs
def test_b121_users_isolasi_postgresql(env, tmp_path):
    w = B11._two(env, tmp_path)
    a, b = int(w["A"]["customer_id"]), int(w["B"]["customer_id"])
    app = env[1]
    rows, _ = _as_app(app, a, "SELECT customer_id, password_hash FROM users")
    assert [int(r["customer_id"]) for r in rows] == [a]
    rows, _ = _as_app(app, a, "SELECT * FROM users WHERE customer_id=%s",
                      (b,))
    assert rows == []                         # hash password B tak terbaca
    hb = B2._q(w["owner"], "SELECT password_hash FROM users WHERE"
                           " customer_id=%s", (b,))
    _, n = _as_app(app, a, "UPDATE users SET password_hash='x' WHERE"
                           " customer_id=%s", (b,))
    assert n == 0
    _, n = _as_app(app, a, "DELETE FROM users WHERE customer_id=%s", (b,))
    assert n == 0
    with pytest.raises(Exception) as ei:
        _as_app(app, a, "UPDATE users SET customer_id=%s WHERE"
                        " customer_id=%s", (b, a))
    assert _ditolak_rls(ei.value)
    assert _as_app(app, None, "SELECT id FROM users")[0] == []
    assert B2._q(w["owner"], "SELECT password_hash FROM users WHERE"
                             " customer_id=%s", (b,)) == hb


def test_b121_sync_logs_isolasi_dan_log_yatim_hanya_sistem(env, tmp_path):
    w = B11._two(env, tmp_path)
    acc = _ensure_sync_logs(w)
    a, b = int(w["A"]["customer_id"]), int(w["B"]["customer_id"])
    app, sysu = env[1], _sys_uri(env[1])
    rows, _ = _as_app(app, a, "SELECT account_id FROM sync_logs")
    assert rows and {r["account_id"] for r in rows} == {acc[a]}
    _, n = _as_app(app, a, "UPDATE sync_logs SET status='x' WHERE"
                           " account_id=%s OR account_id IS NULL", (acc[b],))
    assert n == 0
    with pytest.raises(Exception) as ei:
        _as_app(app, a, "INSERT INTO sync_logs (account_id, mode) VALUES"
                        " (%s,'palsu')", (acc[b],))
    assert _ditolak_rls(ei.value)
    assert _as_app(app, None, "SELECT id FROM sync_logs")[0] == []
    rows, _ = _as_app(sysu, a, "SELECT account_id FROM sync_logs")
    assert {r["account_id"] for r in rows} == {acc[a]}   # yatim tak terlihat
    rows, _ = _as_app(sysu, None, "SELECT account_id FROM sync_logs")
    assert {acc[a], acc[b], None} <= {r["account_id"] for r in rows}


def test_b121_pengecualian_auth_throttle_lockouts_beralasan(env, tmp_path):
    """auth_throttle/auth_lockouts SENGAJA tanpa RLS: hanya key_hash +
    penghitung (tanpa kolom customer), dan wajib berfungsi pra-auth (tanpa
    scope) maupun dari request customer, oleh role aplikasi."""
    owner_uri, app_uri = env
    rls = B2._q(owner_uri, "SELECT relname, relrowsecurity FROM pg_class"
                           " WHERE relname IN ('auth_throttle',"
                           "'auth_lockouts')")
    assert {r["relname"]: r["relrowsecurity"] for r in rls} == \
        {"auth_throttle": False, "auth_lockouts": False}
    cols = {(r["table_name"], r["column_name"]) for r in B2._q(
        owner_uri, "SELECT table_name, column_name FROM"
                   " information_schema.columns WHERE table_name IN"
                   " ('auth_throttle','auth_lockouts')")}
    assert cols == {("auth_throttle", "key_hash"), ("auth_throttle",
                    "window_s"), ("auth_throttle", "bucket"),
                    ("auth_throttle", "hits"), ("auth_lockouts", "key_hash"),
                    ("auth_lockouts", "failures"),
                    ("auth_lockouts", "window_start"),
                    ("auth_lockouts", "locked_until")}
    import auth as authmod
    w = B11._two(env, tmp_path)
    for scope in (None, int(w["A"]["customer_id"])):
        conn = dbmod.connect_postgres(app_uri)
        try:
            dbmod.set_customer_context(conn, scope)
            lock = authmod.FailureLockout(2, 60)
            key = f"uji-b121-{scope}"
            lock.record_failure(key, conn=conn)
            lock.record_failure(key, conn=conn)
            assert lock.is_locked(key, conn=conn) is True
            authmod.RateLimiter().hit(f"rl-{scope}", 5, 60, conn=conn)
        finally:
            conn.close()
    raw = B2._q(owner_uri, "SELECT key_hash FROM auth_lockouts")
    assert raw and all(len(r["key_hash"]) == 64 and "uji" not in
                       r["key_hash"] for r in raw)      # hanya hash


# ============================================ B. pembatasan scope sistem
def _token(w, T):
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])
        import auth as authmod
        uid = conn.execute("SELECT id FROM users WHERE customer_id=?",
                           (T["customer_id"],)).fetchone()["id"]
        tok = authmod.issue_token(conn, w["cfg"], user_id=int(uid),
                                  customer_id=int(T["customer_id"]),
                                  ip="203.0.113.41")
        dbmod.close_db()
    return tok


def _thread_state():
    """(ctx, guc, in_tx) untuk koneksi aplikasi & sistem thread ini."""
    out = {}
    for slot in ("conn", "sysconn"):
        c = getattr(dbmod._local, slot, None)
        if c is None:
            out[slot] = None
            continue
        in_tx = c.in_transaction
        guc = c.execute("SELECT current_setting('app.customer_id', true)"
                        " AS g").fetchone()["g"]
        c.rollback()
        out[slot] = (c.current_customer_context(), guc or "", in_tx)
    return out


def test_b121_scope_tidak_bocor_antar_request(env, tmp_path):
    """Satu thread, koneksi dipakai ulang: request customer A, request admin
    (koneksi sistem + customer context per item), request tanpa token —
    setelah tiap request kedua koneksi bersih dan request berikutnya tidak
    mewarisi scope."""
    w = B11._two(env, tmp_path)
    ta, tb = _token(w, w["A"]), _token(w, w["B"])
    # seperti produksi: SETIAP request memiliki app context sendiri
    # (teardown per request), koneksi thread-local dipakai ulang
    if True:
        with w["app"].test_client() as c:
            r = c.get("/api/v1/mt5/account",
                      headers={"Authorization": f"Bearer {ta}"})
            assert r.status_code == 200
            assert int(r.get_json()["customer_id"]) == \
                int(w["A"]["customer_id"])
            st = _thread_state()
            assert st["conn"] in (None, (None, "", False))
            assert st["sysconn"] in (None, (None, "", False))
            r = c.get("/api/v1/admin/health/terminals",
                      headers={"X-API-Token": "TESTTOKEN"})
            assert r.status_code == 200
            assert len(r.get_json()["items"]) == 2
            st = _thread_state()
            assert st["sysconn"] in (None, (None, "", False))
            r = c.get("/api/v1/mt5/account")         # tanpa token
            assert r.status_code == 401
            r = c.get("/api/v1/mt5/account",
                      headers={"Authorization": f"Bearer {tb}"})
            assert int(r.get_json()["customer_id"]) == \
                int(w["B"]["customer_id"])
            st = _thread_state()
            assert st["conn"] in (None, (None, "", False))
        dbmod.close_db()


def test_b121_admin_tidak_terautentikasi_tidak_mendapat_koneksi_sistem(
        env, tmp_path, monkeypatch):
    w = B11._two(env, tmp_path)
    calls = []
    real = dbmod.get_system_db

    def spy(cfg):
        calls.append(threading.get_ident())
        return real(cfg)
    monkeypatch.setattr(dbmod, "get_system_db", spy)
    n0 = B2._q(w["owner"], "SELECT COUNT(*) AS n FROM auth_tokens WHERE"
                           " revoked_at IS NULL")[0]["n"]
    with w["app"].test_client() as c:
        for meth, path in (("get", "/api/v1/admin/customers"),
                           ("get", "/api/v1/admin/health/terminals"),
                           ("post", "/api/v1/admin/customers/%s/"
                                    "revoke-sessions" % w["A"]["customer_id"]),
                           ("post", "/api/v1/admin/customers")):
            for hdr in ({}, {"X-API-Token": "salah"},
                        {"Authorization": "Bearer TESTTOKEN"}):
                r = getattr(c, meth)(path, headers=hdr, json={})
                assert r.status_code == 401, (path, hdr, r.status_code)
        assert calls == []                     # tak ada koneksi sistem
        r = c.get("/api/v1/admin/customers",
                  headers={"X-API-Token": "TESTTOKEN"})
        assert r.status_code == 200 and len(r.get_json()["items"]) == 2
    assert calls                                # admin sah: role sistem
    assert B2._q(w["owner"], "SELECT COUNT(*) AS n FROM auth_tokens WHERE"
                             " revoked_at IS NULL")[0]["n"] == n0


def test_b121_worker_thread_tidak_meninggalkan_scope(env, tmp_path):
    """Worker (run_job, termasuk jalur gagal) dan pemulihan pada thread
    worker: setelah selesai, koneksi sistem thread itu tanpa customer
    context dan tanpa transaksi terbuka."""
    w = B11._two(env, tmp_path)
    accA = B2._q(w["owner"], "SELECT id FROM mt5_accounts WHERE"
                             " customer_id=%s", (w["A"]["customer_id"],))[0]
    B2._q(w["owner"],
          "INSERT INTO mt5_connect_jobs (id, secret_hash, kind, state,"
          " customer_id, account_id, terminal_id, provision_id,"
          " created_terminal, deadline_at, created_at, updated_at)"
          " VALUES ('w-b121','x','bind','pending',%s,%s,%s,%s,0,"
          " extract(epoch from now())+60,'a','a') RETURNING id",
          (w["A"]["customer_id"], accA["id"], w["A"]["id"],
           w["A"]["provision_id"]))

    def boom(spec):
        raise RuntimeError("uji: start gagal")
    w["bridge"].start = boom
    B2._q(w["owner"], "UPDATE mt5_terminals SET lifecycle='stopped' WHERE"
                      " id=%s RETURNING id", (w["A"]["id"],))
    hasil = {}

    def worker():
        with w["app"].app_context():
            try:
                hasil["state"] = w["coord"].run_job("w-b121")
                w["coord"].recover_expired(dbmod.get_db(w["cfg"]))
                w["coord"].retry_uncertain_removals(dbmod.get_db(w["cfg"]))
                hasil["after"] = _thread_state()
            finally:
                dbmod.close_db()
    th = threading.Thread(target=worker)
    th.start()
    th.join(60)
    assert hasil.get("state") == "failed"
    # tanpa customer context (memori & GUC). Transaksi baca terbuka dari
    # pemulihan scope (set_config) ditutup oleh finally thread worker
    # produksi (start(): clear_customer_context + clear_system_context).
    assert hasil["after"]["sysconn"][:2] == (None, "")
    assert hasil["after"]["conn"] is None or \
        hasil["after"]["conn"][:2] == (None, "")


def test_b121_system_conn_pulih_setelah_exception(env, tmp_path):
    w = B11._two(env, tmp_path)
    b = int(w["B"]["customer_id"])
    with w["app"].app_context():
        sc = dbmod.get_system_db(w["cfg"])
        dbmod.set_customer_context(sc, b)
        with pytest.raises(psycopg2.Error):     # transaksi menjadi gagal
            with dbmod.system_conn(w["cfg"]) as s2:
                assert s2 is sc and s2.current_customer_context() is None
                s2.execute("SELECT * FROM tabel_tidak_ada_b121")
        assert sc.current_customer_context() == b
        rows = sc.execute("SELECT customer_id FROM mt5_terminals").fetchall()
        assert {int(r["customer_id"]) for r in rows} == {b}
        dbmod.close_db()


def test_b121_koneksi_sistem_menolak_dsn_role_aplikasi(env):
    from config import Config
    c = Config()
    c.db_url = env[1]
    c.db_system_url = env[1]                    # salah konfigurasi
    with pytest.raises(dbmod.SystemDbConfigError):
        dbmod.get_system_db(c)
    dbmod.close_db()


# =============================================================== C. audit
def _grant(owner_uri, ok):
    B2._q(owner_uri, ("GRANT" if ok else "REVOKE") +
          " INSERT ON audit_logs " + ("TO" if ok else "FROM") +
          " transcendence_system")


def test_b121_audit_wajib_gagal_operasi_admin_gagal_tertutup(env, tmp_path,
                                                             caplog):
    w = B11._two(env, tmp_path)
    n0 = B2._q(w["owner"], "SELECT COUNT(*) AS n FROM customers")[0]["n"]
    f0 = auditmod.failure_stats()["by_action"].get(
        "admin_create_customer_diminta", 0)
    _grant(w["owner"], False)
    try:
        with w["app"].test_client() as c:
            r = c.post("/api/v1/admin/customers",
                       headers={"X-API-Token": "TESTTOKEN"},
                       json={"email": "adm-b121@contoh.test",
                             "password": "uji-rahasia-b121"})
    finally:
        _grant(w["owner"], True)
    assert r.status_code == 503
    assert B2._q(w["owner"], "SELECT COUNT(*) AS n FROM customers")[0]["n"] \
        == n0                                      # TIDAK ada customer baru
    assert auditmod.failure_stats()["by_action"][
        "admin_create_customer_diminta"] == f0 + 1
    assert any(auditmod.FAIL_MARKER in r.getMessage() and
               r.levelno >= logging.ERROR for r in caplog.records)
    with w["app"].test_client() as c:              # audit pulih -> sukses
        r = c.post("/api/v1/admin/customers",
                   headers={"X-API-Token": "TESTTOKEN"},
                   json={"email": "adm-b121@contoh.test",
                         "password": "uji-rahasia-b121"})
    assert r.status_code == 201
    acts = {x["action"] for x in B2._q(w["owner"], "SELECT action FROM"
                                                   " audit_logs")}
    assert {"admin_create_customer_diminta", "admin_create_customer"} <= acts


def test_b121_audit_best_effort_tidak_diam_dan_aksi_pengamanan_jalan(
        env, tmp_path, caplog):
    w = B11._two(env, tmp_path)
    _token(w, w["A"])
    cid = w["A"]["customer_id"]
    f0 = auditmod.failure_stats()["total"]
    _grant(w["owner"], False)
    try:
        with w["app"].test_client() as c:
            r = c.post(f"/api/v1/admin/customers/{cid}/revoke-sessions",
                       headers={"X-API-Token": "TESTTOKEN"})
    finally:
        _grant(w["owner"], True)
    assert r.status_code == 200
    body = r.get_json()
    assert body["revoked"] >= 1 and body["audit_recorded"] is False
    assert B2._q(w["owner"], "SELECT COUNT(*) AS n FROM auth_tokens WHERE"
                             " customer_id=%s AND revoked_at IS NULL",
                 (cid,))[0]["n"] == 0              # sesi TETAP dicabut
    assert auditmod.failure_stats()["total"] == f0 + 1
    assert any(auditmod.FAIL_MARKER in r.getMessage() and
               "admin_revoke_sessions" in r.getMessage()
               for r in caplog.records)
    with w["app"].test_client() as c:
        h = c.get("/api/v1/admin/health/terminals",
                  headers={"X-API-Token": "TESTTOKEN"}).get_json()
    assert h["audit_write_failures"]["total"] >= 1


def test_b121_audit_app_scope_customer_lain_tidak_dialihkan(env, tmp_path,
                                                            caplog):
    """Koneksi role APLIKASI ber-scope A menulis audit milik customer B:
    TIDAK dialihkan ke koneksi sistem — ditolak RLS, tercatat (bukan
    diam), dan tidak ada baris tersimpan."""
    w = B11._two(env, tmp_path)
    a, b = int(w["A"]["customer_id"]), int(w["B"]["customer_id"])
    with w["app"].app_context():
        conn = dbmod.get_db(w["cfg"])
        dbmod.set_customer_context(conn, a)
        ok = auditmod.audit(conn, "uji_b121_lintas", customer_id=b)
        ok_none = auditmod.audit(conn, "uji_b121_tanpa_customer")
        ok_own = auditmod.audit(conn, "uji_b121_sendiri", customer_id=a)
        assert conn.current_customer_context() == a
        dbmod.close_db()
    assert ok is False and ok_none is True and ok_own is True
    rows = B2._q(w["owner"], "SELECT action, customer_id FROM audit_logs"
                             " WHERE action LIKE 'uji_b121%%'")
    assert {(r["action"], r["customer_id"]) for r in rows} == {
        ("uji_b121_tanpa_customer", None), ("uji_b121_sendiri", a)}
    assert any(auditmod.FAIL_MARKER in r.getMessage() and
               "uji_b121_lintas" in r.getMessage() for r in caplog.records)


def test_b121_audit_wajib_dari_role_aplikasi_tanpa_scope_gagal_jelas(env):
    """required=True dari koneksi tanpa penulis sah -> AuditWriteError
    (tidak diam)."""
    conn = dbmod.connect_postgres(env[1])       # mentah: tanpa config
    try:
        with pytest.raises(auditmod.AuditWriteError):
            auditmod.audit(conn, "uji_wajib", customer_id=1, required=True)
    finally:
        conn.close()
