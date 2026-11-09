"""Regresi remediasi keamanan — basis PR #4 (SQLite; versi PostgreSQL
non-owner di test_remediation_pr4_pg.py).

Keputusan produk (2026-11): registrasi publik TETAP AKTIF; unbind / broker
menolak / admin -> cabut SEMUA sesi; riwayat jurnal DIPERTAHANKAN.

Temuan yang dicakup: T1 (kode customer), T2 (spoof XFF), T3 (registrasi
tidak menghabiskan slot MT5), T5 (investor-only di sync), S3 (lockout
persisten), S4 (pencabutan sesi), S5 (port tidak dibebaskan), R1
(metadata), Tahap 1/2/5 (registrasi, sesi, rate-limit lintas worker &
restart). Semua memakai MockMT5Client — tanpa broker nyata.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import auth as authmod
import db as dbmod
from app import create_app
from config import Config, load_config
from mt5client.mock import MockMT5Client, scenario_dataset
from tests.conftest import bind_account, register_and_login

BACKEND = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ helpers
def _cfg(**kw):
    c = Config()
    tmp = tempfile.mkdtemp(prefix="rem4-")
    c.db_path = f"{tmp}/journal.db"
    c.api_token = "TESTTOKEN"
    c.mt5_enabled = True
    c.cred_key = Fernet.generate_key().decode("ascii")
    c.term_base_dir = f"{tmp}/terminals"
    c.terminal_provision_mode = "mock"
    c.max_customers = 4
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _ds(login, server, trade_allowed=False, drop_trade_allowed=False):
    ds = scenario_dataset()
    acc = ds["account"]._replace(login=login, server=server,
                                 trade_allowed=trade_allowed)
    if drop_trade_allowed:
        d = acc._asdict()
        d.pop("trade_allowed")
        ds["account"] = d          # dict tanpa field trade_allowed
    else:
        ds["account"] = acc
    return ds


class _BrokerMock(MockMT5Client):
    """Mock read-only; broker tiruan dapat MENOLAK kredensial (pesan
    otorisasi MT5 sungguhan) selama broker['reject'] True."""

    def __init__(self, ds, broker):
        super().__init__(ds)
        self._broker = broker

    def connect(self):
        if self._broker["reject"]:
            from mt5client.provider import MT5ConnectionError
            raise MT5ConnectionError(
                "MT5 initialize gagal (-6, 'Terminal: Authorization "
                "failed')")
        return super().connect()


def _client(cfg, datasets=None, broker=None):
    """[broker] dict opsional {'reject': bool} — broker tiruan menolak
    kredensial (pesan otorisasi MT5) selama reject=True."""
    broker = broker if broker is not None else {"reject": False}

    def factory(acc_cfg=None):
        if acc_cfg is None:
            return MockMT5Client()
        # broker tiruan menyajikan akun YANG DIMINTA (sesi investor)
        return _BrokerMock((datasets or {}).get(str(acc_cfg.mt5_login))
                           or _ds(int(acc_cfg.mt5_login),
                                  acc_cfg.mt5_server), broker)
    app = create_app(cfg, provider_factory=factory)
    app.config["TESTING"] = True
    return app.test_client()


def _register(c, email, ip="198.51.100.10", password="rahasia123",
              **headers):
    return c.post("/api/v1/auth/register",
                  json={"email": email, "password": password},
                  headers={"X-Real-IP": ip, **headers})


def _login(c, email, ip="198.51.100.10"):
    r = c.post("/api/v1/auth/login",
               json={"email": email, "password": "rahasia123"},
               headers={"X-Real-IP": ip})
    assert r.status_code == 200, r.get_json()
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def _n(cfg, sql, params=()):
    return int(dbmod.get_db(cfg).execute(sql, params).fetchone()["n"])


# ======================================================= T1: kode customer
def test_t1_kode_customer_tidak_bentrok_setelah_customer_dihapus():
    cfg = _cfg()
    c = _client(cfg)
    assert _register(c, "a@t.id").status_code == 201
    assert _register(c, "b@t.id").status_code == 201
    conn = dbmod.get_db(cfg)
    # simulasi cleanup customer gagal-aktivasi: customer PERTAMA dihapus
    conn.execute("DELETE FROM customers WHERE code='C001'")
    conn.commit()
    # versi lama: COUNT(*)+1 = 2 -> 'C002' bentrok -> 409 permanen
    for i, email in enumerate(("c@t.id", "d@t.id")):
        r = _register(c, email, ip=f"198.51.100.{20 + i}")
        assert r.status_code == 201, r.get_json()
    codes = [r["code"] for r in conn.execute(
        "SELECT code FROM customers ORDER BY id")]
    assert len(codes) == len(set(codes)) == 3
    assert "C001" not in codes          # kode lama TIDAK dipakai ulang


def test_t1_kode_customer_dari_id_deterministik():
    assert authmod.customer_code_for(1) == "C001"
    assert authmod.customer_code_for(42) == "C042"
    assert authmod.customer_code_for(1234) == "C1234"


def test_t1_registrasi_bersamaan_semua_unik_tanpa_5xx():
    cfg = _cfg()
    c = _client(cfg)
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        cl = c.application.test_client()
        barrier.wait()
        r = _register(cl, f"u{i}@t.id", ip=f"10.{i}.0.1")
        results.append(r.status_code)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results == [201] * 8, results
    codes = [r["code"] for r in dbmod.get_db(cfg).execute(
        "SELECT code FROM customers")]
    assert len(codes) == len(set(codes)) == 8
    # registrasi TIDAK memakai slot MT5
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 0


def test_t1_registrasi_bersamaan_dengan_batas_portal():
    cfg = _cfg(max_registered_customers=4)
    c = _client(cfg)
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        cl = c.application.test_client()
        barrier.wait()
        results.append(_register(cl, f"v{i}@t.id",
                                 ip=f"10.{i}.0.2").status_code)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == [201] * 4 + [403] * 4, results
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 4


# ======================================================= T2: IP klien
class _H(dict):
    def get(self, k, d=None):
        for key, v in self.items():
            if key.lower() == k.lower():
                return v
        return d


def test_t2_header_diabaikan_bila_peer_tidak_tepercaya():
    cfg = Config()
    h = _H({"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"})
    assert authmod.client_ip_from("203.0.113.9", h, cfg) == "203.0.113.9"


def test_t2_x_real_ip_dipakai_hanya_dari_proxy_tepercaya():
    cfg = Config()
    h = _H({"X-Real-IP": "198.51.100.7", "X-Forwarded-For": "1.1.1.1"})
    assert authmod.client_ip_from("127.0.0.1", h, cfg) == "198.51.100.7"
    assert authmod.client_ip_from(
        "127.0.0.1", _H({"X-Real-IP": "evil<script>"}), cfg) == "127.0.0.1"


def test_t2_xff_hanya_entri_paling_kanan_bila_dikonfigurasi():
    cfg = Config()
    cfg.client_ip_header = "X-Forwarded-For"
    h = _H({"X-Forwarded-For": "6.6.6.6, 7.7.7.7, 192.0.2.10"})
    assert authmod.client_ip_from("127.0.0.1", h, cfg) == "192.0.2.10"


def test_t2_spoof_xff_tidak_melewati_rate_limit_login():
    cfg = _cfg()
    c = _client(cfg)
    codes = []
    for i in range(12):
        r = c.post("/api/v1/auth/login",
                   json={"email": f"x{i}@t.id", "password": "salah-salah"},
                   headers={"X-Forwarded-For": f"10.9.{i}.{i}"})
        codes.append(r.status_code)
    assert codes[:10] == [401] * 10, codes
    assert codes[10:] == [429, 429], codes


# ===================================== Tahap 1: registrasi publik aman
def test_reg_aktif_secara_default(monkeypatch):
    monkeypatch.delenv("AUTH_PUBLIC_REGISTER_ENABLED", raising=False)
    assert Config().public_register_enabled is True
    assert load_config().public_register_enabled is True


def test_reg_normal_tanpa_terminal_dan_password_ter_hash():
    cfg = _cfg()
    c = _client(cfg)
    r = _register(c, "Normal@T.ID")
    assert r.status_code == 201
    body = r.get_json()
    assert body["email"] == "normal@t.id" and body["customer_code"]
    row = dbmod.get_db(cfg).execute(
        "SELECT password_hash FROM users WHERE email='normal@t.id'"
    ).fetchone()
    assert "rahasia123" not in row["password_hash"]
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 0
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_accounts") == 0


def test_reg_duplikat_case_insensitive_ditolak_tanpa_baris_ganda():
    cfg = _cfg()
    c = _client(cfg)
    assert _register(c, "dup@t.id").status_code == 201
    assert _register(c, "DUP@t.id", ip="198.51.100.11").status_code == 409
    assert _n(cfg, "SELECT COUNT(*) AS n FROM users") == 1
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 1


@pytest.mark.parametrize("email,password,name", [
    ("bukan-email", "rahasia123", None),
    ("a@@t.id", "rahasia123", None),
    ("a b@t.id", "rahasia123", None),
    ("x@localhost", "rahasia123", None),
    ("ok@t.id", "pendek", None),
    ("ok@t.id", "ada\x00nul123", None),
    ("ok@t.id", "rahasia123", "nama\nbaris"),
    ("ok@t.id", "rahasia123", "x" * 81),
])
def test_reg_validasi_input_ditolak_tanpa_efek(email, password, name):
    cfg = _cfg()
    c = _client(cfg)
    body = {"email": email, "password": password}
    if name is not None:
        body["display_name"] = name
    r = c.post("/api/v1/auth/register", json=body,
               headers={"X-Real-IP": "198.51.100.30"})
    assert r.status_code == 400, r.get_json()
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 0


def test_reg_spoof_xff_tidak_melewati_limit_per_ip():
    cfg = _cfg()
    c = _client(cfg)
    codes = []
    for i in range(7):
        r = c.post("/api/v1/auth/register",
                   json={"email": f"sp{i}@t.id", "password": "rahasia123"},
                   headers={"X-Forwarded-For": f"203.0.{i}.{i}"})
        codes.append(r.status_code)
    # limit 5/jam per IP: XFF palsu tidak menciptakan IP baru
    assert codes == [201] * 5 + [429, 429], codes
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 5


def test_reg_massal_dari_satu_subnet_dibatasi_subnet_lain_tidak_terkunci():
    cfg = _cfg(register_per_subnet_day=3)
    c = _client(cfg)
    codes = [_register(c, f"m{i}@t.id", ip=f"203.0.113.{i + 1}").status_code
             for i in range(5)]
    assert codes == [201, 201, 201, 429, 429], codes
    # satu sumber penyalahgunaan TIDAK mengunci registrasi dari subnet lain
    assert _register(c, "lain@t.id", ip="198.18.7.7").status_code == 201
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 4


def test_reg_limit_harian_per_ip():
    cfg = _cfg(register_per_ip_hour=100, register_per_ip_day=2)
    c = _client(cfg)
    codes = [_register(c, f"d{i}@t.id", ip="198.18.1.1").status_code
             for i in range(3)]
    assert codes == [201, 201, 429], codes


def test_reg_saat_slot_mt5_penuh_tetap_bisa_tapi_tidak_dapat_terminal():
    cfg = _cfg(max_customers=2)
    c = _client(cfg)
    for i in range(2):
        assert _register(c, f"s{i}@t.id", ip=f"198.18.2.{i}").status_code \
            == 201
        bind_account(c, _login(c, f"s{i}@t.id"), login=7000 + i)
    r = _register(c, "s9@t.id", ip="198.18.3.1")
    assert r.status_code == 201       # registrasi tidak memakai slot
    h = _login(c, "s9@t.id")
    r = c.post("/api/v1/accounts/bind", headers=h,
               json={"login": 7009, "server": "Exness-MT5Real",
                     "password": "pw-investor"})
    assert r.status_code == 403
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 2
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_accounts WHERE"
                   " login=7009") == 0


# ======================================= Tahap 2: pencabutan seluruh sesi
def _tokens(c, email, n, ip="198.51.100.10"):
    return [_login(c, email, ip=ip) for _ in range(n)]


def test_s4_unbind_dari_satu_perangkat_mencabut_semua_dan_riwayat_tetap():
    cfg = _cfg()
    c = _client(cfg)
    assert _register(c, "rv@t.id").status_code == 201
    h1, h2, h3 = _tokens(c, "rv@t.id", 3)
    bind_account(c, h1, login=41414141)
    assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                  headers=h1).get_json()["status"] == "ok"
    deals = _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals")
    tt = dbmod.trades_table(dbmod.get_db(cfg))
    trades = _n(cfg, f"SELECT COUNT(*) AS n FROM {tt}")
    assert deals > 0 and trades > 0
    r = c.post("/api/v1/accounts/unbind", headers=h2)
    assert r.status_code == 200 and r.get_json()["history_retained"]
    for h in (h1, h2, h3):
        assert c.get("/api/v1/auth/me", headers=h).status_code == 401
    assert _n(cfg, "SELECT COUNT(*) AS n FROM auth_tokens WHERE"
                   " revoked_at IS NULL") == 0
    # riwayat jurnal UTUH; kredensial dihapus; akun 'unbound'
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals") == deals
    assert _n(cfg, f"SELECT COUNT(*) AS n FROM {tt}") == trades
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_credentials") == 0
    assert dbmod.get_db(cfg).execute(
        "SELECT state FROM mt5_accounts").fetchone()["state"] == "unbound"
    # login ulang: riwayat masih bisa dibaca, sync ditolak (tidak aktif)
    h = _login(c, "rv@t.id")
    assert c.get("/api/v1/journal/trades", headers=h).get_json()["total"] \
        == trades
    assert c.post("/api/v1/mt5/sync", json={}, headers=h).status_code == 409


def test_s4_broker_menolak_saat_sync_mencabut_semua_sesi():
    cfg = _cfg()
    broker = {"reject": False}
    c = _client(cfg, broker=broker)
    assert _register(c, "br@t.id").status_code == 201
    h1, h2 = _tokens(c, "br@t.id", 2)
    bind_account(c, h1, login=42424242)
    assert c.post("/api/v1/mt5/sync", json={"mode": "full"},
                  headers=h1).get_json()["status"] == "ok"
    deals = _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals")
    broker["reject"] = True       # password investor diganti di broker
    c.application.extensions["mt5_registry"].evict(
        int(dbmod.get_db(cfg).execute(
            "SELECT id FROM mt5_accounts").fetchone()["id"]))
    body = c.post("/api/v1/mt5/sync", json={}, headers=h1).get_json()
    assert body["status"] == "error"
    for h in (h1, h2):
        assert c.get("/api/v1/auth/me", headers=h).status_code == 401
    row = dbmod.get_db(cfg).execute(
        "SELECT state FROM mt5_accounts").fetchone()
    assert row["state"] == "credentials_rejected"
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_credentials") == 0
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals") == deals
    # scheduler tidak lagi memilih akun ini
    reg = c.application.extensions["mt5_registry"]
    assert reg.active_account_ids(dbmod.get_db(cfg)) == []
    # bind ulang (password investor BARU) mengaktifkan kembali akun SAMA
    broker["reject"] = False
    h = _login(c, "br@t.id")
    r = c.post("/api/v1/accounts/bind", headers=h,
               json={"login": 42424242, "server": "Exness-MT5Real",
                     "password": "password-baru-1"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["state"] == "active"


def test_s4_admin_mencabut_akses():
    cfg = _cfg()
    c = _client(cfg)
    assert _register(c, "adm@t.id").status_code == 201
    hs = _tokens(c, "adm@t.id", 3)
    cid = _n(cfg, "SELECT customer_id AS n FROM users WHERE"
                  " email='adm@t.id'")
    r = c.post(f"/api/v1/admin/customers/{cid}/revoke-sessions")
    assert r.status_code == 401                     # tanpa token admin
    r = c.post(f"/api/v1/admin/customers/{cid}/revoke-sessions",
               headers={"X-API-Token": "TESTTOKEN"})
    assert r.status_code == 200 and r.get_json()["revoked"] == 3
    for h in hs:
        assert c.get("/api/v1/auth/me", headers=h).status_code == 401


def test_s4_audit_tanpa_password_atau_token_mentah():
    cfg = _cfg()
    c = _client(cfg)
    assert _register(c, "au@t.id").status_code == 201
    h = _login(c, "au@t.id")
    raw_token = h["Authorization"].split(" ", 1)[1]
    bind_account(c, h, login=43434343, password="INVESTOR-SECRET-xyz")
    c.post("/api/v1/accounts/unbind", headers=h)
    blob = json.dumps([dict(r) for r in dbmod.get_db(cfg).execute(
        "SELECT * FROM audit_logs")])
    for secret in ("rahasia123", "INVESTOR-SECRET-xyz", raw_token):
        assert secret not in blob
    # defense-in-depth: kunci rahasia di detail audit disensor
    import audit as auditmod
    auditmod.audit(dbmod.get_db(cfg), "uji", detail={"password": "BOCOR1",
                                                      "nested": {
                                                          "token": "BOCOR2"}})
    blob = json.dumps([dict(r) for r in dbmod.get_db(cfg).execute(
        "SELECT detail FROM audit_logs")])
    assert "BOCOR1" not in blob and "BOCOR2" not in blob


def test_s4_restart_backend_token_dicabut_tetap_ditolak():
    cfg = _cfg()
    c = _client(cfg)
    assert _register(c, "rs@t.id").status_code == 201
    h1, h2 = _tokens(c, "rs@t.id", 2)
    bind_account(c, h1, login=44444444)
    c.post("/api/v1/accounts/unbind", headers=h1)
    dbmod.close_db()
    c2 = _client(cfg)            # proses baru, DB yang sama
    for h in (h1, h2):
        assert c2.get("/api/v1/auth/me", headers=h).status_code == 401


# ================================= Tahap 5 / S3: rate-limit & lockout DB
_LOCK_WORKER = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from cryptography.fernet import Fernet
    from config import Config
    from app import create_app
    from mt5client.mock import MockMT5Client
    c = Config()
    c.db_path = sys.argv[2]
    c.api_token = "T"
    c.cred_key = Fernet.generate_key().decode()
    c.term_base_dir = sys.argv[2] + ".term"
    app = create_app(c, provider_factory=lambda *a, **k: MockMT5Client())
    cl = app.test_client()
    pw, n = sys.argv[3], int(sys.argv[4])
    out = [cl.post("/api/v1/auth/login",
                   json={"email": "lk@t.id", "password": pw},
                   headers={"X-Real-IP": "10.3.%d.1" % i}).status_code
           for i in range(n)]
    print(json.dumps(out))
""")


def _lock_proc(db_path, password, n):
    out = subprocess.run([sys.executable, "-c", _LOCK_WORKER, str(BACKEND),
                          db_path, password, str(n)], capture_output=True,
                         text=True, timeout=120)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_s3_lockout_bertahan_setelah_restart_proses():
    """Restart SUNGGUHAN: proses #1 membuat 5 kegagalan login lalu keluar;
    proses #2 (memori bersih) tetap menolak password BENAR (429)."""
    cfg = _cfg()
    c = _client(cfg)
    assert _register(c, "lk@t.id").status_code == 201
    dbmod.close_db()
    assert _lock_proc(cfg.db_path, "salah-salah", 5) == [401] * 5
    assert _lock_proc(cfg.db_path, "rahasia123", 1) == [429]


def test_s3_percobaan_login_bersamaan_terhitung_atomik():
    cfg = _cfg()
    c = _client(cfg)
    assert _register(c, "cc@t.id").status_code == 201
    barrier = threading.Barrier(12)
    codes = []

    def worker(i):
        cl = c.application.test_client()
        barrier.wait()
        r = cl.post("/api/v1/auth/login",
                    json={"email": f"nobody{i}@t.id", "password": "x" * 9},
                    headers={"X-Real-IP": "10.4.0.1"})
        codes.append(r.status_code)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes.count(401) == 10 and codes.count(429) == 2, codes


_WORKER = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from cryptography.fernet import Fernet
    from config import Config
    from app import create_app
    from mt5client.mock import MockMT5Client
    c = Config()
    c.db_path = sys.argv[2]
    c.db_url = sys.argv[3]
    c.api_token = "T"
    c.cred_key = Fernet.generate_key().decode()
    c.term_base_dir = sys.argv[2] + ".term"
    app = create_app(c, provider_factory=lambda *a, **k: MockMT5Client())
    cl = app.test_client()
    out = []
    for i in range(int(sys.argv[4])):
        r = cl.post("/api/v1/auth/login",
                    json={"email": "w%s_%d@t.id" % (sys.argv[5], i),
                          "password": "salah-salah"},
                    headers={"X-Real-IP": "10.5.0.1"})
        out.append(r.status_code)
    print(json.dumps(out))
""")


def run_workers(db_path: str, db_url: str, n_proc: int, per_proc: int):
    """Jalankan [n_proc] PROSES backend terpisah (worker) pada DB yang
    sama, masing-masing [per_proc] percobaan login dari IP yang sama."""
    procs = [subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(BACKEND), db_path, db_url,
         str(per_proc), str(i)], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True) for i in range(n_proc)]
    codes = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        codes += json.loads(out.strip().splitlines()[-1])
    return codes


def test_rate_limit_konsisten_lintas_banyak_proses_worker():
    cfg = _cfg()
    _client(cfg)                 # migrasi DB
    dbmod.close_db()
    codes = run_workers(cfg.db_path, "", 3, 6)
    assert len(codes) == 18
    # limit login 10 / 5 menit per IP berlaku GLOBAL lintas proses
    assert codes.count(401) == 10 and codes.count(429) == 8, codes


# ================================================= T5: investor-only sync
@pytest.mark.parametrize("variant", ["master", "tanpa_field"])
def test_t5_sync_menolak_sesi_bukan_investor(variant):
    login, server = 31313131, "Exness-MT5Real"
    ds = _ds(login, server, trade_allowed=(variant == "master"),
             drop_trade_allowed=(variant == "tanpa_field"))
    # Kredensial MASTER yang SUDAH tersimpan (data lama / migrasi / bind
    # saat MT5_ENABLED=false — tanpa verifikasi live) tetap tidak boleh
    # dipakai sync: bind dilakukan saat MT5 nonaktif, lalu MT5 diaktifkan.
    cfg = _cfg(mt5_enabled=False)
    c = _client(cfg, {str(login): ds})
    h = register_and_login(c, "inv@t.id")
    bind_account(c, h, login=login, server=server)
    cfg.mt5_enabled = True
    r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=h)
    body = r.get_json()
    assert body["status"] == "error", body
    assert "InvestorOnlyViolation" in body["error"]
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals") == 0


def test_t5_sync_sesi_investor_tetap_berjalan():
    login, server = 32323232, "Exness-MT5Real"
    cfg = _cfg()
    c = _client(cfg, {str(login): _ds(login, server, trade_allowed=False)})
    h = register_and_login(c, "ok@t.id")
    bind_account(c, h, login=login, server=server)
    r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=h)
    assert r.get_json()["status"] == "ok", r.get_json()
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_deals") > 0


# =================================================== S5: port terminal
def test_s5_unbind_tidak_membebaskan_port_ke_customer_lain():
    cfg = _cfg()
    c = _client(cfg)
    hA = register_and_login(c, "pa@t.id")
    bind_account(c, hA, login=51515151)
    conn = dbmod.get_db(cfg)
    q = ("SELECT t.rpyc_port FROM mt5_terminals t JOIN users u"
         " ON u.customer_id=t.customer_id WHERE u.email=?")
    port_a = int(conn.execute(q, ("pa@t.id",)).fetchone()["rpyc_port"])
    assert c.post("/api/v1/accounts/unbind", headers=hA).status_code == 200
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 1
    hB = register_and_login(c, "pb@t.id")
    bind_account(c, hB, login=52525252)
    assert int(conn.execute(q, ("pb@t.id",)).fetchone()["rpyc_port"]) \
        != port_a


# ================================================= R1: metadata infra
def test_r1_me_bind_status_tanpa_metadata_infrastruktur():
    cfg = _cfg()
    c = _client(cfg)
    h = register_and_login(c, "meta@t.id")
    body = bind_account(c, h, login=61616161)
    st = c.get("/api/v1/mt5/status", headers=h).get_json()
    me = c.get("/api/v1/auth/me", headers=h).get_json()
    for blob in (repr(body), repr(st), repr(me)):
        for leak in ("rpyc_port", "service_name", "wine_prefix",
                     "terminal_dir", "mt5server_bin"):
            assert leak not in blob, leak
    assert set((me["terminal"] or {}).keys()) <= {"status"}


def test_r2_marker_auth_broker_spesifik():
    from mt5client.provider import looks_like_broker_auth_error as f
    assert f("MT5 initialize gagal (-6, 'Terminal: Authorization failed')")
    assert f("Invalid login or password")
    assert not f("[Errno 13] Permission denied: '/var/lib/x'")
    assert not f("password file missing")


def test_worker_helper_tersedia_untuk_pg():
    """Sanity: helper multi-proses dipakai juga oleh test PG."""
    assert callable(run_workers) and os.path.isdir(BACKEND)


def test_repr_config_tidak_membocorkan_secret():
    """Secret konfigurasi tidak muncul di repr (log, traceback, output
    assertion pytest)."""
    c = Config()
    c.mt5_password, c.cred_key, c.api_token = "PWX-1", "KEYX-1", "TOKX-1"
    c.db_url = "postgresql://app:DBPW-1@127.0.0.1/db"
    r = repr(c)
    for secret in ("PWX-1", "KEYX-1", "TOKX-1", "DBPW-1"):
        assert secret not in r


@pytest.mark.parametrize("host", ["10.0.0.5", "0.0.0.0", "jurnal.example",
                                  "192.168.1.10", ""])
def test_rpyc_classic_hanya_loopback(host):
    """RPyC classic (mt5server) tanpa autentikasi: provider menolak host
    non-loopback SEBELUM membuka koneksi (fail-closed)."""
    from mt5client.mt5linux_provider import MT5LinuxProvider
    from mt5client.provider import MT5ConnectionError

    class C:
        mt5_host, mt5_port = host, 18812
        mt5_login = mt5_password = mt5_server = ""
    with pytest.raises(MT5ConnectionError) as ei:
        MT5LinuxProvider(C()).connect()
    assert "loopback" in str(ei.value)


def test_rpyc_loopback_diizinkan():
    from mt5client.mt5linux_provider import is_loopback_host
    for h in ("127.0.0.1", "::1", "localhost", "127.0.0.2"):
        assert is_loopback_host(h)


# ========================= Prioritas 3: registrasi massal lintas proses
_REG_WORKER = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from cryptography.fernet import Fernet
    from config import Config
    from app import create_app
    from mt5client.mock import MockMT5Client
    c = Config()
    c.db_path = sys.argv[2]
    c.api_token = "T"
    c.cred_key = Fernet.generate_key().decode()
    c.term_base_dir = sys.argv[2] + ".term"
    c.max_registered_customers = int(sys.argv[5])
    app = create_app(c, provider_factory=lambda *a, **k: MockMT5Client())
    cl = app.test_client()
    out = []
    for i in range(int(sys.argv[3])):
        r = cl.post("/api/v1/auth/register",
                    json={"email": "p%s_%d@t.id" % (sys.argv[4], i),
                          "password": "rahasia123"},
                    headers={"X-Real-IP": (sys.argv[6] % i if "%" in sys.argv[6] else sys.argv[6]),
                             "X-Forwarded-For": "1.2.3.%d" % i})
        out.append(r.status_code)
    print(json.dumps(out))
""")


def _reg_procs(db_path, n_proc, per_proc, cap, ip_fmt):
    procs = [subprocess.Popen(
        [sys.executable, "-c", _REG_WORKER, str(BACKEND), db_path,
         str(per_proc), str(i), str(cap), ip_fmt],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for i in range(n_proc)]
    codes = []
    for p in procs:
        codes += json.loads(p.communicate(timeout=120)[0].strip()
                            .splitlines()[-1])
    return codes


def test_reg_limit_per_ip_konsisten_lintas_proses_dan_restart():
    cfg = _cfg()
    _client(cfg)
    dbmod.close_db()
    # 3 proses worker, IP SAMA (XFF palsu diabaikan): total sukses = 5/jam
    codes = _reg_procs(cfg.db_path, 3, 4, 0, "198.18.9.9")
    assert codes.count(201) == 5 and codes.count(429) == 7, codes
    # "restart": proses baru, memori bersih -> tetap 429
    assert _reg_procs(cfg.db_path, 1, 1, 0, "198.18.9.9") == [429]


def test_reg_massal_multi_subnet_dibatasi_batas_portal():
    cfg = _cfg(max_registered_customers=6)
    _client(cfg)
    dbmod.close_db()
    codes = _reg_procs(cfg.db_path, 3, 4, 6, "10.%d.77.1")
    # 12 IP berbeda, 4 subnet per proses: batas akun portal 6 berlaku
    # global lintas proses (capacity guard DB), sisanya 403 — tanpa 5xx
    assert codes.count(201) == 6 and codes.count(403) == 6, codes
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 6
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_terminals") == 0
    # akun lama tetap bisa login; kapasitas terminal tidak tersentuh
    c = _client(cfg)
    email = dbmod.get_db(cfg).execute(
        "SELECT email FROM users LIMIT 1").fetchone()["email"]
    r = c.post("/api/v1/auth/login",
               json={"email": email, "password": "rahasia123"},
               headers={"X-Real-IP": "198.18.200.1"})
    assert r.status_code == 200


def test_reg_default_batas_portal_aman():
    assert Config().max_registered_customers == 200
