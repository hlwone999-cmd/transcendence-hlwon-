"""Regresi redesign Jurnal MT5 — self-link /mt5/connect + /mt5/disconnect.

Cakupan (syarat implementasi #10): seluruh alur login (connect), logout
(disconnect), isolasi customer, dan read-only:

- D1 : identitas = nomor akun MT5 + server + password investor; tidak ada
       email/password portal/token manual di jalur ini.
- D2 : self-link otomatis dengan batas max_customers customer aktif,
       capacity guard aman pendaftaran bersamaan, UNIQUE(login, server),
       rate limit, lockout, audit, dan flag nonaktif admin.
- D3 : /mt5/disconnect HANYA mencabut token sesi — riwayat jurnal tidak
       terhapus; unbind tetap hanya via /accounts/unbind.
- Keamanan: password investor tidak pernah plaintext di DB/audit/log;
  kredensial tersimpan terenkripsi Fernet.

VERIFIKASI LIVE (hardening post-redesign, requirement 1-8):
- Akun tidak dinyatakan TERHUBUNG sebelum kredensial terverifikasi
  terminal MT5 (login nyata via provider read-only, berbatas waktu).
- Akun BARU: verifikasi live SEBELUM aktivasi; kegagalan (password
  salah, server tidak cocok, terminal/broker mati, timeout, password
  master terdeteksi) membersihkan SEMUA jejak — slot customer bebas
  kembali, tanpa baris yatim.
- Koneksi ulang akun lama tidak cukup membandingkan password
  terenkripsi: broker ikut dikonsultasikan; broker menolak -> 401
  tanpa token; terminal mati -> sesi terbit tapi status
  terminal_pending (BIARKAN riwayat tetap terbaca — D3).
- Password master terdeteksi via account_info().trade_allowed —
  MetaTrader5 tidak membedakan master vs investor secara eksplisit.

Semua test memakai MockMT5Client (read-only provider) tanpa MT5 nyata.
"""
import json
import logging
import threading

from cryptography.fernet import Fernet

from app import create_app
from config import Config
from mt5client.mock import (AccountInfo, MockMT5Client, SymbolInfo, _deal,
                            _order)
from mt5client.provider import ReadOnlyViolationError

SERVER_A = "Exness-MT5Real"
SERVER_B = "BrokerLain-MT5Demo"
LOGIN_A = 11111111
LOGIN_B = 22222222
PW_INVESTOR = "inv-secret-9"


def _acc(login, server, currency="USD"):
    # trade_allowed=False = sesi INVESTOR read-only (semestinya). Sesi
    # password master akan trade_allowed=True — diuji eksplisit lewat knob
    # master_mode pada _client (deteksi password master).
    return AccountInfo(
        login=login, trade_mode=2, leverage=100, limit_orders=200,
        margin_so_mode=0, trade_allowed=False, trade_expert=False,
        margin_mode=0, currency_digits=2, fifo_close=False,
        balance=1000.0, credit=0.0, profit=0.0, equity=1000.0,
        margin=0.0, margin_free=1000.0, margin_level=0.0,
        margin_so_call=50.0, margin_so_so=20.0, margin_initial=0.0,
        margin_maintenance=0.0, assets=0.0, liabilities=0.0,
        commission_blocked=False, name="Uji", email="t@t.id",
        server=server, currency=currency, company=server.split("-")[0])


def _ds_trades(login, server, symbol, point, digits, pid, profit):
    """Dataset 1 posisi tertutup (2 deal) — deterministik."""
    deals = [
        _deal(pid * 10, pid * 10, 10, 0, 0, pid, 0.1, 2000.0,
              commission=-2.0, symbol=symbol),
        _deal(pid * 10 + 1, pid * 10 + 1, 20, 1, 1, pid, 0.1, 2010.0,
              profit=profit, commission=-2.0, symbol=symbol),
    ]
    orders = [_order(pid * 10, 10, 0, pid, 0.1, 2000.0, 1990.0, 2050.0,
                     symbol=symbol)]
    return {"account": _acc(login, server), "deals": deals,
            "orders": orders, "positions": [],
            "symbols": {symbol: SymbolInfo(
                name=symbol, point=point, digits=digits,
                trade_tick_size=point, trade_tick_value=1.0,
                trade_contract_size=100.0, description=symbol)}}


def _make_cfg(max_customers=4, self_link=True, db_path=None,
              verify_timeout=1.0, cred_key=None):
    """Config test; [db_path] memungkinkan DUA app berbagi satu DB
    (dipakai test reconnect: app pertama terminal hidup, kedua mati).
    [cred_key] wajib dibagi antar app yang berbagi DB — tanpa itu app
    kedua tidak bisa mendekripsi kredensial yang ditulis app pertama."""
    cfg = Config()
    import tempfile
    tmp = tempfile.mkdtemp(prefix="connect-")
    cfg.db_path = db_path or f"{tmp}/journal.db"
    cfg.api_token = "TESTTOKEN"
    cfg.mt5_enabled = True
    cfg.cred_key = cred_key or Fernet.generate_key().decode("ascii")
    cfg.term_base_dir = f"{tmp}/terminals"
    cfg.terminal_provision_mode = "mock"
    cfg.max_customers = max_customers
    cfg.mt5_self_link_enabled = self_link
    # verifikasi live cepat di test (produksi: 12s via env)
    cfg.mt5_verify_timeout_seconds = verify_timeout
    return cfg


def _client(datasets=None, max_customers=4, self_link=True, cfg=None,
            fail_connect=False, fail_auth=False, master_mode=False,
            connect_delay=0.0):
    """App test client dengan mock provider per login akun.

    Knob verifikasi live: fail_connect (terminal/broker mati), fail_auth
    (broker menolak kredensial), master_mode (sesi trade-capable —
    password master), connect_delay (provider lambat -> timeout).
    [cfg] dari _make_cfg memungkinkan berbagi DB antar app.
    """
    if cfg is None:
        cfg = _make_cfg(max_customers=max_customers, self_link=self_link)

    def factory(acc_cfg=None):
        ds = None
        if datasets is not None and acc_cfg is not None:
            ds = datasets.get(str(acc_cfg.mt5_login))
        if ds is None and acc_cfg is not None:
            # broker tiruan menyajikan akun yang SAMA dengan yang diminta
            # (verifikasi fail-closed mencocokkan login & server).
            ds = {"account": _acc(int(acc_cfg.mt5_login),
                                  acc_cfg.mt5_server),
                  "deals": [], "orders": [], "positions": [], "symbols": {}}
        return MockMT5Client(
            ds,
            fail_connect=fail_connect,
            fail_auth=fail_auth,
            trade_allowed=True if master_mode else False,
            connect_delay=connect_delay)

    app = create_app(cfg, provider_factory=factory)
    app.config["TESTING"] = True
    return app.test_client()


def _db_conn(app):
    """Koneksi DB test dari thread pytest (path sama dengan app)."""
    import db as dbmod
    return dbmod.get_db(app.config["APP_CFG"])


def _connect(c, login=LOGIN_A, server=SERVER_A, password=PW_INVESTOR):
    return c.post("/api/v1/mt5/connect",
                  json={"login": login, "server": server,
                        "password": password})


# ------------------------------------------------------------- D1: connect
def test_connect_akun_baru_membuat_customer_akun_sesi():
    c = _client({str(LOGIN_A): _ds_trades(LOGIN_A, SERVER_A, "XAUUSD",
                                          0.01, 2, 11, 100.0)})
    r = _connect(c)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["mode"] == "self_link"
    assert body["account"]["login"] == LOGIN_A
    assert body["account"]["server"] == SERVER_A
    assert body["token"]

    # token sesi langsung dipakai akses jurnal — alur login utuh
    h = {"Authorization": f"Bearer {body['token']}"}
    me = c.get("/api/v1/auth/me", headers=h)
    assert me.status_code == 200
    assert me.get_json()["account"]["login"] == LOGIN_A
    # sinkron penuh (di produksi sync awal berjalan background otomatis)
    assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                  headers=h).status_code == 200
    tr = c.get("/api/v1/journal/trades", headers=h)
    assert tr.status_code == 200
    assert tr.get_json()["total"] == 1


def test_connect_kedua_sama_akun_mode_login_tanpa_duplikasi():
    c = _client()
    b1 = _connect(c).get_json()
    assert b1["mode"] == "self_link"
    b2 = _connect(c)  # kredensial sama -> kenali customer, keluarkan sesi
    assert b2.status_code == 200
    assert b2.get_json()["mode"] == "login"

    h1 = {"Authorization": f"Bearer {b1['token']}"}
    h2 = {"Authorization": f"Bearer {b2.get_json()['token']}"}
    a1 = c.get("/api/v1/auth/me", headers=h1).get_json()
    a2 = c.get("/api/v1/auth/me", headers=h2).get_json()
    assert a1["customer"]["customer_id"] == a2["customer"]["customer_id"]

    # tidak ada duplikasi baris akun (UNIQUE(login, server)) —
    # daftar admin menampilkan bound_login per customer
    rows = c.get("/api/v1/admin/customers",
                 headers={"X-API-Token": "TESTTOKEN"}).get_json()["items"]
    logins = [r["bound_login"] for r in rows if r["bound_login"]]
    assert logins.count(LOGIN_A) == 1


def test_connect_password_salah_401_pesan_seragam_lalu_lockout():
    c = _client()
    assert _connect(c).status_code == 201
    for i in range(5):
        r = _connect(c, password=f"salah-{i}")
        assert r.status_code == 401
        # pesan seragam (remediasi S2) — identik dgn akun TIDAK terdaftar
        from api.routes import GENERIC_VERIFY_FAIL
        assert r.get_json()["error"]["message"] == GENERIC_VERIFY_FAIL
    # lockout aktif setelah batas kegagalan (cfg.max_failed_logins=5)
    r = _connect(c, password=PW_INVESTOR)  # password BENAR pun ditolak
    assert r.status_code == 429


def test_connect_kapasitas_penuh_403_customer_lama_masih_bisa_login():
    c = _client(max_customers=2)
    assert _connect(c, login=LOGIN_A, server=SERVER_A).status_code == 201
    assert _connect(c, login=LOGIN_B, server=SERVER_B).status_code == 201
    # slot habis -> akun baru ditolak 403 kapasitas
    r = _connect(c, login=33333333, server="Exness-MT5Trial")
    assert r.status_code == 403
    assert "kapasitas" in r.get_json()["error"]["message"].lower()
    # customer lama tetap bisa login (bukan kapasitas baru)
    r = _connect(c, login=LOGIN_A, server=SERVER_A)
    assert r.status_code == 200


def test_connect_self_link_dinonaktifkan_admin_403():
    c = _client(self_link=False)
    r = _connect(c)
    assert r.status_code == 403
    assert "dinonaktifkan" in r.get_json()["error"]["message"]


def test_connect_input_tidak_valid_400():
    c = _client()
    assert c.post("/api/v1/mt5/connect",
                  json={"server": SERVER_A,
                        "password": "x"}).status_code == 400
    assert c.post("/api/v1/mt5/connect",
                  json={"login": "bukan-angka", "server": SERVER_A,
                        "password": "x"}).status_code == 400
    assert c.post("/api/v1/mt5/connect",
                  json={"login": LOGIN_A, "server": "",
                        "password": "x"}).status_code == 400
    assert c.post("/api/v1/mt5/connect",
                  json={"login": LOGIN_A, "server": SERVER_A,
                        "password": ""}).status_code == 400


def test_connect_bersamaan_slot_terakhir_hanya_satu_sukses():
    """D2: pendaftaran bersamaan pada slot terakhir tidak dobel (guard)."""
    c = _client(max_customers=2)
    assert _connect(c, login=LOGIN_A, server=SERVER_A).status_code == 201
    results = []
    barrier = threading.Barrier(2)
    c1 = c.application.test_client()  # klien per-thread
    c2 = c.application.test_client()

    def worker(cl, login):
        barrier.wait()
        r = cl.post("/api/v1/mt5/connect",
                    json={"login": login, "server": "Exness-MT5Trial8",
                          "password": "pw-rahasia"})
        results.append(r.status_code)

    t1 = threading.Thread(target=worker, args=(c1, 33333333))
    t2 = threading.Thread(target=worker, args=(c2, 44444444))
    t1.start(); t2.start(); t1.join(); t2.join()
    # tepat satu yang mendapat slot terakhir; yang lain 403 kapasitas
    assert sorted(results) == [201, 403], results
    rows = c.get("/api/v1/admin/customers",
                 headers={"X-API-Token": "TESTTOKEN"}).get_json()["items"]
    bound = [r["bound_login"] for r in rows if r["bound_login"]]
    assert len(bound) == 2 and LOGIN_A in bound


# ------------------------------------------------- D3: disconnect / logout
def test_disconnect_mencabut_sesi_riwayat_tidak_terhapus():
    c = _client({str(LOGIN_A): _ds_trades(LOGIN_A, SERVER_A, "XAUUSD",
                                          0.01, 2, 11, 100.0)})
    tok = _connect(c).get_json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                  headers=h).status_code == 200
    assert c.get("/api/v1/journal/trades",
                 headers=h).get_json()["total"] == 1

    # PUTUSKAN KONEKSI
    r = c.post("/api/v1/mt5/disconnect", headers=h)
    assert r.status_code == 200 and r.get_json()["ok"] is True

    # token dicabut -> akses jurnal 401
    assert c.get("/api/v1/journal/trades", headers=h).status_code == 401
    # disconnect kedua dengan token basi -> 401 juga
    assert c.post("/api/v1/mt5/disconnect", headers=h).status_code == 401

    # connect ulang (login) -> riwayat jurnal MASIH ADA (tidak dihapus)
    tok2 = _connect(c).get_json()["token"]
    h2 = {"Authorization": f"Bearer {tok2}"}
    tr = c.get("/api/v1/journal/trades", headers=h2)
    assert tr.status_code == 200
    assert tr.get_json()["total"] == 1  # riwayat utuh
    # akun & terminal tidak ikut terhapus (bukan unbind)
    assert c.get("/api/v1/mt5/account", headers=h2).status_code == 200


def test_unbind_tidak_ada_di_jalur_disconnect():
    """D3: unbind tetap hanya via /accounts/unbind (API/admin); connect
    ulang setelah unbind memakai ulang identitas customer yang sama."""
    c = _client()
    tok = _connect(c).get_json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    cid_before = c.get("/api/v1/auth/me",
                       headers=h).get_json()["customer"]["customer_id"]
    # disconnect tidak menghapus akun
    c.post("/api/v1/mt5/disconnect", headers=h)
    tok2 = _connect(c).get_json()["token"]
    h2 = {"Authorization": f"Bearer {tok2}"}
    assert c.get("/api/v1/mt5/account", headers=h2).status_code == 200
    # unbind memang masih ada sebagai API (di luar UI) dan bekerja
    assert c.post("/api/v1/accounts/unbind", headers=h2).status_code == 200
    # setelah unbind, connect dgn kredensial sama = REAKTIVASI akun yang
    # sama (keputusan produk #4: riwayat dipertahankan), tetap pada
    # customer yang SAMA (identitas tidak digandakan)
    r = _connect(c)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["mode"] == "reactivate"
    tok3 = r.get_json()["token"]
    h3 = {"Authorization": f"Bearer {tok3}"}
    cid_after = c.get("/api/v1/auth/me",
                      headers=h3).get_json()["customer"]["customer_id"]
    assert cid_after == cid_before


# ---------------------------------------------------- isolasi customer
def test_isolasi_antar_customer_via_connect():
    ds_a = _ds_trades(LOGIN_A, SERVER_A, "XAUUSD", 0.01, 2, 11, 100.0)
    ds_b = _ds_trades(LOGIN_B, SERVER_B, "EURUSD", 0.00001, 5, 55, 200.0)
    c = _client({str(LOGIN_A): ds_a, str(LOGIN_B): ds_b})
    tok_a = _connect(c, login=LOGIN_A, server=SERVER_A).get_json()["token"]
    tok_b = _connect(c, login=LOGIN_B, server=SERVER_B).get_json()["token"]
    hA = {"Authorization": f"Bearer {tok_a}"}
    hB = {"Authorization": f"Bearer {tok_b}"}
    for h in (hA, hB):
        assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                      headers=h).status_code == 200

    ta = c.get("/api/v1/journal/trades", headers=hA).get_json()
    tb = c.get("/api/v1/journal/trades", headers=hB).get_json()
    assert ta["total"] == 1 and tb["total"] == 1
    assert ta["items"][0]["symbol"] == "XAUUSD"
    assert tb["items"][0]["symbol"] == "EURUSD"

    pos_b = tb["items"][0]["position_id"]
    # A tidak bisa membuka detail trade milik B
    assert c.get(f"/api/v1/journal/trades/{pos_b}",
                 headers=hA).status_code == 404


# --------------------------------------------------------------- keamanan
def test_password_investor_tidak_pernah_plaintext_di_db_dan_audit():
    c = _client()
    body = _connect(c).get_json()
    assert body["token"]

    import db as dbmod
    from config import load_config  # noqa: F401
    app = c.application if hasattr(c, "application") else None
    cfg_obj = app.config["APP_CFG"]
    conn = dbmod.get_db(cfg_obj)

    cred = conn.execute(
        "SELECT enc_password, key_version FROM mt5_credentials").fetchone()
    assert cred is not None
    # 1) tersimpan terenkripsi — plaintext TIDAK ada di kolom
    assert PW_INVESTOR not in cred["enc_password"]
    # 2) ciphertext bisa didekripsi balik oleh server (verifikasi dua arah)
    from security import crypto
    assert crypto.decrypt_password(cfg_obj, cred["enc_password"],
                                   cred["key_version"]) == PW_INVESTOR

    # 3) audit_logs tidak pernah memuat password
    rows = conn.execute("SELECT detail FROM audit_logs").fetchall()
    for r in rows:
        assert PW_INVESTOR not in (r["detail"] or "")

    # 4) hash password user internal tidak memuat password investor
    u = conn.execute("SELECT password_hash FROM users").fetchone()
    assert PW_INVESTOR not in u["password_hash"]


def test_password_tidak_bocor_ke_log_backend(caplog):
    c = _client()
    with caplog.at_level(logging.DEBUG):
        assert _connect(c).status_code == 201
        # paksa jalur gagal juga
        _connect(c, password="salah-banget")
    blob = json.dumps(caplog.records
                      and [r.getMessage() for r in caplog.records] or [])
    assert PW_INVESTOR not in blob
    assert "salah-banget" not in blob


# ---------------------------------------------------------------- read-only
def test_provider_mock_tetap_menolak_method_tulis():
    """READ-ONLY MT5 tidak berubah: method tulis diblokir by design."""
    p = MockMT5Client()
    for forbidden in ("order_send", "order_check", "positions_close",
                      "order_modify", "eval"):
        try:
            getattr(p, forbidden)
        except ReadOnlyViolationError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"{forbidden} seharusnya diblokir")


def test_source_connect_tanpa_method_tulis_mt5():
    """Static guard: kode /mt5/connect tidak memanggil method MT5 tulis."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "api" /
           "routes.py").read_text(encoding="utf-8")
    block = src[src.index("def mt5_connect"):src.index("def _server_allowed")]
    for forbidden in ("order_send", "order_check", "positions_close",
                      "positions_modify", "order_modify", "order_delete",
                      ".login("):
        assert forbidden not in block, forbidden


# =============================== verifikasi live kredensial (hardening) ====
def test_connect_akun_baru_broker_menolak_kredensial_cleanup_penuh():
    """Requirement 1/2/4/6: broker menolak password investor yang salah ->
    401 dengan pesan jelas TANPA membocorkan kredensial; SEMUA jejak
    dibersihkan (slot customer bebas, tanpa baris akun/kredensial/terminal
    yatim), dan alasan sebenarnya hanya di audit_logs."""
    c = _client(fail_auth=True)
    r = _connect(c, password="inv-salah-benar")
    assert r.status_code == 401, r.get_json()
    msg = r.get_json()["error"]["message"]
    assert "Verifikasi ke server broker gagal" in msg
    assert "Exness-Real" in msg          # panduan umum server
    assert "inv-salah-benar" not in msg  # kredensial tidak bocor
    assert PW_INVESTOR not in msg
    conn = _db_conn(c.application)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM customers").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_accounts").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_credentials").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_terminals").fetchone()["n"] == 0
    dets = " ".join(r["detail"] or "" for r in
                    conn.execute("SELECT detail FROM audit_logs").fetchall())
    assert "live_auth_failed" in dets
    assert "inv-salah-benar" not in dets and PW_INVESTOR not in dets


def test_connect_akun_baru_master_password_ditolak_tidak_tersimpan():
    """Requirement 8: API MetaTrader5 menerima password master ATAU investor
    tanpa parameter pembeda. Kredensial yang menghasilkan sesi berhak trade
    (account_info().trade_allowed=True) DITOLAK — tidak pernah tersimpan,
    customer baru dibuang, alasan tercatat di audit."""
    c = _client(master_mode=True)
    r = _connect(c, password="master-password-1")
    assert r.status_code == 401, r.get_json()
    msg = r.get_json()["error"]["message"]
    assert "MASTER" in msg and "INVESTOR" in msg
    assert "master-password-1" not in msg
    conn = _db_conn(c.application)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_credentials").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM customers").fetchone()["n"] == 0
    dets = " ".join(r["detail"] or "" for r in
                    conn.execute("SELECT detail FROM audit_logs").fetchall())
    assert "master_password_ditolak" in dets


def test_master_password_berulang_memicu_lockout():
    """Kegagalan verifikasi live (termasuk master password) terhitung ke
    lockout per (login, server) — probing berulang tidak tak terbatas."""
    c = _client(master_mode=True)
    for i in range(5):
        assert _connect(c, password=f"master-{i}").status_code == 401
    r = _connect(c, password="master-lain")
    assert r.status_code == 429, r.get_json()


def test_connect_akun_baru_broker_unreachable_slot_bebas_lalu_bisa_dipakai():
    """Requirement 2/6: terminal/broker tidak terjangkau -> 503, akun TIDAK
    diaktifkan, slot TIDAK terpakai; app berikutnya (terminal hidup, DB
    sama) masih bisa memakai slot kapasitas yang sama (max_customers=1)."""
    import tempfile
    db_path = f"{tempfile.mkdtemp(prefix='vfy-')}/journal.db"
    cfg1 = _make_cfg(max_customers=1, db_path=db_path)
    c1 = _client(cfg=cfg1, fail_connect=True)
    r = _connect(c1)
    assert r.status_code == 503, r.get_json()
    msg = r.get_json()["error"]["message"]
    assert "BELUM diaktifkan" in msg and "tidak terpakai" in msg
    conn = _db_conn(c1.application)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM customers").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_terminals").fetchone()["n"] == 0
    # slot bebas: app kedua (terminal hidup) pada DB yang sama berhasil
    c2 = _client(cfg=_make_cfg(max_customers=1, db_path=db_path),
                 datasets={str(LOGIN_A): _ds_trades(LOGIN_A, SERVER_A,
                                                    "XAUUSD", 0.01, 2, 11,
                                                    100.0)})
    r2 = _connect(c2)
    assert r2.status_code == 201, r2.get_json()
    assert r2.get_json()["connection_state"] == "connected"
    assert r2.get_json()["live_verified"] is True


def test_connect_akun_baru_timeout_broker_resource_dibersihkan():
    """Requirement 6: verifikasi melewati batas waktu (provider lambat) ->
    503 + SEMUA resource dibersihkan (tidak ada baris yatim)."""
    c = _client(connect_delay=0.8, cfg=_make_cfg(verify_timeout=0.3))
    r = _connect(c)
    assert r.status_code == 503, r.get_json()
    assert "batas waktu" in r.get_json()["error"]["message"]
    conn = _db_conn(c.application)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM customers").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_accounts").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_credentials").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_terminals").fetchone()["n"] == 0


def test_reconnect_password_cocok_tapi_broker_menolak_401_tanpa_token():
    """Requirement 5: koneksi ulang TIDAK cukup membandingkan password
    terenkripsi bila broker menolak (password diubah di broker) -> 401
    dengan pesan jelas, TANPA token baru.

    Remediasi S4 (keputusan berubah dari versi awal): kredensial yang
    ditolak broker berarti akses lama TIDAK lagi sah (mis. pemilik
    mengganti password investor untuk memutus penonton) — SEMUA sesi
    customer dicabut."""
    import tempfile
    db_path = f"{tempfile.mkdtemp(prefix='rec-')}/journal.db"
    key = Fernet.generate_key().decode("ascii")  # SATU kunci utk dua app
    c1 = _client(cfg=_make_cfg(db_path=db_path, cred_key=key),
                 datasets={str(LOGIN_A): _ds_trades(LOGIN_A, SERVER_A,
                                                    "XAUUSD", 0.01, 2, 11,
                                                    100.0)})
    b1 = _connect(c1).get_json()
    assert b1["connection_state"] == "connected"
    # broker sekarang menolak kredensial tersimpan
    c2 = _client(cfg=_make_cfg(db_path=db_path, cred_key=key),
                 fail_auth=True)
    r = _connect(c2)  # password tersimpan yang benar dikirim ulang
    assert r.status_code == 401, r.get_json()
    msg = r.get_json()["error"]["message"]
    assert "tidak lagi diterima" in msg
    assert "sudah diubah" in msg
    assert PW_INVESTOR not in msg
    # S4: token lama ikut dicabut — kredensial sudah tidak sah di broker
    h = {"Authorization": f"Bearer {b1['token']}"}
    assert c2.get("/api/v1/auth/me", headers=h).status_code == 401


def test_reconnect_terminal_mati_sesi_terbit_status_terminal_pending():
    """Requirement 1/3/5: terminal mati saat reconnect -> sesi tetap
    diterbitkan agar riwayat jurnal bisa dibuka (D3), TAPI status BUKAN
    TERHUBUNG: connection_state='terminal_pending', live_verified=False.
    /mt5/status pun jujur: connected=False dengan alasan."""
    import tempfile
    db_path = f"{tempfile.mkdtemp(prefix='pend-')}/journal.db"
    key = Fernet.generate_key().decode("ascii")  # SATU kunci utk dua app
    c1 = _client(cfg=_make_cfg(db_path=db_path, cred_key=key),
                 datasets={str(LOGIN_A): _ds_trades(LOGIN_A, SERVER_A,
                                                    "XAUUSD", 0.01, 2, 11,
                                                    100.0)})
    assert _connect(c1).status_code == 201
    c2 = _client(cfg=_make_cfg(db_path=db_path, cred_key=key),
                 fail_connect=True)
    r = _connect(c2)
    assert r.status_code == 200, r.get_json()
    b = r.get_json()
    assert b["mode"] == "login"
    assert b["connection_state"] == "terminal_pending"
    assert b["live_verified"] is False
    # riwayat jurnal tetap terbaca dengan sesi ini (D3)
    h = {"Authorization": f"Bearer {b['token']}"}
    assert c2.get("/api/v1/journal/trades", headers=h).status_code == 200
    # status endpoint TIDAK mengklaim terhubung saat terminal mati
    st = c2.get("/api/v1/mt5/status", headers=h).get_json()
    assert st["connected"] is False
    assert st["reason"]


def test_provisioning_gagal_tidak_meninggalkan_customer_yatim():
    """Requirement 6: provisioning terminal gagal (kapasitas terminal
    penuh) -> 409 rapi, customer baru hasil percobaan DIBUANG, tidak ada
    terminal yatim, slot customer tetap terpakai hanya oleh yang sah."""
    import tempfile
    db_path = f"{tempfile.mkdtemp(prefix='prov-')}/journal.db"
    cfg = _make_cfg(max_customers=2, db_path=db_path)
    # setup: customer "hantu" dibuat lewat registrasi portal legacy yang
    # diaktifkan EKSPLISIT untuk test ini (default produksi juga aktif)
    cfg.public_register_enabled = True
    c1 = _client(cfg=cfg)
    assert _connect(c1, login=LOGIN_A, server=SERVER_A).status_code == 201
    conn = _db_conn(c1.application)
    # customer kedua (tanpa akun) + baris terminal "hantu" miliknya ->
    # kapasitas terminal (2) penuh sedangkan slot customer masih bebas 1
    assert c1.post("/api/v1/auth/register",
                   json={"email": "ghost@test.id",
                         "password": "ghostpass1"}).status_code == 201
    cid2 = int(conn.execute(
        "SELECT customer_id FROM users WHERE email='ghost@test.id'"
    ).fetchone()["customer_id"])
    conn.execute(
        """INSERT INTO mt5_terminals (customer_id, rpyc_port, wine_prefix,
             terminal_dir, data_dir, service_name, status)
           VALUES (?, 18813, '/tmp/x1', '/tmp/x1', '/tmp/x1', 'ghost',
                   'ready')""", (cid2,))
    # customer hantu dinonaktifkan agar slot customer AKTIF tersisa 1
    conn.execute("UPDATE customers SET status='disabled' WHERE id=?",
                 (cid2,))
    conn.commit()
    r = _connect(c1, login=LOGIN_B, server=SERVER_B)
    # slot MT5 (terminal) penuh -> 403 (keputusan produk: slot = terminal)
    assert r.status_code == 403, r.get_json()
    assert "slot MT5" in r.get_json()["error"]["message"]
    # customer percobaan dibuang — tidak ada yatim (tetap 2 baris: A + hantu)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM customers").fetchone()["n"] == 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_accounts").fetchone()["n"] == 1
    # slot terminal bebas: percobaan berikutnya berhasil
    conn.execute("DELETE FROM mt5_terminals WHERE customer_id=?", (cid2,))
    conn.commit()
    r2 = _connect(c1, login=LOGIN_B, server=SERVER_B)
    assert r2.status_code == 201, r2.get_json()


def test_live_verify_unit_klasifikasi_provider():
    """Unit _live_verify_account: klasifikasi ok/auth/unreachable pada
    provider mock + guard marker pesan otorisasi broker."""
    from api.routes import (_live_verify_account,
                            _looks_like_broker_auth_error)
    assert _looks_like_broker_auth_error(
        "MT5 initialize gagal (-6, 'Terminal: Invalid login or password')")
    assert _looks_like_broker_auth_error("authorization failed")
    assert not _looks_like_broker_auth_error(
        "Tidak bisa terhubung ke mt5server 127.0.0.1:18813: refused")

    c = _client()
    assert _connect(c).status_code == 201
    conn = _db_conn(c.application)
    reg = c.application.extensions["mt5_registry"]
    acc = conn.execute("SELECT id FROM mt5_accounts").fetchone()
    ver = _live_verify_account(conn, reg, int(acc["id"]), timeout=2.0)
    assert ver["class"] == "ok"
    assert ver["account_info"]["trade_allowed"] is False
    # akun tidak ada -> unreachable (bukan crash)
    ver2 = _live_verify_account(conn, reg, 999999, timeout=1.0)
    assert ver2["class"] == "unreachable"


def test_source_live_verify_tanpa_method_tulis_mt5():
    """Static guard: helper verifikasi live juga bebas method MT5 tulis."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "api" /
           "routes.py").read_text(encoding="utf-8")
    del src
    # implementasi verifikasi & koordinator kini di services/*.py —
    # SELURUH berkas dipindai (cakupan lebih luas dari blok lama)
    base = Path(__file__).resolve().parent.parent / "services"
    for name in ("verification.py", "provisioning.py", "provisioner.py"):
        block = (base / name).read_text(encoding="utf-8")
        for forbidden in ("order_send", "order_check", "positions_close",
                          "positions_modify", "order_modify",
                          "order_delete", ".login("):
            assert forbidden not in block, (name, forbidden)
