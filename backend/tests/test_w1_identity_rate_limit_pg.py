"""W1/D1 — atomisitas rate limit identitas + rem global di PostgreSQL
SUNGGUHAN (pgserver), memakai ROLE APLIKASI non-owner (grant migrasi 003).
Dilewati otomatis bila pgserver tidak terpasang."""
import threading

import auth as authmod
import db as dbmod
from tests.test_remediation_pr4_pg import dbs, pg  # noqa: F401  (fixture)


def _race(app_uri, n, checks):
    res = []
    barrier = threading.Barrier(n)

    def w():
        conn = dbmod.connect_postgres(app_uri)
        barrier.wait()
        try:
            authmod.throttle(checks, conn=conn)
            res.append("ok")
        except authmod.AuthError as e:
            res.append(e.code)
        finally:
            conn.close()

    ts = [threading.Thread(target=w) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return res


def _hits(owner_uri, key):
    conn = dbmod.connect_postgres(owner_uri)
    try:
        return [int(r["hits"]) for r in conn.execute(
            "SELECT hits FROM auth_throttle WHERE key_hash=?",
            (authmod._key_hash(key),)).fetchall()]
    finally:
        conn.close()


def test_pg_identitas_atomik_dan_hit_ditolak_tidak_dihitung(dbs):  # noqa: F811
    owner_uri, app_uri = dbs
    res = _race(app_uri, 16, [("login:email:race@t.id", 5, 300)])
    assert res.count("ok") == 5 and res.count(429) == 11, res
    assert _hits(owner_uri, "login:email:race@t.id") == [5]
    # gelombang kedua (semua ditolak) tidak menambah penghitung
    res = _race(app_uri, 16, [("login:email:race@t.id", 5, 300)])
    assert res.count(429) == 16, res
    assert _hits(owner_uri, "login:email:race@t.id") == [5]


def test_pg_rem_global_atomik_identitas_berbeda(dbs):  # noqa: F811
    owner_uri, app_uri = dbs
    res = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def w(i):
        conn = dbmod.connect_postgres(app_uri)
        barrier.wait()
        try:
            authmod.throttle([(f"login:email:u{i}@t.id", 10, 300),
                              ("login:global", 7, 300)], conn=conn)
            r = "ok"
        except authmod.AuthError as e:
            r = e.code
        finally:
            conn.close()
        with lock:
            res.append(r)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert res.count("ok") == 7 and res.count(429) == 9, res
    assert _hits(owner_uri, "login:global") == [7]


# ============ Atomik semua-atau-tidak di PostgreSQL (role aplikasi)
def test_pg_atomik_global_menolak_identitas_tidak_terpakai(dbs):  # noqa: F811
    owner_uri, app_uri = dbs
    conn = dbmod.connect_postgres(app_uri)
    try:
        for _ in range(2):
            authmod.throttle([("g:full", 2, 300)], conn=conn)
        for _ in range(5):
            try:
                authmod.throttle([("id:x", 10, 300), ("g:full", 2, 300)],
                                 conn=conn)
                raise AssertionError("seharusnya 429")
            except authmod.AuthError as e:
                assert e.code == 429
        assert not conn.in_transaction
    finally:
        conn.close()
    assert _hits(owner_uri, "id:x") == []          # tidak ada baris tersisa
    assert _hits(owner_uri, "g:full") == [2]


def test_pg_atomik_konkurensi_identitas_tidak_bocor(dbs):  # noqa: F811
    owner_uri, app_uri = dbs
    res, lock = [], threading.Lock()
    barrier = threading.Barrier(16)

    def w(i):
        conn = dbmod.connect_postgres(app_uri)
        barrier.wait()
        try:
            authmod.throttle([(f"tid:{i}", 10, 300), ("tg", 7, 300)],
                             conn=conn)
            r = "ok"
        except authmod.AuthError as e:
            r = e.code
        finally:
            conn.close()
        with lock:
            res.append(r)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert res.count("ok") == 7 and res.count(429) == 9, res
    assert sum(sum(_hits(owner_uri, f"tid:{i}")) for i in range(16)) == 7
    assert _hits(owner_uri, "tg") == [7]


def test_pg_atomik_urutan_terbalik_tanpa_deadlock(dbs):  # noqa: F811
    """24 thread, separuh memanggil (A, B) dan separuh (B, A): urutan
    penguncian global (key_hash) mencegah deadlock — tidak ada
    DeadlockDetected, semua lolos, penghitung tepat."""
    owner_uri, app_uri = dbs
    res, errs, lock = [], [], threading.Lock()
    barrier = threading.Barrier(24)

    def w(order):
        conn = dbmod.connect_postgres(app_uri)
        barrier.wait()
        try:
            for _ in range(5):
                authmod.throttle(order, conn=conn)
            r = "ok"
        except Exception as e:  # noqa: BLE001
            r = "err"
            with lock:
                errs.append(f"{type(e).__name__}: {e}"[:200])
        finally:
            conn.close()
        with lock:
            res.append(r)

    ab = [("A", 10_000, 300), ("B", 10_000, 300)]
    ts = [threading.Thread(target=w, args=(ab if i % 2 else ab[::-1],))
          for i in range(24)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert res == ["ok"] * 24, errs
    assert _hits(owner_uri, "A") == _hits(owner_uri, "B") == [120]


def test_pg_atomik_lintas_proses(dbs):  # noqa: F811
    from tests.test_w1_identity_rate_limit import run_throttle_procs
    owner_uri, app_uri = dbs
    res = run_throttle_procs(app_uri, 4, 5)
    assert res.count("ok") == 7 and res.count(429) == 13, res
    assert _hits(owner_uri, "pg_global") == [7]
    conn = dbmod.connect_postgres(owner_uri)
    try:
        tot = conn.execute("SELECT SUM(hits) AS n FROM auth_throttle"
                           ).fetchone()["n"]
    finally:
        conn.close()
    assert tot == 14
