"""Regresi remediasi PR #5 — /mt5/connect, auto-provisioning, sesi.

Temuan & tahap yang dicakup: T1/T2 (lewat connect), T3 (kapasitas),
T4 (cleanup milik request), T5 (master di semua jalur), S1 (fail-closed),
S2 (pesan seragam), S3 (lockout), S4 (pencabutan sesi), S5 (lifecycle
terminal), S6 (terminal_pending jujur), R1/R2/R3/R5/R6, Tahap 3 (job:
async/polling, token sekali, timeout, restart, kepemilikan).

Semua memakai MockMT5Client + MockProvisioner — tanpa broker/kredensial
nyata, tanpa Wine/systemd.
"""
import os
import tempfile
import threading
import time

import pytest
from cryptography.fernet import Fernet

import db as dbmod
from api.routes import GENERIC_VERIFY_FAIL
from app import create_app
from config import Config
from mt5client.mock import MockMT5Client
from tests.conftest import investor_dataset

SRV = "Exness-MT5Real"


def _cfg(**kw):
    c = Config()
    tmp = tempfile.mkdtemp(prefix="rem5-")
    c.db_path = f"{tmp}/journal.db"
    c.api_token = "TESTTOKEN"
    c.mt5_enabled = True
    c.cred_key = Fernet.generate_key().decode("ascii")
    c.term_base_dir = f"{tmp}/terminals"
    c.terminal_provision_mode = "mock"
    c.max_customers = 4
    c.mt5_verify_timeout_seconds = 1.0
    c.mt5_connect_sync_wait_seconds = 5.0
    c.mt5_provision_timeout_seconds = 10.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class _DynBroker(MockMT5Client):
    """Broker tiruan yang memeriksa password SAAT login (seperti broker
    sungguhan) — pemilik dapat mengganti password investor kapan saja."""

    def __init__(self, ds, broker, password, connect_delay=0.0):
        super().__init__(ds, connect_delay=connect_delay)
        self._broker = broker
        self._pw = password

    def connect(self):
        self.fail_auth = self._pw != self._broker["pw"]
        return super().connect()


def _app(cfg, *, good_pw="good", account_override=None, delay=0.0,
         master=False, trade_mode=2, provision_delay=0.0, broker=None):
    """Broker tiruan: password benar = good_pw (atau broker['pw'])."""
    broker = broker if broker is not None else {}

    def factory(acc_cfg=None):
        if acc_cfg is None:
            return MockMT5Client()
        login, server = int(acc_cfg.mt5_login), acc_cfg.mt5_server
        ds = investor_dataset(login, server, trade_mode=trade_mode)
        if account_override is not None:
            ds["account"] = account_override(login, server)
        if "pw" in broker:          # broker dinamis: cek saat connect()
            return _DynBroker(ds, broker, acc_cfg.mt5_password,
                              connect_delay=delay)
        return MockMT5Client(ds, fail_auth=acc_cfg.mt5_password != good_pw,
                             trade_allowed=True if master else None,
                             connect_delay=delay)
    app = create_app(cfg, provider_factory=factory)
    app.config["TESTING"] = True
    if provision_delay:
        reg = app.extensions["mt5_registry"]
        orig = reg.provision_terminal

        def slow(conn, cid):
            time.sleep(provision_delay)
            return orig(conn, cid)
        reg.provision_terminal = slow
    return app


def _post(app, login, password="good", server=SRV, ip="203.0.113.1",
          **headers):
    with app.test_client() as c:
        return c.post("/api/v1/mt5/connect",
                      json={"login": login, "server": server,
                            "password": password},
                      headers={"X-Real-IP": ip, **headers})


def _poll(app, job_id, secret, ip="203.0.113.1"):
    with app.test_client() as c:
        return c.post("/api/v1/mt5/connect/status",
                      json={"job_id": job_id, "job_secret": secret},
                      headers={"X-Real-IP": ip})


def _poll_until_final(app, body, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = _poll(app, body["job_id"], body["job_secret"])
        if r.status_code != 202:
            return r
        time.sleep(0.1)
    raise AssertionError("job tidak selesai")


def _n(cfg, sql, params=()):
    conn = dbmod.get_db(cfg)
    conn.rollback()
    return int(conn.execute(sql, params).fetchone()["n"])


def _no_residue(cfg):
    for t in ("customers", "users", "mt5_accounts", "mt5_credentials",
              "mt5_terminals", "auth_tokens"):
        assert _n(cfg, f"SELECT COUNT(*) AS n FROM {t}") == 0, t
    base = cfg.term_base_dir
    assert not os.path.isdir(base) or os.listdir(base) == [], \
        os.listdir(base)


# =========================================== Tahap 3: job & lifecycle
def test_job_async_polling_token_sekali_dan_tidak_ada_token_sebelum_verified():
    cfg = _cfg(mt5_connect_sync_wait_seconds=0.0)
    app = _app(cfg)
    prov = app.extensions["mt5_provisioner"]
    prov.delays = {"start": 0.6}
    r = _post(app, 10000001)
    assert r.status_code == 202, r.get_json()
    body = r.get_json()
    assert body["connection_state"] == "provisioning"
    assert "token" not in body and body["job_secret"]
    # selama job berjalan: TIDAK ada token aktif apa pun
    assert _n(cfg, "SELECT COUNT(*) AS n FROM auth_tokens") == 0
    assert _poll(app, body["job_id"], "salah").status_code == 404
    final = _poll_until_final(app, body)
    assert final.status_code == 201, final.get_json()
    assert final.get_json()["token"]
    assert final.get_json()["terminal"]["lifecycle"] == "connected"
    # token hanya SEKALI
    assert _poll(app, body["job_id"], body["job_secret"]).status_code == 410
    assert _n(cfg, "SELECT COUNT(*) AS n FROM auth_tokens") == 1
    assert [op for op, _ in prov.calls][:2] == ["install", "start"]


def test_job_timeout_saat_start_dibersihkan_tanpa_yatim():
    cfg = _cfg(mt5_provision_timeout_seconds=1.0,
               mt5_connect_sync_wait_seconds=4.0)
    app = _app(cfg)
    app.extensions["mt5_provisioner"].running_override = False
    r = _post(app, 10000002)
    assert r.status_code == 503, r.get_json()
    _no_residue(cfg)
    assert ("remove", "C001") in app.extensions["mt5_provisioner"].calls


def test_job_install_gagal_dibersihkan():
    cfg = _cfg()
    app = _app(cfg)
    app.extensions["mt5_provisioner"].fail = {"install": True}
    r = _post(app, 10000003)
    assert r.status_code == 503
    _no_residue(cfg)


def test_restart_backend_job_terputus_dipulihkan_dan_dibersihkan(monkeypatch):
    cfg = _cfg(mt5_connect_sync_wait_seconds=0.0,
               mt5_provision_timeout_seconds=10.0)
    app = _app(cfg)
    coord = app.extensions["mt5_coordinator"]
    # simulasi proses mati: worker job tidak pernah berjalan
    monkeypatch.setattr(coord, "start", lambda job_id: None)
    r = _post(app, 10000004)
    assert r.status_code == 202
    body = r.get_json()
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_accounts WHERE"
                   " state='pending'") == 1
    conn = dbmod.get_db(cfg)
    conn.execute("UPDATE mt5_connect_jobs SET deadline_at=?",
                 (time.time() - 1,))
    conn.commit()
    dbmod.close_db()
    app2 = _app(cfg)            # restart: pemulihan saat startup
    r = _poll(app2, body["job_id"], body["job_secret"])
    assert r.status_code == 503, r.get_json()
    _no_residue(cfg)


def test_remove_tanpa_bukti_kepemilikan_tidak_menghapus():
    cfg = _cfg()
    app = _app(cfg)
    prov = app.extensions["mt5_provisioner"]
    prov.fail = {"start": True}
    base = os.path.join(cfg.term_base_dir, "C001")
    # direktori pihak lain (penanda berbeda) sudah ada di path customer
    os.makedirs(base)
    with open(os.path.join(base, ".provision_id"), "w") as f:
        f.write("f" * 32)
    r = _post(app, 10000005)
    assert r.status_code == 503
    # install menolak (bukan milik job) -> TIDAK dihapus; slot ditahan
    assert os.path.isfile(os.path.join(base, ".provision_id"))
    row = dbmod.get_db(cfg).execute(
        "SELECT lifecycle FROM mt5_terminals").fetchone()
    assert row is not None and row["lifecycle"] == "failed"
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_accounts") == 0


def test_satu_customer_satu_terminal_saat_connect_identitas_sama_bersamaan():
    cfg = _cfg()
    app = _app(cfg, delay=0.3)
    res = []
    barrier = threading.Barrier(4)

    def w(i):
        barrier.wait()
        res.append(_post(app, 10000006, ip=f"198.51.100.{i}").status_code)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert res.count(201) == 1, res
    assert set(res) <= {201, 409, 401}, res
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 1
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_accounts") == 1
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 1


def test_empat_slot_penuh_connect_bersamaan_tanpa_yatim():
    cfg = _cfg(max_customers=4)
    app = _app(cfg, delay=0.2)
    res = []
    barrier = threading.Barrier(7)

    def w(i):
        barrier.wait()
        res.append(_post(app, 20000000 + i, ip=f"198.51.101.{i}"
                         ).status_code)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(7)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(res) == [201] * 4 + [403] * 3, res
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 4
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 4
    assert len(os.listdir(cfg.term_base_dir)) == 4


def test_registrasi_dan_provisioning_bersamaan():
    cfg = _cfg(max_customers=2)
    app = _app(cfg, delay=0.2)
    res = {"reg": [], "con": []}
    barrier = threading.Barrier(6)

    def reg(i):
        barrier.wait()
        with app.test_client() as c:
            res["reg"].append(c.post(
                "/api/v1/auth/register",
                json={"email": f"r{i}@t.id", "password": "rahasia123"},
                headers={"X-Real-IP": f"10.8.{i}.1"}).status_code)

    def con(i):
        barrier.wait()
        res["con"].append(_post(app, 30000000 + i,
                                ip=f"10.9.{i}.1").status_code)

    ts = [threading.Thread(target=reg, args=(i,)) for i in range(3)] + \
         [threading.Thread(target=con, args=(i,)) for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert res["reg"] == [201] * 3, res      # registrasi tidak terkunci
    assert sorted(res["con"]) == [201, 201, 403], res
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 2


# ===================================================== S1 fail-closed
@pytest.mark.parametrize("override,expect", [
    (lambda login, server: None, 503),
    (lambda login, server: {"login": login, "server": server,
                            "trade_mode": 2}, 503),
    (lambda login, server: {"login": login + 1, "server": server,
                            "trade_allowed": False, "trade_mode": 2}, 401),
    (lambda login, server: {"login": login, "server": "Lain-Srv",
                            "trade_allowed": False, "trade_mode": 2}, 401),
    (lambda login, server: {"login": login, "server": server,
                            "trade_allowed": None, "trade_mode": 2}, 401),
    (lambda login, server: {"login": login, "server": server,
                            "trade_allowed": False}, 503),
])
def test_s1_verifikasi_fail_closed(override, expect):
    cfg = _cfg()
    app = _app(cfg, account_override=override)
    r = _post(app, 10101010)
    assert r.status_code == expect, r.get_json()
    assert "token" not in (r.get_json() or {})
    _no_residue(cfg)


def test_s1_unit_klasifikasi():
    from api.routes import _classify_account_info as k
    ok = {"login": 1, "server": "S", "trade_allowed": False, "trade_mode": 2}
    kw = dict(enforce_investor_readonly=True, expected_login=1,
              expected_server="s", require_real=True)
    assert k(ok, **kw) == "ok"
    assert k(None, **kw) == "unverified"
    assert k({}, **kw) == "unverified"
    assert k({**ok, "trade_allowed": None}, **kw) == "master_password"
    assert k({**ok, "trade_allowed": True}, **kw) == "master_password"
    assert k({k2: v for k2, v in ok.items() if k2 != "trade_allowed"},
             **kw) == "unverified"
    assert k({**ok, "trade_mode": 0}, **kw) == "not_real"
    assert k({k2: v for k2, v in ok.items() if k2 != "trade_mode"},
             **kw) == "unverified"
    assert k({**ok, "login": 2}, **kw) == "identity_mismatch"


# ============================================= T5: master di semua jalur
def test_t5_master_ditolak_connect_akun_baru():
    cfg = _cfg()
    r = _post(_app(cfg, master=True), 20202020)
    assert r.status_code == 401 and "MASTER" in r.get_json()["error"]["message"]
    _no_residue(cfg)


def test_t5_master_ditolak_bind_dengan_mt5_aktif():
    from tests.conftest import register_and_login
    cfg = _cfg()
    app = _app(cfg, master=True)
    c = app.test_client()
    h = register_and_login(c, "bm@t.id")
    r = c.post("/api/v1/accounts/bind", headers=h,
               json={"login": 20202021, "server": SRV, "password": "good"})
    assert r.status_code == 401, r.get_json()
    assert "MASTER" in r.get_json()["error"]["message"]
    for t in ("mt5_accounts", "mt5_credentials", "mt5_terminals"):
        assert _n(cfg, f"SELECT COUNT(*) AS n FROM {t}") == 0, t


def test_t5_master_tersimpan_lama_ditolak_saat_reconnect_dan_sesi_dicabut():
    from tests.conftest import register_and_login
    cfg = _cfg(mt5_enabled=False)
    app = _app(cfg, master=True)
    c = app.test_client()
    h = register_and_login(c, "old@t.id")
    assert c.post("/api/v1/accounts/bind", headers=h,
                  json={"login": 20202023, "server": SRV,
                        "password": "good"}).status_code == 201
    cfg.mt5_enabled = True
    r = _post(app, 20202023)
    assert r.status_code == 401 and "MASTER" in r.get_json()["error"]["message"]
    assert c.get("/api/v1/auth/me", headers=h).status_code == 401
    row = dbmod.get_db(cfg).execute("SELECT state FROM mt5_accounts"
                                    ).fetchone()
    assert row["state"] == "credentials_rejected"
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_credentials") == 0


# ===================================================== T3: kapasitas
def test_t3_akun_demo_tidak_bisa_menduduki_slot():
    cfg = _cfg(max_customers=1)
    r = _post(_app(cfg, trade_mode=0), 30303030)
    assert r.status_code == 403 and "REAL" in r.get_json()["error"]["message"]
    _no_residue(cfg)
    assert _post(_app(cfg), 30303031).status_code == 201


def test_t3_allowlist_server():
    cfg = _cfg(mt5_self_link_server_allowlist=("Exness-MT5Real*",))
    app = _app(cfg)
    r = _post(app, 30303032, server="BrokerLain-MT5Demo")
    assert r.status_code == 401
    assert r.get_json()["error"]["message"] == GENERIC_VERIFY_FAIL
    _no_residue(cfg)
    assert _post(app, 30303033, server="exness-mt5real7").status_code == 201


def test_t3_registrasi_massal_tidak_menghabiskan_slot_mt5():
    cfg = _cfg(max_customers=1)
    app = _app(cfg)
    with app.test_client() as c:
        for i in range(5):
            assert c.post("/api/v1/auth/register",
                          json={"email": f"m{i}@t.id",
                                "password": "rahasia123"},
                          headers={"X-Real-IP": f"10.10.{i}.1"}
                          ).status_code == 201
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 0
    assert _post(app, 30303040).status_code == 201   # slot masih tersedia


# ===================================== T1 lewat connect: gagal+sukses
def test_t1_connect_gagal_dan_sukses_bersamaan_tidak_memblokir_registrasi():
    cfg = _cfg()
    app = _app(cfg, delay=0.6)
    res = {}
    t = threading.Thread(target=lambda: res.update(
        a=_post(app, 40404040, password="salah", ip="198.51.100.1")))
    t.start()
    time.sleep(0.2)
    res["b"] = _post(app, 40404041, ip="198.51.100.2")
    t.join()
    assert res["a"].status_code == 401
    assert res["b"].status_code == 201
    for i, login in enumerate((40404042, 40404043)):
        r = _post(app, login, ip=f"198.51.100.{10 + i}")
        assert r.status_code == 201, r.get_json()


# ======================================== T4: cleanup milik request saja
def test_t4_penyerang_tidak_bisa_menghapus_terminal_pemilik():
    cfg = _cfg()
    app = _app(cfg, provision_delay=0.4)
    res = {}
    t1 = threading.Thread(target=lambda: res.update(
        r1=_post(app, 50505050, ip="198.51.100.20")))
    t1.start()
    time.sleep(0.15)
    res["r2"] = _post(app, 50505050, password="salah", ip="198.51.100.21")
    t1.join()
    assert res["r1"].status_code == 201, res["r1"].get_json()
    assert res["r2"].status_code in (401, 409), res["r2"].get_json()
    conn = dbmod.get_db(cfg)
    conn.rollback()
    term = conn.execute("SELECT terminal_dir, lifecycle FROM mt5_terminals"
                        ).fetchone()
    assert term is not None and term["lifecycle"] == "connected"
    assert os.path.isdir(os.path.dirname(term["terminal_dir"]))
    h = {"Authorization": f"Bearer {res['r1'].get_json()['token']}"}
    with app.test_client() as c:
        assert c.get("/api/v1/mt5/status", headers=h).get_json()[
            "connected"] is True


def test_t4_cleanup_job_tidak_menyentuh_terminal_yang_tidak_dibuatnya():
    cfg = _cfg()
    app = _app(cfg)
    assert _post(app, 50505051).status_code == 201
    conn = dbmod.get_db(cfg)
    coord = app.extensions["mt5_coordinator"]
    t = conn.execute("SELECT id FROM mt5_terminals").fetchone()
    job = {"id": "x", "customer_id": None, "account_id": None,
           "created_account": 0, "created_terminal": 0,
           "terminal_id": t["id"], "provision_id": None,
           "created_customer": 0, "prev_account_state": None}
    coord.cleanup(conn, job)
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 1
    # provision_id SALAH -> remove_terminal menolak menghapus
    assert coord.remove_terminal(conn, int(t["id"]), "0" * 32) is False
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 1


# ===================================================== T2 lewat connect
def test_t2_spoof_xff_tidak_melewati_rate_limit_connect():
    cfg = _cfg()
    app = _app(cfg)
    codes = []
    for i in range(12):
        with app.test_client() as c:
            r = c.post("/api/v1/mt5/connect",
                       json={"login": 60606000 + i, "server": SRV,
                             "password": "salah"},
                       headers={"X-Forwarded-For": f"10.{i}.{i}.{i}"})
        codes.append(r.status_code)
    assert codes[:10] == [401] * 10, codes
    assert codes[10:] == [429, 429], codes
    _no_residue(cfg)


# ===================================================== S2: anti-enumerasi
def test_s2_pesan_seragam_terdaftar_vs_tidak_vs_varian_kapital():
    cfg = _cfg()
    app = _app(cfg)
    assert _post(app, 70707070).status_code == 201
    known = _post(app, 70707070, password="salah", ip="203.0.113.50")
    unknown = _post(app, 70707071, password="salah", ip="203.0.113.51")
    variant = _post(app, 70707070, password="salah", server=SRV.lower(),
                    ip="203.0.113.52")
    for r in (known, unknown, variant):
        assert r.status_code == 401
        assert r.get_json()["error"]["message"] == GENERIC_VERIFY_FAIL
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 1


# ======================================================= S3: lockout
def test_s3_penyerang_dari_ip_lain_tidak_mengunci_pemilik():
    cfg = _cfg()
    app = _app(cfg)
    assert _post(app, 80808080, ip="203.0.113.60").status_code == 201
    for _ in range(5):
        _post(app, 80808080, password="salah", ip="198.51.100.66")
    assert _post(app, 80808080, password="salah",
                 ip="198.51.100.66").status_code == 429
    assert _post(app, 80808080, ip="203.0.113.60").status_code == 200


def test_s3_brute_force_terdistribusi_dibatasi_dan_bertahan_restart():
    cfg = _cfg()
    app = _app(cfg)
    assert _post(app, 80808081, ip="203.0.113.61").status_code == 201
    for i in range(4 * cfg.max_failed_logins):
        _post(app, 80808081, password=f"salah{i}", ip=f"10.1.{i}.1")
    dbmod.close_db()
    app2 = _app(cfg)             # restart: lockout persisten di DB
    assert _post(app2, 80808081, ip="10.2.0.1").status_code == 429


# ============================================== S4: sesi & broker menolak
def test_s4_disconnect_all_mencabut_semua_perangkat():
    cfg = _cfg()
    app = _app(cfg)
    t1 = _post(app, 90909090).get_json()["token"]
    t2 = _post(app, 90909090, ip="203.0.113.70").get_json()["token"]
    with app.test_client() as c:
        h1 = {"Authorization": f"Bearer {t1}"}
        h2 = {"Authorization": f"Bearer {t2}"}
        r = c.post("/api/v1/mt5/disconnect", headers=h1, json={"all": True})
        assert r.get_json()["revoked"] == "all"
        assert c.get("/api/v1/auth/me", headers=h2).status_code == 401
        assert c.get("/api/v1/auth/me", headers=h1).status_code == 401
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_accounts") == 1


def test_s4_disconnect_default_hanya_sesi_ini():
    cfg = _cfg()
    app = _app(cfg)
    t1 = _post(app, 90909091).get_json()["token"]
    t2 = _post(app, 90909091, ip="203.0.113.71").get_json()["token"]
    with app.test_client() as c:
        c.post("/api/v1/mt5/disconnect",
               headers={"Authorization": f"Bearer {t1}"})
        assert c.get("/api/v1/auth/me", headers={
            "Authorization": f"Bearer {t2}"}).status_code == 200


def test_s4_broker_menolak_lalu_reconnect_password_baru():
    cfg = _cfg()
    broker = {"pw": "good"}
    app = _app(cfg, broker=broker)
    t1 = _post(app, 91919191).get_json()["token"]
    t2 = _post(app, 91919191, ip="203.0.113.80").get_json()["token"]
    with app.test_client() as c:
        h1 = {"Authorization": f"Bearer {t1}"}
        assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                      headers=h1).get_json()["status"] == "ok"
    deals = _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals")
    broker["pw"] = "baru-1"           # pemilik ganti password investor
    r = _post(app, 91919191, password="good", ip="203.0.113.81")
    assert r.status_code == 401
    assert "tidak lagi diterima" in r.get_json()["error"]["message"]
    with app.test_client() as c:
        for t in (t1, t2):
            assert c.get("/api/v1/auth/me", headers={
                "Authorization": f"Bearer {t}"}).status_code == 401
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_credentials") == 0
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals") == deals
    # password LAMA tidak bisa memulihkan; password BARU (live) bisa
    assert _post(app, 91919191, password="good",
                 ip="203.0.113.82").status_code == 401
    r = _post(app, 91919191, password="baru-1", ip="203.0.113.83")
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["mode"] == "reactivate" and r.get_json()["token"]
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals") == deals


def test_s4_unbind_lalu_reconnect_riwayat_tetap_dan_slot_dibebaskan():
    cfg = _cfg(max_customers=1)
    app = _app(cfg)
    tok = _post(app, 92929292).get_json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    with app.test_client() as c:
        assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                      headers=h).get_json()["status"] == "ok"
        deals = _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals")
        r = c.post("/api/v1/accounts/unbind", headers=h)
        assert r.get_json()["terminal_released"] is True
        assert c.get("/api/v1/auth/me", headers=h).status_code == 401
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 0
    # slot bebas untuk customer lain
    assert _post(app, 92929293, ip="203.0.113.90").status_code == 201
    # reaktivasi akun lama: slot penuh -> 403, riwayat tetap utuh
    assert _post(app, 92929292).status_code == 403
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals WHERE account_id="
                   "(SELECT id FROM mt5_accounts WHERE login=92929292)") \
        == deals


# =================================================== S6: status jujur
def test_s6_reconnect_terminal_mati_terminal_pending_bukan_connected():
    cfg = _cfg()
    app = _app(cfg)
    assert _post(app, 93939393).status_code == 201
    reg = app.extensions["mt5_registry"]
    conn = dbmod.get_db(cfg)
    acc_id = int(conn.execute("SELECT id FROM mt5_accounts").fetchone()["id"])
    reg.evict(acc_id)
    rt = reg.runtime_for(conn, acc_id)
    rt.provider.fail_connect = True
    r = _post(app, 93939393, ip="203.0.113.91")
    assert r.status_code == 200
    body = r.get_json()
    assert body["connection_state"] == "terminal_pending"
    assert body["live_verified"] is False


# ============================================================ R1/R2/R5/R6
def test_r1_response_connect_tanpa_metadata_internal():
    cfg = _cfg()
    body = _post(_app(cfg), 12121212).get_json()
    assert "customer_code" not in body
    assert set(body["terminal"].keys()) <= {"status", "lifecycle"}
    for leak in ("rpyc", "provision_id", "wine", "service_name"):
        assert leak not in repr(body), leak


def test_r2_marker_auth_spesifik():
    from api.routes import _looks_like_broker_auth_error as f
    assert f("MT5 initialize gagal (-6, 'Terminal: Authorization failed')")
    assert not f("[Errno 13] Permission denied: '/var/lib/x'")
    assert not f("password file missing")


def test_r5_lock_dipegang_thread_verifikasi_sampai_selesai():
    from api.routes import _live_verify_account
    cfg = _cfg()
    app = _app(cfg)
    assert _post(app, 13131313).status_code == 201
    conn = dbmod.get_db(cfg)
    reg = app.extensions["mt5_registry"]
    acc_id = int(conn.execute("SELECT id FROM mt5_accounts"
                              ).fetchone()["id"])
    rt = reg.runtime_for(conn, acc_id)
    rt.provider.connect_delay = 0.8
    ver = _live_verify_account(conn, reg, acc_id, timeout=0.2)
    assert ver["class"] == "timeout"
    assert rt.lock.locked()
    time.sleep(1.2)
    assert not rt.lock.locked()


def test_r6_email_sintetis_tanpa_tabrakan_potongan_slug():
    from api.routes import _synthetic_email
    a = _synthetic_email(1, "X" * 60 + "-A")
    b = _synthetic_email(1, "X" * 60 + "-B")
    assert a != b
    assert _synthetic_email(1, "Exness-Real") == \
        _synthetic_email(1, "exness-real")


def test_static_seluruh_backend_tanpa_pemanggilan_mt5_tulis():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    bad = {"order_send", "order_check", "order_calc_margin",
           "order_calc_profit", "positions_close", "positions_close_by",
           "positions_modify", "order_modify", "order_delete", "order_close"}
    for p in root.rglob("*.py"):
        if "tests" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else \
                    getattr(f, "id", None)
                assert name not in bad, (str(p), name)


def test_batas_portal_penuh_tidak_menghalangi_self_link_mt5():
    """Banjir registrasi portal (batas akun portal penuh) TIDAK boleh
    menghalangi aktivasi MT5: self-link dibatasi slot terminal saja."""
    cfg = _cfg(max_registered_customers=2, max_customers=2)
    app = _app(cfg)
    with app.test_client() as c:
        codes = [c.post("/api/v1/auth/register",
                        json={"email": f"f{i}@t.id",
                              "password": "rahasia123"},
                        headers={"X-Real-IP": f"10.20.{i}.1"}).status_code
                 for i in range(4)]
    assert codes == [201, 201, 403, 403], codes
    assert _post(app, 15151515).status_code == 201
    assert _post(app, 15151516, ip="203.0.113.99").status_code == 201
    assert _post(app, 15151517, ip="203.0.113.98").status_code == 403
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 2


def test_mode_rpc_per_terminal_migrasi_bertahap():
    """Terminal lama tetap 'classic' (kompatibel); terminal baru mengikuti
    MT5_RPC_MODE_DEFAULT; provider dipilih PER TERMINAL."""
    cfg = _cfg()
    app = _app(cfg)
    assert _post(app, 16161601).status_code == 201
    cfg.mt5_rpc_mode_default = "ro"
    cfg.mt5_rpc_secret = "s" * 40
    assert _post(app, 16161602, ip="203.0.113.97").status_code == 201
    conn = dbmod.get_db(cfg)
    modes = {r["login"]: r["rpc_mode"] for r in conn.execute(
        "SELECT a.login, t.rpc_mode FROM mt5_accounts a JOIN mt5_terminals t"
        " ON t.customer_id=a.customer_id")}
    assert modes == {16161601: "classic", 16161602: "ro"}
    reg = app.extensions["mt5_registry"]
    ids = {r["login"]: r["id"] for r in conn.execute(
        "SELECT id, login FROM mt5_accounts")}
    a = reg._account_config(conn, ids[16161601])
    b = reg._account_config(conn, ids[16161602])
    assert a.mt5_provider == "mt5linux" and a.mt5_token is None
    assert b.mt5_provider == "mt5ro" and len(b.mt5_token) == 64


def test_db_dump_byte_tanpa_password_investor_atau_token_mentah():
    """Seluruh byte berkas DB (semua tabel + WAL) setelah alur lengkap:
    connect, gagal, reaktivasi, sync, unbind — password investor &
    token sesi mentah TIDAK pernah tersimpan plaintext."""
    cfg = _cfg()
    broker = {"pw": "INV-PLAIN-777"}
    app = _app(cfg, broker=broker)
    tok = _post(app, 17171717, password="INV-PLAIN-777").get_json()["token"]
    _post(app, 17171717, password="SALAH-PLAIN-888", ip="203.0.113.60")
    with app.test_client() as c:
        h = {"Authorization": f"Bearer {tok}"}
        c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=h)
        c.post("/api/v1/accounts/unbind", headers=h)
    r = _post(app, 17171717, password="INV-PLAIN-777", ip="203.0.113.61")
    tok2 = r.get_json()["token"]
    conn = dbmod.get_db(cfg)
    conn.commit()
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        p = cfg.db_path + suffix
        if os.path.exists(p):
            blob += open(p, "rb").read()
    for secret in ("INV-PLAIN-777", "SALAH-PLAIN-888", tok, tok2):
        assert secret.encode() not in blob, secret
    enc = conn.execute("SELECT enc_password FROM mt5_credentials").fetchone()
    assert enc["enc_password"].startswith("gAAAA")    # Fernet
