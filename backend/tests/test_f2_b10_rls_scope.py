"""B10 / T3-1: scope RLS pemanggil dipulihkan setelah recover_expired,
cleanup, dan retry_uncertain_removals.

PostgreSQL lokal (pgserver), role aplikasi non-owner `transcendence_app`,
RLS aktif. Bukan staging.
"""
import threading
import time

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg2")

import db as dbmod  # noqa: E402
from services import provisioner as P  # noqa: E402
from services import provisioning as PV  # noqa: E402
from tests import test_remediation_pr5_pg as PG  # noqa: E402

env = PG.env
pg = PG.pg


# ------------------------------------------------------------ pembantu
def _scope(conn):
    """(scope di memori, GUC di transaksi berjalan)."""
    guc = conn.execute(
        "SELECT current_setting('app.customer_id', true) AS v").fetchone()["v"]
    return conn.current_customer_context(), guc


def _cust_of(owner_uri, login):
    return PG._own(owner_uri, "SELECT customer_id FROM mt5_accounts"
                              " WHERE login=%s", (login,))


def _acc_of(owner_uri, login):
    return PG._own(owner_uri, "SELECT id FROM mt5_accounts WHERE login=%s",
                   (login,))


def _world(env_):
    """A dan C terhubung (punya kredensial); B punya job pending yang
    sudah lewat deadline (akan diproses recover_expired)."""
    owner_uri, app_uri = env_
    cfg = PG._cfg(app_uri)
    app = PG._app(cfg)
    assert PG._post(app, 81000001, ip="203.0.113.31").status_code == 201
    assert PG._post(app, 81000003, ip="203.0.113.33").status_code == 201
    coord = app.extensions["mt5_coordinator"]
    real_start = coord.start
    coord.start = lambda job_id: None              # worker job B tak jalan
    assert PG._post(app, 81000002, ip="203.0.113.32").status_code == 202
    coord.start = real_start
    _expire(owner_uri)
    w = {"owner": owner_uri, "cfg": cfg, "app": app, "coord": coord,
         "prov": app.extensions["mt5_provisioner"],
         "A": _cust_of(owner_uri, 81000001),
         "B": _cust_of(owner_uri, 81000002),
         "C": _cust_of(owner_uri, 81000003),
         "accA": _acc_of(owner_uri, 81000001),
         "accC": _acc_of(owner_uri, 81000003)}
    assert len({w["A"], w["B"], w["C"]}) == 3
    return w


def _expire(owner_uri):
    import psycopg2
    c = psycopg2.connect(owner_uri)
    c.autocommit = True
    c.cursor().execute("UPDATE mt5_connect_jobs SET deadline_at=%s WHERE"
                       " state NOT IN ('connected','failed')",
                       (time.time() - 1,))
    c.close()


def _job_b_state(w):
    # akun B dihapus oleh cleanup (dibuat job itu); baris job tetap ada
    return PG._own(w["owner"], "SELECT state FROM mt5_connect_jobs WHERE"
                               " customer_id=%s", (w["B"],))


def _assert_scope_a(w, conn):
    """Scope = A, dan dengan scope itu A tidak dapat membaca/mengubah
    kredensial customer lain (C)."""
    mem, guc = _scope(conn)
    assert mem == w["A"] and guc == str(w["A"]), (mem, guc)
    own = conn.execute("SELECT count(*) AS n FROM mt5_credentials WHERE"
                       " account_id=?", (w["accA"],)).fetchone()["n"]
    other = conn.execute("SELECT count(*) AS n FROM mt5_credentials WHERE"
                         " account_id=?", (w["accC"],)).fetchone()["n"]
    upd = conn.execute("UPDATE mt5_credentials SET key_version=key_version"
                       " WHERE account_id=?", (w["accC"],)).rowcount
    conn.rollback()
    assert own == 1 and other == 0 and upd == 0, (own, other, upd)


def _with_scope_a(w):
    conn = dbmod.get_db(w["cfg"])
    dbmod.set_customer_context(conn, w["A"])
    conn.commit()
    return conn


# ------------------------------------------------------------ 1. normal
def test_b10_recover_job_b_scope_kembali_a(env):
    w = _world(env)
    with w["app"].app_context():
        conn = _with_scope_a(w)
        _assert_scope_a(w, conn)
        assert w["coord"].recover_expired(conn) >= 1
        _assert_scope_a(w, conn)
        dbmod.close_db()
    assert _job_b_state(w) == "failed"              # job B benar diproses


def test_b10_tanpa_scope_tetap_tanpa_scope(env):
    w = _world(env)
    with w["app"].app_context():
        conn = dbmod.get_db(w["cfg"])
        dbmod.set_customer_context(conn, None)
        w["coord"].recover_expired(conn)
        mem, guc = _scope(conn)
        assert mem is None and guc in ("", None)
        n = conn.execute("SELECT count(*) AS n FROM mt5_credentials") \
            .fetchone()["n"]
        assert n == 0                               # default-deny
        dbmod.close_db()


def test_b10_early_return_tanpa_job(env):
    w = _world(env)
    with w["app"].app_context():
        conn = _with_scope_a(w)
        w["coord"].recover_expired(conn)            # memproses B
        _assert_scope_a(w, conn)
        assert w["coord"].recover_expired(conn) == 0   # tak ada lagi
        _assert_scope_a(w, conn)
        dbmod.close_db()


# ------------------------------------------------------------ 2. exception
def test_b10_exception_saat_cleanup_scope_kembali_a(env):
    w = _world(env)

    def boom(spec):
        raise RuntimeError("galat tak terduga (uji)")
    w["prov"].remove = boom
    with w["app"].app_context():
        conn = _with_scope_a(w)
        with pytest.raises(RuntimeError):
            w["coord"].recover_expired(conn)
        _assert_scope_a(w, conn)
        dbmod.close_db()


def test_b10_transaksi_gagal_saat_cleanup_scope_kembali_a(env):
    """Galat SQL membuat transaksi berstatus gagal; pemulihan melakukan
    rollback lalu memasang ulang scope A."""
    w = _world(env)

    def bad_sql(spec):
        # B12.1: pemulihan berjalan di koneksi ROLE SISTEM -> galat SQL
        # disuntikkan ke koneksi itu (koneksi pemanggil tidak dipakai)
        dbmod.get_system_db(w["cfg"]).execute(
            "SELECT * FROM tabel_tidak_ada")
    w["prov"].remove = bad_sql
    with w["app"].app_context():
        conn = _with_scope_a(w)
        try:
            w["coord"].recover_expired(conn)
        except Exception:  # noqa: BLE001
            pass
        _assert_scope_a(w, conn)
        sc = dbmod.get_system_db(w["cfg"])      # koneksi sistem tetap sehat
        assert sc.current_customer_context() is None
        assert sc.execute("SELECT 1 AS x").fetchone()["x"] == 1
        dbmod.close_db()


# ------------------------------------------------------------ 3. timeout
@pytest.mark.parametrize("state", ["helper_running", "unknown",
                                   "stopped_incomplete"])
def test_b10_helper_timeout_scope_kembali_a(env, state):
    w = _world(env)

    def slow(spec):
        raise P.ProvisionTimeout("helper remove timeout", state)
    w["prov"].remove = slow
    with w["app"].app_context():
        conn = _with_scope_a(w)
        w["coord"].recover_expired(conn)
        _assert_scope_a(w, conn)
        dbmod.close_db()


# ------------------------------------------------------------ 4. nested
def test_b10_recovery_bersarang_kembali_ke_level_sebelumnya(env):
    w = _world(env)
    seen = {}
    real = w["prov"].remove

    def nested(spec):
        # B12.1: cleanup job B berjalan di koneksi ROLE SISTEM thread ini
        conn = dbmod.get_system_db(w["cfg"])
        seen["dalam_sebelum"] = conn.current_customer_context()
        if not seen.get("sekali"):
            seen["sekali"] = True
            w["coord"].recover_expired(conn)        # pemanggilan bersarang
            w["coord"].retry_uncertain_removals(conn)
        seen["dalam_sesudah"] = conn.current_customer_context()
        return real(spec)
    w["prov"].remove = nested
    with w["app"].app_context():
        conn = _with_scope_a(w)
        w["coord"].recover_expired(conn)
        _assert_scope_a(w, conn)
        dbmod.close_db()
    # di dalam cleanup job B scope = B; setelah panggilan bersarang
    # kembali ke B (level sebelumnya), bukan A dan bukan None
    assert seen["dalam_sebelum"] == w["B"]
    assert seen["dalam_sesudah"] == w["B"]


def test_b10_retry_uncertain_removals_scope_kembali_a(env):
    w = _world(env)
    with w["app"].app_context():
        conn = _with_scope_a(w)
        w["coord"].retry_uncertain_removals(conn)
        _assert_scope_a(w, conn)
        dbmod.close_db()


# ------------------------------------------------------------ 5. dua koneksi
def test_b10_dua_koneksi_tidak_saling_mencemari(env):
    """Thread A (scope A) terus membaca; thread B (scope C) berulang kali
    menjalankan recovery. Masing-masing koneksi terpisah."""
    w = _world(env)
    errs, stop = [], threading.Event()
    counts = {"A": 0, "B": 0}

    def reader_a():
        try:
            with w["app"].app_context():
                conn = _with_scope_a(w)
                while not stop.is_set():
                    _assert_scope_a(w, conn)
                    counts["A"] += 1
                dbmod.close_db()
        except Exception as e:  # noqa: BLE001
            errs.append(("A", repr(e)))

    def recovery_b():
        try:
            with w["app"].app_context():
                conn = dbmod.get_db(w["cfg"])
                for _ in range(15):
                    dbmod.set_customer_context(conn, w["C"])
                    conn.commit()
                    w["coord"].recover_expired(conn)
                    w["coord"].retry_uncertain_removals(conn)
                    mem, guc = _scope(conn)
                    conn.rollback()
                    if (mem, guc) != (w["C"], str(w["C"])):
                        errs.append(("B", mem, guc))
                    counts["B"] += 1
                dbmod.close_db()
        except Exception as e:  # noqa: BLE001
            errs.append(("B", repr(e)))
    ta = threading.Thread(target=reader_a)
    tb = threading.Thread(target=recovery_b)
    ta.start()
    tb.start()
    tb.join(120)
    stop.set()
    ta.join(30)
    assert errs == []
    assert counts["A"] > 10 and counts["B"] == 15, counts
    assert _job_b_state(w) == "failed"


# ------------------------------------------------------------ 6. pemulihan gagal
def test_b10_pemulihan_gagal_default_deny_dan_koneksi_tidak_dipakai(env,
                                                                    monkeypatch):
    """B12.1: pemulihan memakai koneksi ROLE SISTEM; koneksi itu yang
    scope-nya berganti, jadi jaminan B10/B10.1 diuji di sana: pemulihan
    scope koneksi sistem gagal -> ScopeRestoreError, koneksi sistem
    diamankan (ditutup, slot dilepas), koneksi pemanggil TIDAK disentuh."""
    w = _world(env)
    with w["app"].app_context():
        conn = _with_scope_a(w)
        sc = _sys_prev(w, w["A"])
        _fail_on(monkeypatch, w["A"], system_only=True)
        # operasi BERHENTI dengan galat yang jelas (B10.1) — TIDAK pernah
        # selesai seolah sukses memakai koneksi yang tidak pasti
        with pytest.raises(PV.ScopeRestoreError):
            w["coord"].recover_expired(conn)
        monkeypatch.undo()
        _assert_sys_aman(w, sc)
        _assert_scope_a(w, conn)               # pemanggil utuh (scope A)
        dbmod.close_db()


# ================================================================ B10.1
# Pemulihan scope gagal setelah retry -> koneksi diamankan SEKALI dan
# galat dilempar (tanpa sukses palsu); galat asli body tidak ditutupi.
def _fail_on(monkeypatch, cid_fail, system_only=False):
    """set_customer_context gagal untuk cid tertentu (pemulihan gagal pada
    percobaan pertama DAN ulang). system_only: hanya pada koneksi role
    sistem (B12.1)."""
    real = dbmod.set_customer_context
    calls = {"n": 0}

    def f(conn, cid):
        if cid == cid_fail and (not system_only or
                                getattr(conn, "role_kind", "") == "system"):
            calls["n"] += 1
            raise RuntimeError("pemulihan scope gagal (uji)")
        return real(conn, cid)
    monkeypatch.setattr(dbmod, "set_customer_context", f)
    return real, calls


def _count_close(monkeypatch):
    n = {"close": 0}
    real = dbmod.PostgresConn.close

    def c(self):
        n["close"] += 1
        return real(self)
    monkeypatch.setattr(dbmod.PostgresConn, "close", c)
    return n


def _sys_prev(w, cid):
    """Koneksi sistem thread ini dengan customer context [cid] (keadaan
    'sebelum' yang harus dipulihkan setelah pemulihan)."""
    sc = dbmod.get_system_db(w["cfg"])
    dbmod.set_customer_context(sc, cid)
    sc.commit()
    return sc


def _assert_sys_aman(w, sc):
    """Koneksi sistem lama tak dapat dipakai; get_system_db() memberi
    koneksi BARU tanpa customer context."""
    with pytest.raises(Exception):
        sc.execute("SELECT 1").fetchone()
    fresh = dbmod.get_system_db(w["cfg"])
    assert fresh is not sc
    assert fresh.current_customer_context() is None


def _assert_aman(w, conn):
    """Koneksi lama tak dapat dipakai; get_db() baru tanpa scope."""
    with pytest.raises(Exception):
        conn.execute("SELECT 1").fetchone()
    fresh = dbmod.get_db(w["cfg"])
    assert fresh is not conn
    mem, guc = _scope(fresh)
    assert mem is None and guc in ("", None)
    assert fresh.execute("SELECT count(*) AS n FROM mt5_credentials") \
        .fetchone()["n"] == 0                     # default-deny


def test_b101_body_normal_pemulihan_gagal_wajib_exception(env, monkeypatch):
    w = _world(env)
    with w["app"].app_context():
        conn = _with_scope_a(w)
        closes = _count_close(monkeypatch)
        real, calls = _fail_on(monkeypatch, w["A"])
        done = {"body": False}
        with pytest.raises(PV.ScopeRestoreError):
            with PV._caller_scope(conn):
                real(conn, w["B"])               # body mengganti scope
                done["body"] = True              # body selesai NORMAL
        assert done["body"] is True
        assert calls["n"] == 2                   # percobaan + ulang
        assert closes["close"] == 1              # ditutup TEPAT sekali
        monkeypatch.undo()
        _assert_aman(w, conn)
        dbmod.close_db()


def test_b101_body_exception_tidak_ditutupi(env, monkeypatch):
    w = _world(env)

    class GalatAsli(Exception):
        pass
    with w["app"].app_context():
        conn = _with_scope_a(w)
        closes = _count_close(monkeypatch)
        real, _ = _fail_on(monkeypatch, w["A"])
        with pytest.raises(GalatAsli):           # BUKAN ScopeRestoreError
            with PV._caller_scope(conn):
                real(conn, w["B"])
                raise GalatAsli("galat body")
        assert closes["close"] == 1              # tetap diamankan
        monkeypatch.undo()
        _assert_aman(w, conn)
        dbmod.close_db()


def test_b101_bersarang_gagal_ditutup_sekali_galat_diteruskan(env,
                                                             monkeypatch):
    """Context manager bersarang: dalam gagal memulihkan -> diamankan &
    ScopeRestoreError; luar TIDAK menutup lagi dan meneruskan galat itu."""
    w = _world(env)
    with w["app"].app_context():
        conn = _with_scope_a(w)
        closes = _count_close(monkeypatch)
        real, _ = _fail_on(monkeypatch, w["B"])  # pemulihan ke B gagal
        with pytest.raises(PV.ScopeRestoreError):
            with PV._caller_scope(conn):         # luar: prev A
                real(conn, w["B"])
                with PV._caller_scope(conn):     # dalam: prev B
                    real(conn, w["C"])
        assert closes["close"] == 1
        monkeypatch.undo()
        _assert_aman(w, conn)
        dbmod.close_db()


def test_b101_bersarang_normal_tidak_merusak_scope(env):
    w = _world(env)
    with w["app"].app_context():
        conn = _with_scope_a(w)
        with PV._caller_scope(conn):             # prev A
            dbmod.set_customer_context(conn, w["B"])
            with PV._caller_scope(conn):         # prev B
                dbmod.set_customer_context(conn, w["C"])
                assert _scope(conn) == (w["C"], str(w["C"]))
            assert _scope(conn) == (w["B"], str(w["B"]))
        _assert_scope_a(w, conn)
        dbmod.close_db()


def test_b101_tidak_ada_sukses_palsu_lewat_api(env, monkeypatch):
    """recover_expired / retry_uncertain_removals: pemulihan gagal ->
    selalu galat, tidak pernah mengembalikan hasil."""
    w = _world(env)
    for fn in ("recover_expired", "retry_uncertain_removals"):
        with w["app"].app_context():
            conn = _with_scope_a(w)
            sc = _sys_prev(w, w["A"])            # B12.1: koneksi sistem
            _fail_on(monkeypatch, w["A"], system_only=True)
            got = {}
            with pytest.raises(PV.ScopeRestoreError):
                got["hasil"] = getattr(w["coord"], fn)(conn)
            assert got == {}                     # tak ada nilai kembali
            monkeypatch.undo()
            _assert_sys_aman(w, sc)
            _assert_scope_a(w, conn)             # pemanggil utuh
            dbmod.close_db()
