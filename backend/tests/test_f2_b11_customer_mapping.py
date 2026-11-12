"""B11 / T3-2: setiap operasi terminal terikat ke customer_id job.

Mapping job -> terminal yang hilang, salah, berubah, atau milik customer
lain harus gagal tertutup tanpa mengubah resource customer lain.

PostgreSQL lokal (pgserver), role aplikasi non-owner, RLS aktif, helper
asli in-process dengan lock flock nyata (HelperBridge). Bukan staging.
"""
import threading
import time

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg2")

import db as dbmod  # noqa: E402
from provisioner import mt5_provision_helper as H  # noqa: E402
from services import provisioning as PV  # noqa: E402
from tests import test_f2_b2_races as B2  # noqa: E402
from tests import test_remediation_pr5_pg as PG  # noqa: E402
from tests.test_remediation_pr5 import _post  # noqa: E402

env = PG.env
pg = PG.pg
_tanpa_chown = B2._tanpa_chown


# ------------------------------------------------------------ pembantu
def _two(env_, tmp_path):
    owner_uri, cfg, app, bridge, hcfg = B2._mk(env_, tmp_path)
    for login, ip in ((91000001, "203.0.113.41"), (91000002, "203.0.113.42")):
        r = B2._final(app, _post(app, login, ip=ip))
        assert r.status_code == 201, r.get_json()
    rows = B2._q(owner_uri, "SELECT t.id, t.customer_id, t.provision_id,"
                            " c.code FROM mt5_terminals t JOIN customers c"
                            " ON c.id=t.customer_id ORDER BY t.id")
    assert len(rows) == 2
    return {"owner": owner_uri, "cfg": cfg, "app": app, "bridge": bridge,
            "hcfg": hcfg, "coord": app.extensions["mt5_coordinator"],
            "A": rows[0], "B": rows[1]}


def _snap(w, T):
    """Seluruh keadaan milik customer T yang tidak boleh berubah."""
    q = w["owner"]
    return (
        B2._q(q, "SELECT id, customer_id, lifecycle, op_owner, provision_id,"
                 " last_error FROM mt5_terminals WHERE customer_id=%s"
                 " ORDER BY id", (T["customer_id"],)),
        B2._q(q, "SELECT id, state FROM mt5_accounts WHERE customer_id=%s",
              (T["customer_id"],)),
        B2._q(q, "SELECT k.account_id, k.enc_password FROM mt5_credentials k"
                 " JOIN mt5_accounts a ON a.id=k.account_id WHERE"
                 " a.customer_id=%s", (T["customer_id"],)),
        H.phase_of(H.paths(w["hcfg"], T["code"])),
        sorted(p.name for p in H.paths(w["hcfg"], T["code"])["base"]
               .iterdir()) if H.paths(w["hcfg"], T["code"])["base"].exists()
        else None,
    )


def _forge_job(w, jid, *, customer_id, terminal, state="failed",
               error_class="cleanup:terminal:uji", created_terminal=1,
               provision_id=None, deadline=None):
    B2._q(w["owner"],
          "INSERT INTO mt5_connect_jobs (id, secret_hash, kind, state,"
          " error_class, customer_id, terminal_id, provision_id,"
          " created_terminal, deadline_at, created_at, updated_at)"
          " VALUES (%s,'x','bind',%s,%s,%s,%s,%s,%s,%s,'a','a')"
          " RETURNING id",
          (jid, state, error_class, customer_id, terminal["id"],
           provision_id or terminal["provision_id"], created_terminal,
           deadline if deadline is not None else time.time() - 1))


def _cleanup(w, jid):
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])   # B12.1: pemanggil tepercaya = role sistem
        w["coord"].cleanup(conn, w["coord"].load(conn, jid))
        dbmod.close_db()


def _helper_ops_on(w, code):
    return [c for c in w["bridge"].calls if c in ("install", "start",
                                                  "remove")], code


# ------------------------------------------------------------ 1. normal
def test_b11_mapping_normal_cleanup_hanya_milik_sendiri(env, tmp_path):
    w = _two(env, tmp_path)
    b0 = _snap(w, w["B"])
    jobA = B2._q(w["owner"], "SELECT id FROM mt5_connect_jobs WHERE"
                             " customer_id=%s", (w["A"]["customer_id"],))[0]
    B2._q(w["owner"], "UPDATE mt5_connect_jobs SET state='failed',"
                      " error_class='cleanup:terminal:uji' WHERE id=%s"
                      " RETURNING id", (jobA["id"],))
    B2._q(w["owner"], "UPDATE mt5_terminals SET op_owner=%s WHERE id=%s"
                      " RETURNING id", ("job:" + jobA["id"], w["A"]["id"]))
    _cleanup(w, jobA["id"])
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == []                 # milik A terhapus
    assert H.phase_of(H.paths(w["hcfg"], w["A"]["code"])) == "absent"
    assert _snap(w, w["B"]) == b0                       # B utuh


# ------------------------------------------------------------ 2. A -> B
@pytest.mark.parametrize("lifecycle_b", ["connected", "provisioning"])
def test_b11_job_a_menunjuk_terminal_b_tidak_menyentuh_b(env, tmp_path,
                                                         lifecycle_b):
    w = _two(env, tmp_path)
    B2._q(w["owner"], "UPDATE mt5_terminals SET lifecycle=%s WHERE id=%s"
                      " RETURNING id", (lifecycle_b, w["B"]["id"]))
    b0 = _snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    _forge_job(w, "a-ke-b", customer_id=w["A"]["customer_id"],
               terminal=w["B"])
    _cleanup(w, "a-ke-b")
    assert _snap(w, w["B"]) == b0
    assert "remove" not in w["bridge"].calls[calls0:]   # helper tidak dipanggil


def test_b11_run_job_a_dengan_terminal_b_gagal_tanpa_helper(env, tmp_path):
    w = _two(env, tmp_path)
    b0 = _snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    accA = B2._q(w["owner"], "SELECT id FROM mt5_accounts WHERE"
                             " customer_id=%s", (w["A"]["customer_id"],))[0]
    B2._q(w["owner"],
          "INSERT INTO mt5_connect_jobs (id, secret_hash, kind, state,"
          " customer_id, account_id, terminal_id, provision_id,"
          " created_terminal, deadline_at, created_at, updated_at)"
          " VALUES ('run-a-b','x','bind','pending',%s,%s,%s,%s,1,%s,'a','a')"
          " RETURNING id", (w["A"]["customer_id"], accA["id"], w["B"]["id"],
                            w["B"]["provision_id"], time.time() + 60))
    with w["app"].app_context():
        st = w["coord"].run_job("run-a-b")
        dbmod.close_db()
    assert st == "failed"
    ops = [c for c in w["bridge"].calls[calls0:]
           if c in ("install", "start", "remove")]
    assert ops == []                                    # tak ada helper
    assert _snap(w, w["B"]) == b0
    job = B2._q(w["owner"], "SELECT state, token_issued_at FROM"
                            " mt5_connect_jobs WHERE id='run-a-b'")[0]
    assert job["token_issued_at"] is None


def test_b11_claim_token_terminal_customer_lain_ditolak(env, tmp_path):
    w = _two(env, tmp_path)
    _forge_job(w, "tok-a-b", customer_id=w["A"]["customer_id"],
               terminal=w["B"], state="connected", error_class=None)
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])   # B12.1: pemanggil tepercaya = role sistem
        assert w["coord"].claim_token(conn, "tok-a-b") is False
        dbmod.close_db()


# ------------------------------------------------------------ 3. berubah
def test_b11_mapping_berubah_saat_remove_tidak_menghapus_baris(env,
                                                               tmp_path):
    """Selama helper menghapus terminal A, baris terminal A berubah menjadi
    milik customer LAIN (korupsi/bug; customer ketiga tanpa terminal karena
    UNIQUE(customer_id)). DELETE bersyarat customer -> baris TIDAK dihapus
    oleh operasi A; resource B tetap utuh."""
    w = _two(env, tmp_path)
    other = B2._q(w["owner"], "INSERT INTO customers (code) VALUES ('C099')"
                              " RETURNING id")[0]["id"]
    b0 = _snap(w, w["B"])
    real = w["bridge"].remove

    def remove_lalu_ubah(spec):
        out = real(spec)
        B2._q(w["owner"], "UPDATE mt5_terminals SET customer_id=%s WHERE"
                          " id=%s RETURNING id", (other, w["A"]["id"]))
        return out
    w["bridge"].remove = remove_lalu_ubah
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])   # B12.1: pemanggil tepercaya = role sistem
        r = w["coord"].remove_terminal(conn, int(w["A"]["id"]),
                                       w["A"]["provision_id"],
                                       customer_id=w["A"]["customer_id"])
        dbmod.close_db()
    assert r is False                                   # tidak dianggap bebas
    assert B2._q(w["owner"], "SELECT customer_id FROM mt5_terminals WHERE"
                             " id=%s", (w["A"]["id"],))[0]["customer_id"] \
        == other                                        # baris tidak dihapus
    assert _snap(w, w["B"]) == b0                       # B sepenuhnya utuh


def test_b11_mapping_berubah_saat_verifikasi_tidak_connected(env, tmp_path,
                                                             monkeypatch):
    w = _two(env, tmp_path)
    other = B2._q(w["owner"], "INSERT INTO customers (code) VALUES ('C098')"
                              " RETURNING id")[0]["id"]
    real = PV.live_verify_account
    seen = {}

    def verify_lalu_ubah(conn, *a, **k):
        tid = B2._q(w["owner"], "SELECT terminal_id FROM mt5_connect_jobs"
                                " WHERE state='verifying'")
        if tid:
            seen["tid"] = tid[0]["terminal_id"]
            changed = B2._q(w["owner"], "UPDATE mt5_terminals SET"
                                        " customer_id=%s WHERE id=%s"
                                        " RETURNING id", (other, seen["tid"]))
            seen["diubah"] = len(changed) == 1      # perubahan BENAR terjadi
        return real(conn, *a, **k)
    monkeypatch.setattr(PV, "live_verify_account", verify_lalu_ubah)
    r = B2._final(w["app"], _post(w["app"], 91000003, ip="203.0.113.43"))
    assert r.status_code not in (200, 201), r.get_json()
    assert seen.get("diubah") is True
    assert B2._q(w["owner"], "SELECT count(*) AS n FROM mt5_connect_jobs"
                             " WHERE state='connected' AND terminal_id=%s",
                 (seen["tid"],))[0]["n"] == 0
    assert B2._q(w["owner"], "SELECT count(*) AS n FROM auth_tokens WHERE"
                             " customer_id NOT IN (%s, %s) AND revoked_at"
                             " IS NULL", (w["A"]["customer_id"],
                                          w["B"]["customer_id"]))[0]["n"] == 0


# ------------------------------------------------------------ 4. hilang/rusak
def test_b11_job_tanpa_customer_tidak_menyentuh_apa_pun(env, tmp_path):
    w = _two(env, tmp_path)
    a0, b0 = _snap(w, w["A"]), _snap(w, w["B"])
    for T in (w["A"], w["B"]):
        jid = f"tanpa-cust-{T['id']}"
        _forge_job(w, jid, customer_id=None, terminal=T)
        _cleanup(w, jid)
    assert _snap(w, w["A"]) == a0 and _snap(w, w["B"]) == b0


def test_b11_job_terminal_tidak_ada_aman(env, tmp_path):
    w = _two(env, tmp_path)
    b0 = _snap(w, w["B"])
    _forge_job(w, "hantu", customer_id=w["A"]["customer_id"],
               terminal={"id": 999999, "provision_id": "c" * 32})
    _cleanup(w, "hantu")
    assert _snap(w, w["B"]) == b0


def test_b11_remove_tanpa_customer_id_ditolak(env, tmp_path):
    w = _two(env, tmp_path)
    b0 = _snap(w, w["B"])
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])   # B12.1: pemanggil tepercaya = role sistem
        assert w["coord"].remove_terminal(conn, int(w["B"]["id"]),
                                          w["B"]["provision_id"],
                                          customer_id=None) is False
        assert w["coord"].remove_terminal(conn, int(w["B"]["id"]),
                                          w["B"]["provision_id"],
                                          customer_id=w["A"]["customer_id"]) \
            is False
        dbmod.close_db()
    assert _snap(w, w["B"]) == b0


# ------------------------------------------------------------ 5. bersamaan
def test_b11_dua_job_bersamaan_hanya_milik_sendiri(env, tmp_path):
    """Dua cleanup bersamaan (koneksi terpisah): job sah A atas terminal A
    dan job korup (customer A) atas terminal B."""
    w = _two(env, tmp_path)
    b0 = _snap(w, w["B"])
    jobA = B2._q(w["owner"], "SELECT id FROM mt5_connect_jobs WHERE"
                             " customer_id=%s", (w["A"]["customer_id"],))[0]
    B2._q(w["owner"], "UPDATE mt5_connect_jobs SET state='failed',"
                      " error_class='cleanup:terminal:uji' WHERE id=%s"
                      " RETURNING id", (jobA["id"],))
    B2._q(w["owner"], "UPDATE mt5_terminals SET op_owner=%s WHERE id=%s"
                      " RETURNING id", ("job:" + jobA["id"], w["A"]["id"]))
    _forge_job(w, "korup-b", customer_id=w["A"]["customer_id"],
               terminal=w["B"])
    errs, barrier = [], threading.Barrier(2)

    def run(jid):
        try:
            barrier.wait()
            _cleanup(w, jid)
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    th = [threading.Thread(target=run, args=(j,))
          for j in (jobA["id"], "korup-b")]
    [t.start() for t in th]
    [t.join(60) for t in th]
    assert errs == []
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == []
    assert _snap(w, w["B"]) == b0


# ------------------------------------------------------------ 6. pemulihan
def test_b11_pemulihan_pemilik_basi_customer_lain_tidak_disentuh(env,
                                                                 tmp_path):
    """Terminal B dipegang 'job:X' padahal job X milik customer A (final).
    Sapuan pemilik basi + retry TIDAK boleh mengubah atau menghapus B."""
    w = _two(env, tmp_path)
    _forge_job(w, "basi-x", customer_id=w["A"]["customer_id"],
               terminal=w["B"], error_class="interrupted")
    B2._q(w["owner"], "UPDATE mt5_terminals SET op_owner='job:basi-x' WHERE"
                      " id=%s RETURNING id", (w["B"]["id"],))
    b0 = _snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])   # B12.1: pemanggil tepercaya = role sistem
        w["coord"].retry_uncertain_removals(conn)
        w["coord"].recover_expired(conn)
        dbmod.close_db()
    assert _snap(w, w["B"]) == b0
    assert "remove" not in w["bridge"].calls[calls0:]


def test_b11_unbind_hanya_terminal_customer_sendiri(env, tmp_path):
    """Jalur unbind meneruskan customer pemilik request."""
    w = _two(env, tmp_path)
    b0 = _snap(w, w["B"])
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
        r = c.post("/api/v1/accounts/unbind",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.get_json()["terminal_released"] is True
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == []
    assert _snap(w, w["B"]) == b0


# ============================================================ B11.1
# Pemulihan (_retry_uncertain_removals): kolom eksplisit, mapping
# customer terminal <-> job diverifikasi, fail-closed, tanpa KeyError,
# dan exception satu baris tidak membuat baris lain terlewati.
def _pending(w, T, why="retry:1:uji"):
    B2._q(w["owner"], "UPDATE mt5_terminals SET op_owner=%s,"
                      " lifecycle='failed', last_error=%s WHERE id=%s"
                      " RETURNING id", (PV.OWNER_PENDING, why, T["id"]))


def _retry(w):
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])   # B12.1: pemanggil tepercaya = role sistem
        n = w["coord"].retry_uncertain_removals(conn)
        dbmod.close_db()
    return n


def _tanpa_error_kode(caplog):
    """Tidak ada exception pemrograman (KeyError/TypeError/IndexError)
    yang ditelan oleh blok except pemulihan."""
    bad = [r for r in caplog.records if r.exc_info and
           issubclass(r.exc_info[0], (KeyError, TypeError, IndexError))]
    assert bad == [], [r.getMessage() for r in bad]


def test_b111_pemilik_basi_mapping_benar_dipulihkan(env, tmp_path, caplog):
    w = _two(env, tmp_path)
    b0 = _snap(w, w["B"])
    _forge_job(w, "basi-a", customer_id=w["A"]["customer_id"],
               terminal=w["A"], error_class="interrupted")
    B2._q(w["owner"], "UPDATE mt5_terminals SET op_owner='job:basi-a' WHERE"
                      " id=%s RETURNING id", (w["A"]["id"],))
    assert _retry(w) == 1
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == []
    assert H.phase_of(H.paths(w["hcfg"], w["A"]["code"])) == "absent"
    assert _snap(w, w["B"]) == b0
    _tanpa_error_kode(caplog)


def test_b111_pemilik_basi_bukan_job_pembuat_dilepas_saja(env, tmp_path,
                                                          caplog):
    w = _two(env, tmp_path)
    b0 = _snap(w, w["B"])
    _forge_job(w, "basi-a2", customer_id=w["A"]["customer_id"],
               terminal=w["A"], error_class="interrupted", created_terminal=0)
    B2._q(w["owner"], "UPDATE mt5_terminals SET op_owner='job:basi-a2' WHERE"
                      " id=%s RETURNING id", (w["A"]["id"],))
    calls0 = len(w["bridge"].calls)
    assert _retry(w) == 0
    t = B2._q(w["owner"], "SELECT op_owner FROM mt5_terminals WHERE id=%s",
              (w["A"]["id"],))
    assert t == [{"op_owner": None}]
    assert "remove" not in w["bridge"].calls[calls0:]
    assert _snap(w, w["B"]) == b0
    _tanpa_error_kode(caplog)


@pytest.mark.parametrize("job_cust", ["lain", "null"])
def test_b111_pending_dengan_job_korup_fail_closed(env, tmp_path, caplog,
                                                   job_cust):
    """Terminal B removal-pending, tetapi ada job yang merujuk B dengan
    customer A / tanpa customer: mapping korup -> helper tidak dipanggil,
    B tidak disentuh."""
    w = _two(env, tmp_path)
    _pending(w, w["B"])
    _forge_job(w, "korup-" + job_cust, terminal=w["B"],
               customer_id=(w["A"]["customer_id"] if job_cust == "lain"
                            else None))
    b0 = _snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    assert _retry(w) == 0
    assert _snap(w, w["B"]) == b0
    assert "remove" not in w["bridge"].calls[calls0:]
    assert any("pemulihan DITOLAK" in r.getMessage() for r in caplog.records)
    _tanpa_error_kode(caplog)


def test_b111_pending_mapping_benar_dipulihkan_lain_utuh(env, tmp_path,
                                                         caplog):
    w = _two(env, tmp_path)
    _pending(w, w["A"])
    b0 = _snap(w, w["B"])
    assert _retry(w) == 1
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == []
    assert _snap(w, w["B"]) == b0
    _tanpa_error_kode(caplog)


def test_b111_data_kandidat_tidak_lengkap_ditolak(env, tmp_path):
    w = _two(env, tmp_path)
    with w["app"].app_context():
        conn = dbmod.get_system_db(w["cfg"])   # B12.1: pemanggil tepercaya = role sistem
        f = w["coord"]._recovery_mapping_problem
        A = w["A"]
        assert f(conn, {"id": A["id"], "customer_id": A["customer_id"],
                        "provision_id": A["provision_id"]}) is None
        assert "customer_id" in f(conn, {"id": A["id"], "customer_id": None,
                                         "provision_id": A["provision_id"]})
        assert "provision_id" in f(conn, {"id": A["id"],
                                          "customer_id": A["customer_id"],
                                          "provision_id": None})
        assert "tidak ada" in f(conn, {"id": A["id"], "customer_id": 987654,
                                       "provision_id": A["provision_id"]})
        # spec_for tanpa customer -> None (fail-closed)
        assert w["coord"].spec_for(conn, int(A["id"]), None) is None
        assert w["coord"].spec_for(conn, int(A["id"]),
                                   w["B"]["customer_id"]) is None
        dbmod.close_db()


def test_b111_exception_satu_baris_tidak_melewatkan_baris_lain(
        env, tmp_path, monkeypatch, caplog):
    """Exception saat memproses A dicatat (log), B tetap diproses pada
    putaran yang sama, dan A tidak diklaim sehingga diproses pada
    putaran berikutnya (tidak hilang diam-diam)."""
    w = _two(env, tmp_path)
    _pending(w, w["A"])
    _pending(w, w["B"])
    asli = PV.ConnectCoordinator._recovery_mapping_problem

    def rusak(conn, r):
        if int(r["id"]) == int(w["A"]["id"]):
            raise RuntimeError("uji: gangguan baris A")
        return asli(conn, r)
    monkeypatch.setattr(PV.ConnectCoordinator, "_recovery_mapping_problem",
                        staticmethod(rusak))
    assert _retry(w) == 1
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["B"]["id"],)) == []
    a = B2._q(w["owner"], "SELECT op_owner, lifecycle FROM mt5_terminals"
                          " WHERE id=%s", (w["A"]["id"],))
    assert a == [{"op_owner": PV.OWNER_PENDING, "lifecycle": "failed"}]
    assert any("retry remove terminal" in r.getMessage() and r.exc_info
               for r in caplog.records)
    monkeypatch.setattr(PV.ConnectCoordinator, "_recovery_mapping_problem",
                        staticmethod(asli))
    assert _retry(w) == 1
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == []
    _tanpa_error_kode(caplog)


# ============================================================ B11.2
# Pemilik basi (op_owner='job:X') hanya dipulihkan bila job X cocok
# SELURUH identitasnya dengan terminal: customer_id, terminal_id,
# provision_id. Selain itu: lifecycle/op_owner tidak berubah, helper
# tidak dipanggil, terminal & resource tidak dihapus, n tidak bertambah.
def _stale_on(w, T, jid, **kw):
    kw.setdefault("customer_id", T["customer_id"])
    kw.setdefault("terminal", T)
    _forge_job(w, jid, error_class="interrupted", **kw)
    B2._q(w["owner"], "UPDATE mt5_terminals SET op_owner=%s WHERE id=%s"
                      " RETURNING id", ("job:" + jid, T["id"]))


def _ditolak(w, T, caplog, calls0, alasan):
    assert "remove" not in w["bridge"].calls[calls0:]
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (T["id"],)) != []
    assert H.phase_of(H.paths(w["hcfg"], T["code"])) != "absent"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("pemilik basi" in m and "DITOLAK" in m and alasan in m
               for m in msgs), msgs
    _tanpa_error_kode(caplog)


@pytest.mark.parametrize("target", ["tidak_ada", "terminal_b"])
def test_b112_job_customer_sama_terminal_id_berbeda(env, tmp_path, caplog,
                                                    target):
    w = _two(env, tmp_path)
    lain = ({"id": 987654, "provision_id": w["A"]["provision_id"]}
            if target == "tidak_ada" else w["B"])
    _stale_on(w, w["A"], "x-tid", terminal=lain,
              provision_id=w["A"]["provision_id"])
    a0, b0 = _snap(w, w["A"]), _snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    assert _retry(w) == 0
    assert _snap(w, w["A"]) == a0 and _snap(w, w["B"]) == b0
    _ditolak(w, w["A"], caplog, calls0, "terminal_id")


@pytest.mark.parametrize("prov", ["lain", "null"])
def test_b112_job_provision_id_berbeda(env, tmp_path, caplog, prov):
    w = _two(env, tmp_path)
    _stale_on(w, w["A"], "x-prov")
    B2._q(w["owner"], "UPDATE mt5_connect_jobs SET provision_id=%s WHERE"
                      " id='x-prov' RETURNING id",
          ("f" * 32 if prov == "lain" else None,))
    a0, b0 = _snap(w, w["A"]), _snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    assert _retry(w) == 0
    assert _snap(w, w["A"]) == a0 and _snap(w, w["B"]) == b0
    _ditolak(w, w["A"], caplog, calls0, "provision_id")


@pytest.mark.parametrize("pemilik_job", ["A", "B"])
def test_b112_job_menunjuk_b_op_owner_di_a(env, tmp_path, caplog,
                                           pemilik_job):
    """Job X merujuk terminal B (customer A atau B), tetapi op_owner
    'job:X' berada di terminal A. A dan B tidak boleh berubah."""
    w = _two(env, tmp_path)
    _forge_job(w, "x-silang", customer_id=w[pemilik_job]["customer_id"],
               terminal=w["B"], error_class="interrupted")
    B2._q(w["owner"], "UPDATE mt5_terminals SET op_owner='job:x-silang'"
                      " WHERE id=%s RETURNING id", (w["A"]["id"],))
    a0, b0 = _snap(w, w["A"]), _snap(w, w["B"])
    calls0 = len(w["bridge"].calls)
    assert _retry(w) == 0
    assert _snap(w, w["A"]) == a0 and _snap(w, w["B"]) == b0
    _ditolak(w, w["A"], caplog, calls0,
             "customer" if pemilik_job == "B" else "terminal_id")
    assert "remove" not in w["bridge"].calls[calls0:]


def test_b112_job_basi_identitas_lengkap_cocok_dipulihkan(env, tmp_path,
                                                          caplog):
    w = _two(env, tmp_path)
    _stale_on(w, w["A"], "x-sah")
    b0 = _snap(w, w["B"])
    assert _retry(w) == 1
    assert B2._q(w["owner"], "SELECT id FROM mt5_terminals WHERE id=%s",
                 (w["A"]["id"],)) == []
    assert H.phase_of(H.paths(w["hcfg"], w["A"]["code"])) == "absent"
    assert _snap(w, w["B"]) == b0
    assert not any("DITOLAK" in r.getMessage() for r in caplog.records)
    _tanpa_error_kode(caplog)


def test_b112_terminal_lama_provision_null_null_dilepas(env, tmp_path,
                                                        caplog):
    """Terminal lama (provision_id NULL) dipegang job non-pembuat yang
    juga NULL: identitas sah (NULL = NULL), op_owner dilepas saja."""
    w = _two(env, tmp_path)
    B2._q(w["owner"], "UPDATE mt5_terminals SET provision_id=NULL WHERE"
                      " id=%s RETURNING id", (w["A"]["id"],))
    _stale_on(w, w["A"], "x-lama", created_terminal=0)
    B2._q(w["owner"], "UPDATE mt5_connect_jobs SET provision_id=NULL WHERE"
                      " id='x-lama' RETURNING id")
    calls0 = len(w["bridge"].calls)
    assert _retry(w) == 0
    assert B2._q(w["owner"], "SELECT op_owner FROM mt5_terminals WHERE"
                             " id=%s", (w["A"]["id"],)) == [{"op_owner": None}]
    assert "remove" not in w["bridge"].calls[calls0:]
    assert not any("DITOLAK" in r.getMessage() for r in caplog.records)


def test_b112_identitas_berubah_setelah_validasi_update_atomik_menolak(
        env, tmp_path, monkeypatch, caplog):
    """Job diubah (terminal_id) di antara validasi Python dan UPDATE:
    predikat EXISTS di UPDATE menolak — terminal tidak disentuh."""
    w = _two(env, tmp_path)
    _stale_on(w, w["A"], "x-balap")
    a0 = _snap(w, w["A"])
    asli = PV.ConnectCoordinator._stale_owner_problem

    def balap(r):
        hasil = asli(r)
        B2._q(w["owner"], "UPDATE mt5_connect_jobs SET terminal_id=987654"
                          " WHERE id='x-balap' RETURNING id")
        return hasil
    monkeypatch.setattr(PV.ConnectCoordinator, "_stale_owner_problem",
                        staticmethod(balap))
    calls0 = len(w["bridge"].calls)
    assert _retry(w) == 0
    assert _snap(w, w["A"]) == a0
    assert "remove" not in w["bridge"].calls[calls0:]
    assert any("berubah sebelum dilepas" in r.getMessage()
               for r in caplog.records)
    _tanpa_error_kode(caplog)
