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
    return AccountInfo(
        login=login, trade_mode=0, leverage=100, limit_orders=200,
        margin_so_mode=0, trade_allowed=True, trade_expert=False,
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


def _client(datasets=None, max_customers=4, self_link=True):
    """App test client dengan mock provider per login akun."""
    cfg = Config()
    import tempfile
    tmp = tempfile.mkdtemp(prefix="connect-")
    cfg.db_path = f"{tmp}/journal.db"
    cfg.api_token = "TESTTOKEN"
    cfg.mt5_enabled = True
    cfg.cred_key = Fernet.generate_key().decode("ascii")
    cfg.term_base_dir = f"{tmp}/terminals"
    cfg.terminal_provision_mode = "mock"
    cfg.max_customers = max_customers
    cfg.mt5_self_link_enabled = self_link
    app = create_app(cfg, provider_factory=lambda acc_cfg=None:
                     MockMT5Client(datasets[str(acc_cfg.mt5_login)]
                                   if acc_cfg else None))
    app.config["TESTING"] = True
    return app.test_client()


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
        # pesan seragam — tidak membocorkan detail internal
        assert "Verifikasi gagal" in r.get_json()["error"]["message"]
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
    # setelah unbind, connect dgn kredensial sama = link ulang akun,
    # tetap pada customer yang SAMA (identitas tidak digandakan)
    r = _connect(c)
    assert r.status_code == 201, r.get_json()
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
    block = src[src.index("def mt5_connect"):src.index("def registry_provision")]
    for forbidden in ("order_send", "order_check", "positions_close",
                      "positions_modify", "order_modify", "order_delete",
                      ".login("):
        assert forbidden not in block, forbidden
