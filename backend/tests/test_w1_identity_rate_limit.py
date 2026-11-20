"""W1 — rate limit & lockout berbasis IDENTITAS + rem GLOBAL (bukan IP).

Latar: di balik proxy NAT penyedia VPS semua request tiba dari peer yang
SAMA dan X-Forwarded-For seluruhnya isian klien. IP (REMOTE_ADDR,
X-Real-IP, X-Forwarded-For) tidak boleh menjadi identitas rate limit /
lockout: kalau dipakai, satu klien menghabiskan kuota SEMUA pengguna.

Dicakup: kunci identitas (email ternormalisasi, akun MT5, job_id), rem
global (urutan identitas -> global), lockout, anti-enumerasi, akses
bersamaan (thread + atomik), konfigurasi env, dan bukti bahwa tidak ada
kunci throttle yang memuat alamat IP. Semua memakai SQLite + mock MT5.
"""
import logging
import threading
import uuid

import pytest

import auth as authmod
import db as dbmod
from config import Config, load_config
from tests.test_remediation_pr5 import _app, _cfg, _post

IPS = ["198.51.100.7", "203.0.113.9", "10.10.0.1", "192.0.2.44"]


def _client(**kw):
    app = _app(_cfg(**kw))
    return app, app.test_client()


def _login(c, email, password="salah-salah", ip="198.51.100.10",
           remote=None):
    kw = {"environ_overrides": {"REMOTE_ADDR": remote}} if remote else {}
    return c.post("/api/v1/auth/login",
                  json={"email": email, "password": password},
                  headers={"X-Real-IP": ip,
                           "X-Forwarded-For": f"{ip}, 1.2.3.4"}, **kw)


def _register(c, email, ip="198.51.100.10"):
    return c.post("/api/v1/auth/register",
                  json={"email": email, "password": "rahasia123"},
                  headers={"X-Real-IP": ip, "X-Forwarded-For": ip})


def _status(c, job_id, secret="x", ip="198.51.100.10"):
    return c.post("/api/v1/mt5/connect/status",
                  json={"job_id": job_id, "job_secret": secret},
                  headers={"X-Real-IP": ip})


# ======================================================= unit: identitas
def test_email_identity_normalisasi_dan_tidak_valid():
    assert authmod.email_identity("  A.B@Example.COM ") == "a.b@example.com"
    assert authmod.email_identity("bukan-email") is None
    assert authmod.email_identity("") is None


def test_mt5_account_identity_casefold_dan_trim():
    a = authmod.mt5_account_identity(123, "Exness-MT5Real")
    assert a == authmod.mt5_account_identity(123, "  exness-mt5real ")
    assert a != authmod.mt5_account_identity(124, "Exness-MT5Real")


def test_job_identity_uuid4_positif():
    """Semua keluaran uuid.uuid4().hex (format job_id dari provisioning)
    diterima apa adanya."""
    for _ in range(500):
        jid = uuid.uuid4().hex
        assert authmod.job_identity(jid) == jid


def test_job_identity_uuid4_negatif_versi_varian_dan_format():
    jid = uuid.uuid4().hex
    assert jid[12] == "4" and jid[16] in "89ab"
    bad = [
        "", "x" * 32, "0" * 32,
        jid.upper(), jid + "0", jid[:-1], "../" + jid[:29], "' OR 1=1 --",
        str(uuid.UUID(jid)),                       # bentuk berstrip
        jid + "\n", " " + jid,                     # spasi / newline
        uuid.uuid1().hex,                          # UUIDv1
        uuid.uuid3(uuid.NAMESPACE_DNS, "x").hex,   # UUIDv3
        uuid.uuid5(uuid.NAMESPACE_DNS, "x").hex,   # UUIDv5
    ]
    # nibble VERSI selain '4'
    bad += [jid[:12] + v + jid[13:] for v in "0123567890abcdef" if v != "4"]
    # nibble VARIAN di luar {8,9,a,b} (NCS / Microsoft / cadangan)
    bad += [jid[:16] + v + jid[17:] for v in "01234567cdef"]
    for b in bad:
        assert authmod.job_identity(b) == "invalid", repr(b)
    # varian yang sah semuanya diterima
    for v in "89ab":
        ok = jid[:16] + v + jid[17:]
        assert authmod.job_identity(ok) == ok


# ==================================== tidak ada kunci throttle berisi IP
def test_tidak_ada_kunci_throttle_yang_memuat_ip(monkeypatch):
    """Spy semua kunci rate limit di 4 endpoint publik: tidak satu pun
    memuat IP peer/header, dan IP berganti tidak mengubah kunci."""
    seen = []
    orig = authmod.RateLimiter.hit_all

    def spy(self, checks, conn=None):
        seen.extend(k for k, _, _ in checks)
        return orig(self, checks, conn=conn)
    monkeypatch.setattr(authmod.RateLimiter, "hit_all", spy)
    app, c = _client()
    for ip in IPS:
        _register(c, "spy@t.id", ip=ip)
        _login(c, "spy@t.id", ip=ip, remote=ip)
        _post(app, 55500001, password="salah", ip=ip)
        _status(c, uuid.uuid4().hex, ip=ip)
    assert seen, "throttle tidak terpanggil"
    for k in seen:
        for ip in IPS + ["1.2.3.4", "127.0.0.1"]:
            assert ip not in k, k
    # kunci per identitas identik untuk semua IP
    assert len({k for k in seen if k.startswith("login:email:")}) == 1
    assert len({k for k in seen if k.startswith("register:email:")}) == 1
    assert len({k for k in seen if k.startswith("mt5-connect:acct:")}) == 1


def test_kode_tidak_memakai_proxyfix_atau_header_ip_untuk_throttle():
    import inspect
    import api.routes as routes
    src = inspect.getsource(routes)
    assert "ProxyFix" not in src
    assert "require_rate_limit" not in src
    for name in ("auth_login", "auth_register", "mt5_connect",
                 "mt5_connect_status", "_throttle"):
        body = inspect.getsource(getattr(routes, name))
        assert "X-Forwarded-For" not in body and "X-Real-IP" not in body
    # register / status / _throttle tidak membaca IP sama sekali;
    # login & connect hanya meneruskannya sbg metadata sesi (issue_token)
    for name in ("auth_register", "mt5_connect_status", "_throttle"):
        assert "current_ip" not in inspect.getsource(getattr(routes, name))
    body = inspect.getsource(routes.mt5_connect)
    assert "ip_key" not in body
    assert "lock_ip." not in body and "lock_ip =" not in body


# ============================================================ login
def test_login_per_email_tidak_bergantung_ip_peer_maupun_header():
    app, c = _client(max_failed_logins=1000, rl_login_per_email=4)
    codes = [_login(c, "Satu@T.id", ip=IPS[i % 4], remote=IPS[i % 4]
                    ).status_code for i in range(6)]
    assert codes == [401] * 4 + [429] * 2, codes
    # email lain (IP sama) tetap dilayani
    assert _login(c, "dua@t.id", ip=IPS[0]).status_code == 401


def test_login_lockout_per_email_lintas_ip_dan_password_benar_ditolak():
    app, c = _client()
    assert _register(c, "lk@t.id").status_code == 201
    cfg = app.config["APP_CFG"]
    for i in range(cfg.max_failed_logins):
        assert _login(c, "lk@t.id", ip=IPS[i % 4]).status_code == 401
    assert _login(c, "LK@t.id", password="rahasia123",
                  ip="203.0.113.250").status_code == 429
    # email lain tidak terkunci
    assert _register(c, "ok@t.id").status_code == 201
    assert _login(c, "ok@t.id", password="rahasia123").status_code == 200


def test_login_email_ditolak_identitasnya_tidak_menghabiskan_rem_global():
    """Urutan identitas -> global: penyerang yang menggempur SATU email
    berhenti di ember emailnya dan tidak menghabiskan kuota global."""
    app, c = _client(max_failed_logins=1000, rl_login_per_email=2,
                     rl_login_global=5)
    for _ in range(10):
        _login(c, "target@t.id")
    codes = [_login(c, f"u{i}@t.id").status_code for i in range(3)]
    assert codes == [401] * 3, codes            # global terpakai 2 + 3 = 5
    assert _login(c, "u9@t.id").status_code == 429   # global habis


def test_login_email_tidak_valid_tetap_dihitung_rem_global():
    app, c = _client(rl_login_global=2)
    assert _login(c, "bukan-email").status_code == 400
    assert _login(c, "juga bukan").status_code == 400
    assert _login(c, "valid@t.id").status_code == 429


def test_login_anti_enumerasi_respons_dan_kerja_hash_setara(monkeypatch):
    calls = []
    orig = authmod.check_password_hash

    def spy(h, pw):
        calls.append(h)
        return orig(h, pw)
    monkeypatch.setattr(authmod, "check_password_hash", spy)
    app, c = _client()
    assert _register(c, "ada@t.id").status_code == 201
    calls.clear()
    known = _login(c, "ada@t.id")
    unknown = _login(c, "tidak-ada@t.id")
    assert known.status_code == unknown.status_code == 401
    assert known.get_json() == unknown.get_json()
    # tepat SATU verifikasi pbkdf2 di masing-masing jalur (waktu setara)
    assert len(calls) == 2
    assert calls[0].split("$")[0] == calls[1].split("$")[0]   # metode sama


def test_login_bersamaan_satu_email_atomik():
    app, c = _client(max_failed_logins=1000, rl_login_per_email=10)
    codes = []
    barrier = threading.Barrier(12)

    def w(i):
        cl = app.test_client()
        barrier.wait()
        codes.append(_login(cl, "race@t.id", ip=IPS[i % 4]).status_code)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes.count(401) == 10 and codes.count(429) == 2, codes


# ========================================================= register
def test_register_email_tidak_valid_tidak_bisa_melewati_rem_global():
    app, c = _client(rl_register_global_hour=2)
    assert _register(c, "x").status_code == 400
    assert _register(c, "y@").status_code == 400
    assert _register(c, "valid@t.id").status_code == 429


def test_register_rem_global_bersamaan_atomik():
    app, c = _client(rl_register_global_hour=5)
    codes = []
    barrier = threading.Barrier(9)

    def w(i):
        cl = app.test_client()
        barrier.wait()
        codes.append(_register(cl, f"g{i}@t.id", ip=IPS[i % 4]).status_code)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(9)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes.count(201) == 5 and codes.count(429) == 4, codes
    n = dbmod.get_db(app.config["APP_CFG"]).execute(
        "SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    assert n == 5


def test_register_bukan_ember_ip_bersama_pengguna_berbeda_tidak_saling_blokir():
    """Regresi inti W1: dengan default produksi, 12 pengguna berbeda dari
    peer proxy YANG SAMA (10.10.0.1) semuanya bisa mendaftar & login."""
    app, c = _client()
    for i in range(12):
        assert _register(c, f"p{i}@t.id", ip="10.10.0.1").status_code == 201
        assert _login(c, f"p{i}@t.id", password="rahasia123",
                      ip="10.10.0.1").status_code == 200


# ======================================================= mt5 connect
def test_connect_rate_limit_per_akun_lintas_ip():
    app, c = _client(max_failed_logins=1000, rl_connect_per_account=3)
    codes = [_post(app, 55500010, password="salah", ip=IPS[i % 4]
                   ).status_code for i in range(5)]
    assert codes == [401] * 3 + [429] * 2, codes
    # akun lain tidak ikut terbatas
    assert _post(app, 55500011, ip=IPS[0]).status_code == 201


def test_connect_ditolak_per_akun_tidak_menghabiskan_rem_global():
    app, c = _client(max_failed_logins=1000, rl_connect_per_account=2,
                     rl_connect_global=4)
    for i in range(8):
        _post(app, 55500020, password="salah", ip=IPS[i % 4])
    assert _post(app, 55500021).status_code == 201
    assert _post(app, 55500022).status_code == 201
    assert _post(app, 55500023).status_code == 429          # global habis


def test_connect_lockout_pesan_seragam_akun_ada_vs_tidak():
    """Anti-enumerasi: akun terdaftar dan tidak terdaftar sama-sama 401
    pesan generik lalu 429 setelah ambang yang sama."""
    app, c = _client()
    assert _post(app, 55500030).status_code == 201
    cfg = app.config["APP_CFG"]
    out = {}
    for login in (55500030, 55500031):
        rs = [_post(app, login, password="salah")
              for _ in range(cfg.max_failed_logins + 1)]
        out[login] = [r.status_code for r in rs]
        msgs = {r.get_json()["error"]["message"] for r in rs[:-1]}
        assert len(msgs) == 1, msgs
    assert out[55500030] == out[55500031] == \
        [401] * cfg.max_failed_logins + [429], out


def test_connect_bersamaan_satu_akun_rate_limit_atomik():
    app, c = _client(max_failed_logins=1000, rl_connect_per_account=4)
    codes = []
    barrier = threading.Barrier(10)

    def w(i):
        barrier.wait()
        codes.append(_post(app, 55500040, password="salah",
                           ip=IPS[i % 4]).status_code)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes.count(429) == 6, codes
    assert set(codes) <= {401, 409, 429}, codes


# ================================================== mt5 connect/status
def test_status_job_id_tidak_valid_berbagi_satu_ember():
    app, c = _client(rl_connect_status_per_job=3)
    codes = [_status(c, f"acak-{i}").status_code for i in range(5)]
    assert codes == [404] * 3 + [429] * 2, codes
    # job_id berformat valid = ember sendiri (tidak ikut 429)
    assert _status(c, uuid.uuid4().hex).status_code == 404


def test_status_per_job_dan_rem_global():
    app, c = _client(rl_connect_status_per_job=2,
                     rl_connect_status_global=5)
    jid = uuid.uuid4().hex
    assert [_status(c, jid, ip=ip).status_code for ip in IPS[:3]] == \
        [404, 404, 429]
    codes = [_status(c, uuid.uuid4().hex).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429], codes   # global: 2 + 3 = 5


def test_status_polling_normal_satu_job_tidak_terkena_limit():
    """Default produksi: polling klien (<= 150 s, interval >= 2 s => <= 75
    hit) untuk satu job tidak pernah menyentuh batas per job."""
    app, c = _client()
    jid = uuid.uuid4().hex
    codes = {_status(c, jid).status_code for _ in range(75)}
    assert codes == {404}, codes


# ============================================================ config
def test_config_default_dan_env(monkeypatch):
    for k in ("RATE_LOGIN_PER_EMAIL", "RATE_CONNECT_GLOBAL"):
        monkeypatch.delenv(k, raising=False)
    c = Config()
    assert (c.rl_login_per_email, c.rl_login_global) == (10, 300)
    assert (c.rl_register_per_email, c.rl_register_global_hour,
            c.rl_register_global_day) == (3, 30, 100)
    assert (c.rl_connect_per_account, c.rl_connect_global) == (10, 60)
    assert (c.rl_connect_status_per_job,
            c.rl_connect_status_global) == (150, 1500)
    monkeypatch.setenv("RATE_LOGIN_PER_EMAIL", "7")
    monkeypatch.setenv("RATE_CONNECT_GLOBAL", "0")        # dijepit >= 1
    monkeypatch.setenv("RATE_LOGIN_GLOBAL", "bukan-angka")  # -> default
    lc = load_config()
    assert lc.rl_login_per_email == 7
    assert lc.rl_connect_global == 1
    assert lc.rl_login_global == 300


def test_config_env_lama_berbasis_ip_diabaikan_dengan_warning(
        monkeypatch, caplog):
    monkeypatch.setenv("REGISTER_PER_IP_HOUR", "1")
    with caplog.at_level(logging.WARNING, logger="config"):
        lc = load_config()
    assert not hasattr(lc, "register_per_ip_hour")
    assert any("REGISTER_PER_IP_HOUR" in r.getMessage()
               for r in caplog.records)


def test_lockout_akun_mt5_ambang_sama_dengan_login():
    cfg = Config()
    a = authmod._account_lockout_for(cfg)
    b = authmod._lockout_for(cfg)
    assert a._max == b._max == cfg.max_failed_logins


# ============ D1: hit yang DITOLAK tidak memperpanjang pemblokiran
class _Clock:
    """Jam tiruan untuk auth.time.time (jendela throttle deterministik)."""

    def __init__(self, t):
        self.t = float(t)

    def time(self):
        return self.t


def _hits(cfg, key):
    rows = dbmod.get_db(cfg).execute(
        "SELECT bucket, hits FROM auth_throttle WHERE key_hash=?"
        " ORDER BY bucket", (authmod._key_hash(key),)).fetchall()
    return [(int(r["bucket"]), int(r["hits"])) for r in rows]


def _limiter_cfg():
    cfg = _cfg()
    _app(cfg)                       # migrasi DB
    return cfg


def test_d1_hit_ditolak_tidak_menambah_penghitung(monkeypatch):
    cfg = _limiter_cfg()
    clock = _Clock(10_000)          # awal jendela: bucket 100, porsi 0
    monkeypatch.setattr(authmod, "time", clock)
    conn = dbmod.get_db(cfg)
    lim = authmod.RateLimiter()
    res = [lim.hit("d1:a", 3, 100, conn=conn) for _ in range(3)]
    assert res == [True] * 3
    for i in range(50):                       # penyerang terus mencoba
        clock.t = 10_000 + i
        assert lim.hit("d1:a", 3, 100, conn=conn) is False
    assert _hits(cfg, "d1:a") == [(100, 3)]  # tetap 3, bukan 53


def test_d1_pemblokiran_tidak_diperpanjang_dibanding_kontrol(monkeypatch):
    """Kunci A diserang terus setelah batas; kunci B (kontrol) hanya
    mencapai batas. Keduanya harus bebas pada waktu yang SAMA."""
    cfg = _limiter_cfg()
    clock = _Clock(20_000)                     # bucket 200, porsi 0
    monkeypatch.setattr(authmod, "time", clock)
    conn = dbmod.get_db(cfg)
    lim = authmod.RateLimiter()
    for k in ("d1:A", "d1:B"):
        assert [lim.hit(k, 4, 100, conn=conn) for _ in range(4)] == [True] * 4
    for i in range(200):                       # hanya A yang digempur
        clock.t = 20_000 + (i % 100)
        assert lim.hit("d1:A", 4, 100, conn=conn) is False
    # awal jendela berikutnya: jendela geser masih penuh -> keduanya 429
    clock.t = 20_100
    assert lim.hit("d1:A", 4, 100, conn=conn) is False
    assert lim.hit("d1:B", 4, 100, conn=conn) is False
    # setengah jendela kemudian: kapasitas 4 - 4*0.5 = 2 untuk KEDUANYA
    clock.t = 20_150
    a = [lim.hit("d1:A", 4, 100, conn=conn) for _ in range(3)]
    b = [lim.hit("d1:B", 4, 100, conn=conn) for _ in range(3)]
    assert a == b == [True, True, False], (a, b)
    # sebagai pembanding: perilaku LAMA (hit ditolak ikut dihitung) membuat
    # A berisi 204 hit -> estimasi 204*0.5 = 102 > 4 -> tetap terblokir.


def test_d1_endpoint_login_bebas_setelah_jendela_walau_terus_dicoba(
        monkeypatch):
    clock = _Clock(30_000)                     # bucket 100 (w=300), porsi 0
    monkeypatch.setattr(authmod, "time", clock)
    app, c = _client(max_failed_logins=10 ** 6, rl_login_per_email=3)
    codes = [_login(c, "korban@t.id").status_code for _ in range(3)]
    assert codes == [401] * 3
    for i in range(60):                        # penyerang: 60 percobaan
        clock.t = 30_000 + i * 4               # tersebar di jendela
        assert _login(c, "korban@t.id").status_code == 429
    clock.t = 30_300 + 200                     # 2/3 jendela berikutnya
    # kapasitas 3 - 3*(1/3) = 2: pemilik kembali dilayani (401 = sampai
    # verifikasi password), tidak tertahan oleh 60 hit yang ditolak
    assert _login(c, "korban@t.id").status_code == 401


def test_d1_ditolak_lockout_tidak_menambah_kegagalan():
    """Permintaan yang ditolak LOCKOUT (429) tidak memperpanjang lockout:
    jumlah kegagalan/locked_until tidak berubah selama terkunci."""
    app, c = _client()
    assert _register(c, "lk2@t.id").status_code == 201
    cfg = app.config["APP_CFG"]
    for _ in range(cfg.max_failed_logins):
        _login(c, "lk2@t.id")
    q = ("SELECT failures, locked_until FROM auth_lockouts WHERE key_hash=?")
    kh = authmod._key_hash("lk2@t.id")
    before = dict(dbmod.get_db(cfg).execute(q, (kh,)).fetchone())
    for _ in range(20):
        assert _login(c, "lk2@t.id").status_code == 429
    after = dict(dbmod.get_db(cfg).execute(q, (kh,)).fetchone())
    assert after == before, (before, after)


def test_d1_konkurensi_sqlite_hit_ditolak_tidak_dihitung():
    cfg = _limiter_cfg()
    res = []
    barrier = threading.Barrier(16)

    def w():
        conn = dbmod.connect(cfg.db_file)
        barrier.wait()
        try:
            res.append(authmod.RateLimiter().hit("d1:race", 5, 300,
                                                 conn=conn))
        finally:
            conn.close()

    ts = [threading.Thread(target=w) for _ in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert res.count(True) == 5 and res.count(False) == 11, res
    assert [h for _, h in _hits(cfg, "d1:race")] == [5]


# ===== Atomik semua-atau-tidak: throttle(identitas + global) = 1 keputusan
def _sum_hits(cfg, key):
    return sum(h for _, h in _hits(cfg, key))


def test_atomik_global_menolak_identitas_tidak_terpakai():
    cfg = _limiter_cfg()
    conn = dbmod.get_db(cfg)
    for _ in range(2):
        authmod.throttle([("g:full", 2, 300)], conn=conn)
    for _ in range(5):
        with pytest.raises(authmod.AuthError) as ei:
            authmod.throttle([("id:x", 10, 300), ("g:full", 2, 300)],
                             conn=conn)
        assert ei.value.code == 429
    assert _sum_hits(cfg, "id:x") == 0          # dulu: 5 (terpakai)
    assert _sum_hits(cfg, "g:full") == 2


def test_atomik_identitas_menolak_global_tidak_terpakai():
    cfg = _limiter_cfg()
    conn = dbmod.get_db(cfg)
    authmod.throttle([("id:y", 1, 300), ("g:y", 100, 300)], conn=conn)
    for _ in range(5):
        with pytest.raises(authmod.AuthError):
            authmod.throttle([("id:y", 1, 300), ("g:y", 100, 300)],
                             conn=conn)
    assert _sum_hits(cfg, "id:y") == 1 and _sum_hits(cfg, "g:y") == 1


def test_atomik_semua_lolos_semua_bertambah_dan_indeks_penolak():
    cfg = _limiter_cfg()
    conn = dbmod.get_db(cfg)
    lim = authmod.RateLimiter()
    checks = [("a", 2, 300), ("b", 1, 300), ("c", 5, 300)]
    assert lim.hit_all(checks, conn=conn) is None
    assert [_sum_hits(cfg, k) for k in "abc"] == [1, 1, 1]
    assert lim.hit_all(checks, conn=conn) == 1       # 'b' menolak
    assert [_sum_hits(cfg, k) for k in "abc"] == [1, 1, 1]
    with pytest.raises(ValueError):
        lim.hit_all([("a", 2, 300), ("a", 2, 300)], conn=conn)


def test_atomik_jendela_geser_dan_d1_tetap(monkeypatch):
    """hit_all mempertahankan sliding window + D1 per kunci: global penuh
    menolak tanpa menulis; setengah jendela kemudian keduanya lolos."""
    cfg = _limiter_cfg()
    clock = _Clock(40_000)
    monkeypatch.setattr(authmod, "time", clock)
    conn = dbmod.get_db(cfg)
    lim = authmod.RateLimiter()
    for _ in range(4):
        assert lim.hit_all([("g4", 4, 100)], conn=conn) is None
    for i in range(30):
        clock.t = 40_000 + i
        assert lim.hit_all([("idz", 10, 100), ("g4", 4, 100)],
                           conn=conn) == 1
    assert _sum_hits(cfg, "idz") == 0 and _sum_hits(cfg, "g4") == 4
    clock.t = 40_100                      # jendela geser masih penuh
    assert lim.hit_all([("idz", 10, 100), ("g4", 4, 100)], conn=conn) == 1
    clock.t = 40_150                      # kapasitas global 4 - 4*0.5 = 2
    r = [lim.hit_all([("idz", 10, 100), ("g4", 4, 100)], conn=conn)
         for _ in range(3)]
    assert r == [None, None, 1], r
    assert _sum_hits(cfg, "idz") == 2


class _FailingConn:
    """Proksi koneksi: melempar error pada UPSERT ke-[n] (simulasi gagal di
    tengah transaksi). Atribut lain diteruskan ke koneksi asli."""

    def __init__(self, conn, n):
        self._c, self._n, self._seen = conn, n, 0

    def execute(self, sql, params=()):
        if "VALUES (?,?,?,1)" in sql:
            self._seen += 1
            if self._seen == self._n:
                raise RuntimeError("gagal disimulasikan")
        return self._c.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._c, name)


def test_atomik_exception_di_tengah_transaksi_rollback_total():
    cfg = _limiter_cfg()
    conn = dbmod.get_db(cfg)
    with pytest.raises(RuntimeError):
        authmod.throttle([("e1", 10, 300), ("e2", 10, 300)],
                         conn=_FailingConn(conn, 2))
    assert _sum_hits(cfg, "e1") == 0 and _sum_hits(cfg, "e2") == 0
    assert not conn.in_transaction                  # koneksi bersih
    authmod.throttle([("e1", 10, 300), ("e2", 10, 300)], conn=conn)
    assert _sum_hits(cfg, "e1") == _sum_hits(cfg, "e2") == 1


def _thread_race(cfg, jobs):
    res, lock = [], threading.Lock()
    barrier = threading.Barrier(len(jobs))

    def w(checks):
        conn = dbmod.connect(cfg.db_file)
        barrier.wait()
        try:
            authmod.throttle(checks, conn=conn)
            r = "ok"
        except authmod.AuthError as e:
            r = e.code
        finally:
            conn.close()
        with lock:
            res.append(r)

    ts = [threading.Thread(target=w, args=(j,)) for j in jobs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return res


def test_atomik_konkurensi_thread_sqlite_identitas_tidak_bocor():
    cfg = _limiter_cfg()
    jobs = [[(f"tid:{i}", 10, 300), ("tg", 7, 300)] for i in range(16)]
    res = _thread_race(cfg, jobs)
    assert res.count("ok") == 7 and res.count(429) == 9, res
    assert sum(_sum_hits(cfg, f"tid:{i}") for i in range(16)) == 7
    assert _sum_hits(cfg, "tg") == 7


def test_atomik_urutan_terbalik_tanpa_deadlock_sqlite():
    cfg = _limiter_cfg()
    jobs = ([[("A", 1000, 300), ("B", 1000, 300)]] * 12 +
            [[("B", 1000, 300), ("A", 1000, 300)]] * 12)
    res = _thread_race(cfg, jobs)
    assert res == ["ok"] * 24, res
    assert _sum_hits(cfg, "A") == _sum_hits(cfg, "B") == 24


_PROC_WORKER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import auth as authmod, db as dbmod
conn = (dbmod.connect_postgres(sys.argv[2]) if sys.argv[2].startswith("post")
        else dbmod.connect(sys.argv[2]))
out = []
for i in range(int(sys.argv[3])):
    try:
        authmod.throttle([("pid:%s_%d" % (sys.argv[4], i), 10, 300),
                          ("pg_global", 7, 300)], conn=conn)
        out.append("ok")
    except authmod.AuthError as e:
        out.append(e.code)
conn.close()
print(json.dumps(out))
"""


def run_throttle_procs(target, n_proc, per_proc):
    """[n_proc] PROSES terpisah memanggil throttle(identitas unik + global
    limit 7) pada DB yang sama (path SQLite atau URI PostgreSQL)."""
    import json
    import subprocess
    import sys
    from pathlib import Path
    backend = str(Path(__file__).resolve().parent.parent)
    procs = [subprocess.Popen(
        [sys.executable, "-c", _PROC_WORKER, backend, target,
         str(per_proc), str(i)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True) for i in range(n_proc)]
    out = []
    for p in procs:
        so, se = p.communicate(timeout=120)
        assert p.returncode == 0, se[-500:]
        out += json.loads(so.strip().splitlines()[-1])
    return out


def test_atomik_lintas_proses_sqlite():
    cfg = _limiter_cfg()
    dbmod.close_db()
    res = run_throttle_procs(str(cfg.db_file), 4, 5)
    assert res.count("ok") == 7 and res.count(429) == 13, res
    conn = dbmod.connect(cfg.db_file)
    try:
        g = conn.execute("SELECT SUM(hits) AS n FROM auth_throttle WHERE"
                         " key_hash=?", (authmod._key_hash("pg_global"),)
                         ).fetchone()["n"]
        tot = conn.execute("SELECT SUM(hits) AS n FROM auth_throttle"
                           ).fetchone()["n"]
    finally:
        conn.close()
    assert g == 7 and tot == 14       # 7 global + 7 identitas (tanpa bocor)
