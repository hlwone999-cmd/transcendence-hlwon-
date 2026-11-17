"""F2/B2 — race cleanup job A vs job B (T1), retry unbind (S1), exit helper
tidak normal (S2), dan SEMUA jalur penerbitan token.

PostgreSQL SUNGGUHAN, role NON-OWNER `transcendence_app` (RLS aktif),
helper ASLI in-process (lock flock nyata, berkas nyata) lewat HelperBridge,
dan beberapa worker (thread dengan koneksi PostgreSQL terpisah). Bukan
staging: tanpa Wine/MT5/systemd/sudo nyata.
"""
import threading
import time

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg2")
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

import db as dbmod  # noqa: E402
from provisioner import mt5_provision_helper as H  # noqa: E402
from services import provisioner as P  # noqa: E402
from tests import test_f2_remove_timeout as RT  # noqa: E402
from tests import test_remediation_pr5_pg as PG  # noqa: E402
from tests.test_remediation_pr5 import _app, _poll, _post  # noqa: E402
env = PG.env          # fixture PostgreSQL (database baru per tes)
pg = PG.pg            # fixture server PostgreSQL (modul)

_tanpa_chown = RT._tanpa_chown        # fixture autouse (chown dimatikan)


class Bridge(RT.HelperBridge):
    """+ status 'running' = instalasi lengkap (tanpa systemd nyata);
    + `abnormal`: op -> rc yang dikembalikan SETELAH op dijalankan (S2)."""

    def __init__(self, cfg, hcfg):
        super().__init__(cfg, hcfg)
        self.fail_start = False
        self.abnormal = {}

    def _spawn(self, argv, timeout):
        args = list(argv[3:])
        if args[0] == "status":
            code = args[args.index("--code") + 1]
            p = H.paths(self.hcfg, code)
            tombs = H.tombs_for(self.hcfg, code) if hasattr(
                H, "tombs_for") else None
            ph = H.phase_of(p, tombs) if tombs is not None else H.phase_of(p)
            import json
            return 0, json.dumps({"ok": True, "running": ph == "complete",
                                  "busy": H.lock_busy(self.hcfg, code)})
        res = super()._spawn(argv, timeout)
        with self._mu:
            if self.abnormal.get(args[0]):
                return self.abnormal.pop(args[0]), ""
        return res


def _mk(env_, tmp_path, **kw):
    owner_uri, app_uri = env_
    cfg = PG._cfg(app_uri, mt5_connect_sync_wait_seconds=0.0,
                  mt5_provision_timeout_seconds=40.0, **kw)
    cfg, app, _, hcfg = RT._setup(tmp_path, cfg)
    bridge = Bridge(cfg, hcfg)
    app.extensions["mt5_coordinator"].provisioner = bridge
    app.extensions["mt5_provisioner"] = bridge
    return owner_uri, cfg, app, bridge, hcfg


def _q(owner_uri, sql, params=()):
    c = psycopg2.connect(owner_uri)
    c.autocommit = True              # UPDATE uji harus benar-benar tersimpan
    try:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []
    finally:
        c.close()


def _invariants(owner_uri, hcfg):
    """Token & terminal TIDAK PERNAH bertentangan:
    - job 'connected' -> baris terminal ada, lifecycle connected, bukan
      sedang dihapus; akun active; helper: instalasi lengkap.
    - token diklaim -> job connected.
    - tidak ada baris terminal 'connected' tanpa resource helper."""
    bad = []
    for j in _q(owner_uri, "SELECT * FROM mt5_connect_jobs"):
        if j["token_issued_at"] is not None and j["state"] != "connected":
            bad.append(("token_tanpa_connected", j["id"]))
        if j["state"] != "connected":
            continue
        a = _q(owner_uri, "SELECT state, customer_id FROM mt5_accounts"
               " WHERE id=%s", (j["account_id"],))
        if a and a[0]["state"] in ("unbound", "credentials_rejected"):
            # pelepasan SAH sesudah connected: terminal boleh hilang, tetapi
            # TIDAK boleh ada token aktif customer itu
            live = _q(owner_uri, "SELECT COUNT(*) AS n FROM auth_tokens WHERE"
                      " customer_id=%s AND revoked_at IS NULL",
                      (a[0]["customer_id"],))[0]["n"]
            if live:
                bad.append(("token_aktif_setelah_unbind", j["id"]))
            continue
        t = _q(owner_uri, "SELECT t.*, c.code FROM mt5_terminals t JOIN"
               " customers c ON c.id=t.customer_id WHERE t.id=%s",
               (j["terminal_id"],))
        a = _q(owner_uri, "SELECT state FROM mt5_accounts WHERE id=%s",
               (j["account_id"],))
        if not t:
            bad.append(("connected_tanpa_terminal", j["id"]))
            continue
        t = t[0]
        if t["lifecycle"] != "connected" or (t.get("op_owner") or "") \
                .startswith(("remove:", "removal-pending")):
            bad.append(("terminal_tidak_connected", j["id"], t["lifecycle"]))
        if not a or a[0]["state"] != "active":
            bad.append(("akun_tidak_active", j["id"]))
        if H.phase_of(H.paths(hcfg, t["code"])) != "complete":
            bad.append(("resource_hilang", j["id"], t["code"]))
    return bad


def _final(app, r, timeout=30.0):
    """Poll SAMPAI respons final pertama — dan kembalikan respons ITU
    (token hanya dapat diklaim sekali)."""
    if r.status_code != 202:
        return r
    b = r.get_json()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rr = _poll(app, b["job_id"], b["job_secret"])
        if rr.status_code != 202:
            return rr
        time.sleep(0.1)
    raise AssertionError("job tidak final")


def _wait(pred, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _final_jobs(owner_uri):
    return all(j["state"] == "connected" or (
        j["state"] == "failed" and not str(j["error_class"] or "")
        .startswith("cleanup:"))
        for j in _q(owner_uri, "SELECT state, error_class FROM"
                               " mt5_connect_jobs"))


@pytest.fixture(autouse=True)
def _slow_reset():
    yield
    RT.SlowRunner.delay = 0.0


# ================================================================ T1
def test_t1_cleanup_A_bersamaan_connect_B(env, tmp_path):
    """Job A (password salah) gagal -> cleanup -> remove LAMBAT; selama itu
    job B (password benar, customer & terminal sama) connect."""
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    RT.SlowRunner.delay = 2.0
    rA = _post(app, 61000001, password="salah")
    assert rA.status_code == 202, rA.get_json()
    assert _wait(lambda: "remove" in bridge.calls)
    rB = _post(app, 61000001, password="good", ip="203.0.113.9")
    rB = _final(app, rB)
    assert _wait(lambda: _final_jobs(owner_uri))
    assert _invariants(owner_uri, hcfg) == []
    if rB.status_code in (200, 201):          # B boleh berhasil HANYA konsisten
        assert rB.get_json().get("token")


def test_t1_connect_saat_terminal_stopping(env, tmp_path):
    """Unbind -> remove LAMBAT (lifecycle stopping); customer connect lagi
    (reactivate, terminal sama) -> job B TIDAK boleh memakai terminal itu."""
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    r = _post(app, 61000002)
    r = _final(app, r)
    assert r.status_code == 201, r.get_json()
    tok = r.get_json()["token"]
    RT.SlowRunner.delay = 2.0
    out = {}

    def unbind():
        with app.test_client() as c:
            out["u"] = c.post("/api/v1/accounts/unbind",
                              headers={"Authorization": f"Bearer {tok}"})
    th = threading.Thread(target=unbind)
    th.start()
    assert _wait(lambda: bool(_q(owner_uri, "SELECT 1 FROM mt5_terminals"
                                            " WHERE lifecycle='stopping'")))
    rB = _post(app, 61000002, ip="203.0.113.10")
    rB = _final(app, rB)
    th.join(30)
    assert out["u"].status_code == 200
    assert _wait(lambda: _final_jobs(owner_uri))
    assert rB.status_code not in (200, 201), rB.get_json()   # tanpa token
    assert _invariants(owner_uri, hcfg) == []
    assert _q(owner_uri, "SELECT COUNT(*) AS n FROM mt5_terminals")[0]["n"] \
        == 0
    assert H.phase_of(H.paths(hcfg, "C001")) == "absent"


def test_t1_remove_selama_verifikasi_B_tidak_menyentuh(env, tmp_path):
    """Job B (terminal milik customer, created_terminal=0) sedang VERIFIKASI;
    worker lain memanggil remove_terminal -> TIDAK boleh menghapus
    terminal yang dipegang B; B connected konsisten."""
    owner_uri, app_uri = env
    cfg = PG._cfg(app_uri, mt5_connect_sync_wait_seconds=0.0,
                  mt5_provision_timeout_seconds=40.0)
    app = _app(cfg, delay=2.0)                 # verifikasi broker lambat
    cfg, app, _, hcfg = RT._setup(tmp_path, cfg) if False else \
        (cfg, app, None, None)
    tpl = tmp_path / "template"
    tpl.mkdir()
    (tpl / "terminal64.exe").write_text("bin")
    (tmp_path / "units").mkdir()
    import pathlib
    pathlib.Path(cfg.term_base_dir).mkdir(parents=True, exist_ok=True)
    hcfg = H.HelperConfig(base_dir=pathlib.Path(cfg.term_base_dir),
                          template_dir=tpl, unit_dir=tmp_path / "units",
                          service_user="x", port_base=int(cfg.rpyc_port_base),
                          max_slots=int(cfg.max_customers),
                          proc_root=tmp_path / "proc",
                          lock_dir=tmp_path / "locks", lock_wait_seconds=0.3)
    bridge = Bridge(cfg, hcfg)
    coord = app.extensions["mt5_coordinator"]
    coord.provisioner = bridge
    r = _post(app, 61000003)
    assert _wait(lambda: bool(_q(owner_uri, "SELECT 1 FROM mt5_connect_jobs"
                                            " WHERE state='verifying'")))
    t = _q(owner_uri, "SELECT id, provision_id, customer_id FROM"
                      " mt5_terminals")[0]
    res = {}

    def remover():
        with app.app_context():
            conn = dbmod.get_system_db(cfg)   # B12.1: pemanggil tepercaya = role sistem
            res["r"] = coord.remove_terminal(conn, int(t["id"]),
                                             t["provision_id"],
                                             customer_id=t["customer_id"])
            dbmod.close_db()
    th = threading.Thread(target=remover)
    th.start()
    th.join(30)
    r = _final(app, r)
    assert _wait(lambda: _final_jobs(owner_uri))
    assert _invariants(owner_uri, hcfg) == []
    jobs = _q(owner_uri, "SELECT state FROM mt5_connect_jobs")
    if jobs[0]["state"] == "connected":
        assert res["r"] is False                  # remover TIDAK menyentuh
        assert H.phase_of(H.paths(hcfg, "C001")) == "complete"


def test_t1_token_tidak_diklaim_saat_terminal_dihapus(env, tmp_path):
    """Job connected, token BELUM diambil; terminal lalu masuk penghapusan
    (klaim remove) -> klaim token DITOLAK (tidak ada token untuk terminal
    yang sedang dihapus)."""
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    r = _post(app, 61000004)
    assert r.status_code == 202
    b = r.get_json()
    assert _wait(lambda: bool(_q(owner_uri, "SELECT 1 FROM mt5_connect_jobs"
                                            " WHERE state='connected'")))
    _q(owner_uri, "UPDATE mt5_terminals SET lifecycle='stopping',"
                  " op_owner='remove:lain' RETURNING id")
    rr = _poll(app, b["job_id"], b["job_secret"])
    assert rr.status_code == 410, rr.get_json()
    assert _q(owner_uri, "SELECT COUNT(*) AS n FROM auth_tokens")[0]["n"] == 0


def test_t1_B_berhasil_setelah_A_lalu_cleanup_A_ulang_tidak_menyentuh_B(
        env, tmp_path):
    """A mati sebelum worker berjalan (deadline lewat). B connect: pemulihan
    membersihkan A, B membuat terminal baru & connected. Cleanup A diulang
    (crash/pemulihan ganda) -> terminal, akun, kredensial B TIDAK disentuh."""
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    coord = app.extensions["mt5_coordinator"]
    real_start = coord.start
    coord.start = lambda job_id: None
    rA = _post(app, 61000005, password="good")
    assert rA.status_code == 202
    coord.start = real_start
    _q(owner_uri, "UPDATE mt5_connect_jobs SET deadline_at=%s RETURNING id",
       (time.time() - 1,))
    rB = _post(app, 61000005, password="good", ip="203.0.113.11")
    rB = _final(app, rB)
    assert rB.status_code == 201, rB.get_json()
    before = (_q(owner_uri, "SELECT id, lifecycle, provision_id, op_owner"
                            " FROM mt5_terminals"),
              _q(owner_uri, "SELECT id, state FROM mt5_accounts"),
              _q(owner_uri, "SELECT account_id FROM mt5_credentials"))
    jobA = [j for j in _q(owner_uri, "SELECT id FROM mt5_connect_jobs WHERE"
                                     " state='failed'")][0]["id"]
    with app.app_context():
        conn = dbmod.get_system_db(cfg)   # B12.1: pemanggil tepercaya = role sistem
        coord.cleanup(conn, coord.load(conn, jobA))       # cleanup A diulang
        dbmod.close_db()
    after = (_q(owner_uri, "SELECT id, lifecycle, provision_id, op_owner"
                           " FROM mt5_terminals"),
             _q(owner_uri, "SELECT id, state FROM mt5_accounts"),
             _q(owner_uri, "SELECT account_id FROM mt5_credentials"))
    assert after == before
    assert _invariants(owner_uri, hcfg) == []


def test_t1_dua_worker_cleanup_bersamaan(env, tmp_path):
    """Cleanup job A terputus setelah fase akun (crash). Dua worker menjalankan
    recover_expired BERSAMAAN (koneksi PostgreSQL terpisah): tepat satu
    remove helper, baris & resource hilang, error_class final, tanpa galat."""
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    coord = app.extensions["mt5_coordinator"]
    bridge.fail_start = True
    real_rt = coord.remove_terminal

    def crash(*a, **k):
        raise RuntimeError("proses mati di tengah cleanup (uji)")
    coord.remove_terminal = crash
    rA = _post(app, 61000006)
    assert rA.status_code == 202
    assert _wait(lambda: bool(_q(owner_uri, "SELECT 1 FROM mt5_connect_jobs"
                                            " WHERE state='failed'")))
    time.sleep(0.3)
    coord.remove_terminal = real_rt
    _q(owner_uri, "UPDATE mt5_connect_jobs SET deadline_at=%s RETURNING id",
       (time.time() - 1,))
    assert H.phase_of(H.paths(hcfg, "C001")) == "complete"
    before = bridge.calls.count("remove")
    RT.SlowRunner.delay = 1.0
    errs, barrier = [], threading.Barrier(2)

    def worker():
        try:
            with app.app_context():
                conn = dbmod.get_system_db(cfg)   # B12.1: pemanggil tepercaya = role sistem
                barrier.wait()
                coord.recover_expired(conn)
                dbmod.close_db()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    th = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in th]
    [t.join(60) for t in th]
    assert errs == []
    assert bridge.calls.count("remove") - before == 1
    assert _q(owner_uri, "SELECT COUNT(*) AS n FROM mt5_terminals")[0]["n"] \
        == 0
    assert H.phase_of(H.paths(hcfg, "C001")) == "absent"
    assert _final_jobs(owner_uri)
    assert _invariants(owner_uri, hcfg) == []


# ================================================================ S1
def test_s1_unbind_sibuk_masuk_pemulihan_dan_diselesaikan(env, tmp_path):
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    r = _post(app, 61000007)
    r = _final(app, r)
    tok = _q(owner_uri, "SELECT 1 FROM mt5_connect_jobs WHERE"
                        " state='connected'")
    assert tok
    # token login dari job tidak lagi tersedia (sudah diambil) -> unbind via
    # coordinator langsung dengan helper SIBUK (lock dipegang)
    fd = H.acquire_lock(hcfg, "C001")
    try:
        with app.app_context():
            conn = dbmod.get_system_db(cfg)   # B12.1: pemanggil tepercaya = role sistem
            t = conn.execute("SELECT id, provision_id, customer_id FROM"
                             " mt5_terminals").fetchone()
            released = app.extensions["mt5_coordinator"].remove_terminal(
                conn, int(t["id"]), t["provision_id"],
                customer_id=t["customer_id"])
            dbmod.close_db()
    finally:
        import os
        os.close(fd)
    assert released is False
    row = _q(owner_uri, "SELECT lifecycle, last_error, op_owner FROM"
                        " mt5_terminals")[0]
    assert row["last_error"].startswith("retry:1:ProvisionBusy")
    assert row["op_owner"] == "removal-pending"
    # pemulihan (startup) memproses baris ini walau job-nya 'connected'
    with app.app_context():
        n = app.extensions["mt5_coordinator"].retry_uncertain_removals(
            dbmod.get_db(cfg))
        dbmod.close_db()
    assert n == 1
    assert _q(owner_uri, "SELECT COUNT(*) AS n FROM mt5_terminals")[0]["n"] \
        == 0
    assert H.phase_of(H.paths(hcfg, "C001")) == "absent"


def test_s1_retry_habis_dilaporkan_admin(env, tmp_path):
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    bridge.fail_start = True
    fd = H.acquire_lock(hcfg, "C001") if False else None
    coord = app.extensions["mt5_coordinator"]
    real = bridge._after_timeout
    bridge._after_timeout = lambda op, spec, a: (
        (_ for _ in ()).throw(P.ProvisionTimeout("x", "unknown"))
        if op == "remove" else real(op, spec, a))
    bridge.lose = {"remove": 10 ** 6}
    assert _post(app, 61000008).status_code == 202
    assert _wait(lambda: _final_jobs(owner_uri))
    for _ in range(8):
        with app.app_context():
            coord.retry_uncertain_removals(dbmod.get_db(cfg))
            dbmod.close_db()
    row = _q(owner_uri, "SELECT last_error, op_owner FROM mt5_terminals")[0]
    assert row["last_error"].startswith("retry-exhausted:")
    assert row["op_owner"] == "removal-pending"
    assert _q(owner_uri, "SELECT COUNT(*) AS n FROM audit_logs WHERE"
                         " action='terminal_remove_needs_admin'")[0]["n"] >= 1
    assert fd is None


# ================================================================ S2
def test_s2_helper_mati_sinyal_setelah_remove_dipulihkan(env, tmp_path):
    """remove selesai lalu helper 'mati' (rc=-9) -> inspect (stopped_absent)
    -> remove ulang idempoten -> baris dihapus. Bukan sukses palsu: baris
    baru dihapus setelah helper sendiri menjawab already_absent."""
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    bridge.fail_start = True
    bridge.abnormal = {"remove": -9}
    assert _post(app, 61000009).status_code == 202
    assert _wait(lambda: _final_jobs(owner_uri))
    assert bridge.calls.count("remove") == 2 and "inspect" in bridge.calls
    assert _q(owner_uri, "SELECT COUNT(*) AS n FROM mt5_terminals")[0]["n"] \
        == 0


def test_s2_helper_mati_saat_lock_dipegang_fail_closed(env, tmp_path):
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    bridge.fail_start = True
    holder = {}

    def spawn_with_abnormal(argv, timeout, _orig=bridge._spawn):
        if argv[3] == "remove":
            holder["fd"] = H.acquire_lock(hcfg, "C001")   # anak helper hidup
            with bridge._mu:
                bridge.calls.append("remove")
            return -9, ""
        return _orig(argv, timeout)
    bridge._spawn = spawn_with_abnormal
    assert _post(app, 61000010).status_code == 202
    assert _wait(lambda: _final_jobs(owner_uri))
    import os
    os.close(holder["fd"])
    row = _q(owner_uri, "SELECT last_error, op_owner FROM mt5_terminals")[0]
    assert "ProvisionAbnormalExit:helper_running" in row["last_error"]
    assert row["op_owner"] == "removal-pending"
    assert bridge.calls.count("remove") == 1          # TIDAK diulang segera
    assert H.phase_of(H.paths(hcfg, "C001")) == "complete"   # tidak disentuh


def test_s2_install_mati_sinyal_tidak_pernah_connected(env, tmp_path):
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    bridge.abnormal = {"install": -15}
    r = _post(app, 61000011)
    b = r.get_json()
    assert _wait(lambda: _final_jobs(owner_uri))
    assert _poll(app, b["job_id"], b["job_secret"]).status_code == 503
    assert _q(owner_uri, "SELECT COUNT(*) AS n FROM auth_tokens")[0]["n"] == 0
    assert _invariants(owner_uri, hcfg) == []


# ================================================================ token: login
def test_token_login_akun_aktif_tidak_terbit_selama_unbind(env, tmp_path):
    """Jalur token ke-2 (login akun AKTIF, routes.py mt5_connect): selama
    unbind menghapus terminal, akun sudah 'unbound' -> tidak ada token login;
    yang terjadi hanya job reactivate yang ditolak (terminal sedang dihapus)."""
    owner_uri, cfg, app, bridge, hcfg = _mk(env, tmp_path)
    r = _post(app, 61000012)
    r = _final(app, r)
    tok = _poll  # noqa: F841
    n_tok0 = _q(owner_uri, "SELECT COUNT(*) AS n FROM auth_tokens")[0]["n"]
    with app.app_context():
        conn = dbmod.get_system_db(cfg)   # B12.1: pemanggil tepercaya = role sistem
        cid = conn.execute("SELECT customer_id FROM mt5_accounts").fetchone()[
            "customer_id"]
        import auth as authmod
        t0 = authmod.issue_token(conn, cfg, user_id=int(conn.execute(
            "SELECT id FROM users WHERE customer_id=?", (cid,)).fetchone()[
            "id"]), customer_id=int(cid), ip="203.0.113.1")
        dbmod.close_db()
    RT.SlowRunner.delay = 2.0
    out = {}

    def unbind():
        with app.test_client() as c:
            out["u"] = c.post("/api/v1/accounts/unbind",
                              headers={"Authorization": f"Bearer {t0}"})
    th = threading.Thread(target=unbind)
    th.start()
    assert _wait(lambda: bool(_q(owner_uri, "SELECT 1 FROM mt5_terminals"
                                            " WHERE lifecycle='stopping'")))
    n_mid = _q(owner_uri, "SELECT COUNT(*) AS n FROM auth_tokens WHERE"
                          " revoked_at IS NULL")[0]["n"]
    rB = _post(app, 61000012, ip="203.0.113.12")
    th.join(30)
    assert rB.status_code != 200 or rB.get_json().get("mode") != "login"
    assert _wait(lambda: _final_jobs(owner_uri))
    assert _invariants(owner_uri, hcfg) == []
    assert n_mid == 0 and n_tok0 >= 1
