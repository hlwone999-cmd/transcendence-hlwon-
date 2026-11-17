"""TEMUAN AUDIT ROUND-3 #4 — /api/v1/health sebagai READINESS check.

Sebelumnya health hanya HTTP 200 tanpa memeriksa apa pun. Sekarang:
- 200 hanya bila koneksi database hidup DAN skema minimum ada
  (brief §5: customers, users, auth_tokens, mt5_accounts,
  mt5_credentials, mt5_terminals, mt5_deals, mt5_orders);
- 503 bila DB tidak terjangkau / tabel inti hilang;
- status terminal MT5 DILAPORKAN TERPISAH dan TIDAK pernah
  menentukan 200/503 (tidak ada kewajiban MT5 live utk health);
- GET /api/v1/admin/health/terminals: probe per customer (admin).
"""
import pytest

from tests.conftest import bind_account, register_and_login


def test_health_ok_saatschema_lengkap(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["database"]["ok"] is True
    assert body["database"]["engine"] == "sqlite"
    # tidak ada missing_tables saat skema lengkap
    assert "missing_tables" not in body["database"]
    # terminal dilaporkan terpisah, bukan penentu health
    assert "mt5_terminals" in body
    assert body["mt5_terminals"]["reported"] == 0  # belum ada terminal


def test_health_503_bila_tabel_inti_hilang(client, cfg, monkeypatch):
    """Skema minimum tidak lengkap -> 503 (bukan 200 kosongan)."""
    import db as dbmod
    conn = dbmod.get_db(cfg)
    conn.execute("DROP TABLE mt5_orders")
    conn.commit()
    r = client.get("/api/v1/health")
    assert r.status_code == 503
    body = r.get_json()
    assert body["ok"] is False
    assert body["database"]["ok"] is False
    assert body["database"]["missing_tables"] == ["mt5_orders"]
    # terminal tetap dilaporkan walau tidak ready
    assert "mt5_terminals" in body
    conn.execute("CREATE TABLE mt5_orders (id INTEGER PRIMARY KEY)")
    conn.commit()


def test_health_503_bila_db_tak_terjangkau(client, monkeypatch):
    """Koneksi database gagal -> 503 dengan pesan error jelas."""
    import db as dbmod

    def boom(_cfg):
        raise RuntimeError("koneksi database gagal (simulasi)")

    monkeypatch.setattr(dbmod, "get_db", boom)
    r = client.get("/api/v1/health")
    assert r.status_code == 503
    body = r.get_json()
    assert body["ok"] is False
    assert body["database"]["ok"] is False
    assert "RuntimeError" in body["database"]["error"]
    assert "koneksi database gagal" in body["database"]["error"]


def test_health_tidak_mewajibkan_mt5_live(make_client):
    """Terminal tidak online pun backend tetap 200 (audit: jangan
    wajibkan MT5 live untuk health backend)."""
    client = make_client()  # mock provider, tidak ada terminal RPyC hidup
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["database"]["ok"] is True
    # tidak ada field yang mengklaim MT5 hidup
    assert body["mt5_terminals"]["reported"] == 0


def test_health_ringkas_tanpa_kredensial(client):
    """Respons health tidak boleh membocorkan kredensial/konfigurasi."""
    r = client.get("/api/v1/health")
    text = r.get_data(as_text=True).lower()
    for forbidden in ("password", "secret", "dsn", "postgresql://"):
        assert forbidden not in text, forbidden


def test_admin_health_terminals_butuh_token_admin(client):
    r = client.get("/api/v1/admin/health/terminals")
    assert r.status_code == 401
    # token customer biasa TIDAK cukup
    h = register_and_login(client)
    r = client.get("/api/v1/admin/health/terminals", headers=h)
    assert r.status_code == 401


def test_admin_health_terminals_probe_jujur(client, cfg):
    """Dengan token admin: probe per customer; tanpa listener RPyC
    sungguhan statusnya reachable=False (TIDAK klaim live MT5)."""
    h = register_and_login(client)
    bind_account(client, h)
    r = client.get("/api/v1/admin/health/terminals",
                   headers={"X-API-Token": cfg.api_token})
    assert r.status_code == 200
    items = r.get_json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["customer_id"] == 1
    # probe TCP+RPyC pada port tanpa listener: jujur tidak reachable
    assert it["reachable"] is False
    assert it["rpyc_handshake"] is False
    # nilai terminal tercatat di DB (port dialokasikan saat bind)
    assert isinstance(it["port"], int)


def test_health_bersihkan_scope_setelah_request(client):
    """Smoke SQLite: request health selesai tidak meninggalkan transaksi
    terbuka pada koneksi thread-local (di PG context customer juga
    dibersihkan — dites lengkap di test_rls_no_leak_requests)."""
    import db as dbmod
    client.get("/api/v1/health")
    conn = getattr(dbmod._local, "conn", None)
    assert conn is not None
    assert conn.in_transaction is False, \
        "koneksi tidak boleh idle-in-transaction setelah request selesai"
