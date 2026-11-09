"""AUDIT ROUND-3 (tambahan) — tidak ada kebocoran scope RLS antar request.

Yang dibuktikan pada PostgreSQL SUNGGUHAN (pgserver) lewat Flask test
client (alur request nyata, koneksi thread-local dipakai ulang antar
request seperti produksi):

1. Request customer A (scope terpasang) -> setelah request selesai,
   scope WAJIB sudah lepas: memori Python None + GUC session kosong,
   WALAU transaksi baru di-commit pada koneksi yang sama (reapply
   tidak memasang ulang context lama).
2. Request A -> request UNAUTH berikutnya (register/login/401/health)
   tidak mewarisi scope A (default-deny; commit berikutnya tidak
   menghidupkan kembali GUC A).
3. Request A -> request ADMIN berikutnya tidak mewarisi scope A dan
   admin tanpa context benar-benar melihat 0 baris tabel ber-RLS.
4. Cleanup TIDAK membatalkan transaksi yang sudah berhasil: data yang
   di-commit selama request tetap ada setelah teardown.
5. Negative control (bukti celah nyata): bila cleanup dimatikan,
   scope A MENINGGAL pada koneksi dan commit berikutnya memasang ulang
   GUC A — jadi test di atas bukan kosong.
6. Worker sinkronisasi (sync_account) juga membersihkan scope di
   finally — thread koneksi jangka panjang tidak membawa scope akun
   sebelumnya ke tugas berikutnya.
7. AUDIT ROUND-4 #1: sync_account dengan LOCK GAGAL (sync lain masih
   berjalan) TIDAK BOLEH memasang scope sama sekali — versi lama
   memasang context SEBELUM lock sehingga SyncLockBusy (yang dilempar
   di luar try/finally) meninggalkan scope customer di koneksi.
"""
import uuid

import pytest

pgserver = pytest.importorskip(
    "pgserver", reason="pgserver tidak terpasang — bukti no-leak PG dilewati")
psycopg2 = pytest.importorskip(
    "psycopg2", reason="psycopg2 tidak terpasang — bukti no-leak PG dilewati")

import db as dbmod  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from services.sync import SyncLockBusy  # noqa: E402
from tests.conftest import bind_account, register_and_login  # noqa: E402

GUC = dbmod.CUSTOMER_GUC


@pytest.fixture(scope="module")
def pg_server():
    import tempfile

    import pgserver
    d = tempfile.mkdtemp(prefix="pgmc-noleak-")
    return pgserver.get_server(d)


@pytest.fixture()
def pg_cfg(pg_server, tmp_path):
    """Config PostgreSQL nyata; database segar per test."""
    from config import Config
    name = f"noleak_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(pg_server.get_uri())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    cfg = Config()
    # registrasi portal diaktifkan eksplisit (default produksi juga aktif)
    cfg.public_register_enabled = True
    cfg.db_url = pg_server.get_uri().replace("/postgres?", f"/{name}?")
    cfg.db_path = str(tmp_path / "unused.db")
    cfg.api_token = "TESTTOKEN"
    cfg.mt5_enabled = True
    cfg.cred_key = Fernet.generate_key().decode("ascii")
    cfg.term_base_dir = str(tmp_path / "terminals")
    cfg.terminal_provision_mode = "mock"
    cfg.max_customers = 4
    yield cfg
    dbmod.close_db()
    admin = psycopg2.connect(pg_server.get_uri())
    admin.autocommit = True
    admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}"')
    admin.close()


def _client(cfg, datasets=None):
    from app import create_app
    from mt5client.mock import MockMT5Client
    datasets = datasets or {}
    app = create_app(cfg, provider_factory=lambda acc_cfg=None:
                     MockMT5Client(
                         datasets.get(str(acc_cfg.mt5_login))
                         if acc_cfg else None))
    app.config["TESTING"] = True
    return app.test_client()


def _thread_conn(cfg):
    """Koneksi thread-local yang PAKAI-ULANG antar request (seperti
    worker gunicorn: satu thread melayani banyak request berurutan)."""
    return dbmod.get_db(cfg)


def _guc(conn) -> str:
    row = conn.execute(
        "SELECT current_setting(%s, true) AS v", (GUC,)).fetchone()
    return row["v"]


def test_no_leak_antar_request_a_ke_b(pg_cfg):
    c = _client(pg_cfg)
    hA = register_and_login(c, email="lea@test.id")
    hB = register_and_login(c, email="leb@test.id")
    bind_account(c, hA, login=11111111, server="Exness-MT5Real")
    bind_account(c, hB, login=22222222, server="BrokerLain-MT5Demo")

    # request A: scope A aktif SELAMA request (terbukti dari hasil)
    ra = c.get("/api/v1/accounts", headers=hA)
    assert ra.status_code == 200
    assert int(ra.get_json()["account"]["login"]) == 11111111

    # TEARDOWN WAJIB sudah melepas scope pada koneksi thread-local
    conn = _thread_conn(pg_cfg)
    assert conn.current_customer_context() is None, \
        "scope customer masih tertinggal di memori koneksi setelah request"
    # transaksi BARU + commit pada koneksi yang sama: GUC tetap kosong
    conn.execute("SELECT 1")
    conn.commit()
    assert _guc(conn) in ("", None), \
        "GUC app.customer_id harus kosong untuk request berikutnya"

    # request B pada koneksi YANG SAMA: hanya melihat data B
    rb = c.get("/api/v1/accounts", headers=hB)
    assert rb.status_code == 200
    acc_b = rb.get_json()["account"]
    assert acc_b is not None and int(acc_b["login"]) == 22222222, \
        "request B mewarisi data/scope request A — KEBOCORAN"
    assert conn.current_customer_context() is None


def test_request_customer_lalu_unauth_tidak_mewarisi_scope(pg_cfg):
    c = _client(pg_cfg)
    hA = register_and_login(c, email="una@test.id")
    bind_account(c, hA, login=33333333, server="Exness-MT5Real")
    assert c.get("/api/v1/accounts", headers=hA).status_code == 200

    # request TANPA token: 401 + register/login — koneksi thread yang
    # sama dipakai lagi; scope A tidak boleh ikut.
    r401 = c.get("/api/v1/auth/me")            # tanpa token -> 401
    assert r401.status_code == 401
    rlogin = c.post("/api/v1/auth/login",
                    json={"email": "una@test.id", "password": "SALAH"})
    assert rlogin.status_code == 401           # login gagal (audit ditulis)
    rhe = c.get("/api/v1/health")
    assert rhe.status_code == 200

    conn = _thread_conn(pg_cfg)
    assert conn.current_customer_context() is None
    # bukti kuat: commit transaksi baru TIDAK menghidupkan kembali GUC A
    conn.execute("SELECT 1")
    conn.commit()
    assert _guc(conn) in ("", None), \
        "request unauth mewarisi scope customer sebelumnya — KEBOCORAN"


def test_request_customer_lalu_admin_tidak_mewarisi_scope(pg_cfg, pg_server):
    from tests.test_multi_customer import _ds_two_trades
    c = _client(pg_cfg, datasets={
        "44444444": _ds_two_trades(44444444, "Exness-MT5Real", "XAUUSD",
                                    0.01, 2, 44, 10.0)})
    hA = register_and_login(c, email="adm@test.id")
    bind_account(c, hA, login=44444444, server="Exness-MT5Real")
    r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=hA)
    assert r.get_json()["status"] == "ok"
    assert c.get("/api/v1/accounts", headers=hA).status_code == 200

    # request ADMIN (bootstrap token) pada thread yang sama
    ha = {"X-API-Token": pg_cfg.api_token}
    radm = c.get("/api/v1/admin/customers", headers=ha)
    assert radm.status_code == 200

    conn = _thread_conn(pg_cfg)
    assert conn.current_customer_context() is None
    conn.execute("SELECT 1")
    conn.commit()
    assert _guc(conn) in ("", None), \
        "request admin mewarisi scope customer sebelumnya — KEBOCORAN"
    # admin tanpa context pada tabel ber-RLS: default-deny 0 baris.
    # Dibuktikan lewat role aplikasi transcendence_app (koneksi owner
    # PostgreSQL memang bypass RLS — semantik PG, bukan jalur app).
    app_role = dbmod.connect_postgres(
        pg_cfg.db_url.replace("postgres:@", "transcendence_app:@"))
    n = int(app_role.execute(
        "SELECT COUNT(*) AS n FROM mt5_deals").fetchone()["n"])
    assert n == 0, "admin tanpa context tidak boleh melihat baris ber-RLS"
    app_role.close()


def test_cleanup_tidak_membatalkan_transaksi_sudah_berhasil(pg_cfg):
    """Data yang di-commit selama request TETAP ADA setelah teardown
    cleanup berjalan (rollback teardown hanya no-op pada idle conn)."""
    from tests.test_multi_customer import _ds_two_trades
    c = _client(pg_cfg, datasets={
        "55555555": _ds_two_trades(55555555, "Exness-MT5Real", "XAUUSD",
                                    0.01, 2, 55, 10.0)})
    hA = register_and_login(c, email="cm@test.id")
    bind_account(c, hA, login=55555555, server="Exness-MT5Real")
    r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=hA)
    assert r.get_json()["status"] == "ok"
    # request sync sudah selesai + teardown cleanup sudah jalan:
    # data hasil sync WAJIB tetap utuh.
    conn = _thread_conn(pg_cfg)
    n = int(conn.execute(
        "SELECT COUNT(*) AS n FROM mt5_deals").fetchone()["n"])
    assert n > 0, "cleanup tidak boleh menghapus transaksi yang sudah commit"
    n_users = int(conn.execute(
        "SELECT COUNT(*) AS n FROM users").fetchone()["n"])
    assert n_users == 1


def test_negatif_tanpa_cleanup_scope_menyisa(pg_cfg, monkeypatch):
    """Bukti celah nyata: bila cleanup dimatikan, scope A tinggal di
    koneksi dan commit transaksi berikutnya memasang ulang GUC A —
    persis celah yang ditutup teardown (test-test di atas)."""
    monkeypatch.setattr(dbmod, "clear_customer_context",
                        lambda conn: None)   # simulasi tanpa perbaikan
    c = _client(pg_cfg)
    hA = register_and_login(c, email="neg@test.id")
    bind_account(c, hA, login=66666666, server="Exness-MT5Real")
    assert c.get("/api/v1/accounts", headers=hA).status_code == 200
    conn = _thread_conn(pg_cfg)
    id_a = int(conn.execute(
        "SELECT id FROM customers WHERE code IS NOT NULL"
        " ORDER BY id LIMIT 1").fetchone()["id"])
    assert conn.current_customer_context() == id_a, \
        "negative control: scope harusnya MENYISA tanpa cleanup"
    conn.execute("SELECT 1")
    conn.commit()      # memicu reapply context lama
    assert _guc(conn) == str(id_a), \
        "negative control: commit tanpa cleanup menghidupkan GUC lama"
    monkeypatch.undo()


def test_worker_sync_bersihkan_scope_di_finally(pg_cfg):
    """Worker sync: setelah sync_account selesai (sukses maupun gagal),
    scope customer akun lepas dari koneksi thread worker."""
    from tests.test_multi_customer import _ds_two_trades

    # dataset mock dipilih per login akun (pola sama dgn test e2e PG)
    c = _client(pg_cfg, datasets={
        "77777777": _ds_two_trades(77777777, "Exness-MT5Real", "XAUUSD",
                                    0.01, 2, 77, 10.0)})
    hA = register_and_login(c, email="wrk@test.id")
    bind_account(c, hA, login=77777777, server="Exness-MT5Real")
    r = c.post("/api/v1/mt5/sync", json={"mode": "full"}, headers=hA)
    assert r.get_json()["status"] == "ok", r.get_json()
    conn = _thread_conn(pg_cfg)
    acc_id = int(conn.execute(
        "SELECT id FROM mt5_accounts").fetchone()["id"])
    sync = c.application.extensions["mt5_sync"]
    summary = sync.sync_account(acc_id)
    assert summary["status"] == "ok"
    assert conn.current_customer_context() is None, \
        "worker sync meninggalkan scope customer setelah tugas selesai"
    conn.execute("SELECT 1")
    conn.commit()
    assert _guc(conn) in ("", None)


def test_sync_lock_busy_tidak_meninggalkan_scope(pg_cfg):
    """AUDIT ROUND-4 #1 — lock gagal -> SyncLockBusy dan scope RLS
    benar-benar bersih di level koneksi PostgreSQL (memori Python None
    + GUC session kosong walau transaksi baru di-commit).

    Versi lama: set_customer_context() dipanggil SEBELUM rt.lock.
    acquire(); SyncLockBusy dilempar di luar try/finally sehingga
    clear_customer_context tidak pernah berjalan — scope customer
    TERTINGGAL di koneksi thread-local. Kontrol negatif di akhir test
    membuktikan mekanisme leak itu nyata dan terdeteksi oleh assert
    yang sama."""
    from tests.test_multi_customer import _ds_two_trades

    c = _client(pg_cfg, datasets={
        "88888888": _ds_two_trades(88888888, "Exness-MT5Real", "XAUUSD",
                                    0.01, 2, 88, 10.0)})
    hA = register_and_login(c, email="lkb@test.id")
    bind_account(c, hA, login=88888888, server="Exness-MT5Real")
    conn = _thread_conn(pg_cfg)
    acc_id = int(conn.execute(
        "SELECT id FROM mt5_accounts").fetchone()["id"])
    sync = c.application.extensions["mt5_sync"]
    rt = sync._registry.runtime_for(conn, acc_id)

    # pihak lain memegang lock akun ini -> sync_account wajib gagal cepat
    assert rt.lock.acquire(blocking=False)
    with pytest.raises(SyncLockBusy):
        sync.sync_account(acc_id)

    # scope wajib bersih: memori Python + GUC setelah transaksi baru
    assert conn.current_customer_context() is None, \
        "lock gagal: scope customer tertinggal di koneksi (leak round-4 #1)"
    conn.execute("SELECT 1")
    conn.commit()          # reapply tidak boleh menghidupkan GUC lama
    assert _guc(conn) in ("", None), \
        "lock gagal: GUC app.customer_id tidak boleh terisi"

    # KONTROL NEGATIF (bukti assert menangkap bug asli): pasang scope
    # lalu biarkan tertinggal (persis perilaku urutan lama) — detektor
    # WAJIB melihatnya. Setelah itu dibersihkan kembali.
    dbmod.set_customer_context(conn, rt.customer_id)
    assert conn.current_customer_context() == rt.customer_id, \
        "kontrol negatif: detektor memori wajib melihat scope yang sengaja " \
        "ditinggalkan (persis leak urutan lama)"
    conn.execute("SELECT 1")
    conn.commit()
    assert _guc(conn) == str(rt.customer_id), \
        "kontrol negatif: GUC wajib terlihat saat scope ditinggalkan"
    dbmod.clear_customer_context(conn)
    rt.lock.release()
