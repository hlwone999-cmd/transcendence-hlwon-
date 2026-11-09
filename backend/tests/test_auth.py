"""Auth customer — brief §11 #2 (registrasi & login) dan #3 (token
invalid/expired/revoked), plus rate limit, lockout, dan validasi."""
import time

from cryptography.fernet import Fernet

import auth as authmod
from tests.conftest import bind_account, register_and_login


# ------------------------------------------------- #2 registrasi & login
def test_register_and_login_ok(client):
    r = client.post("/api/v1/auth/register",
                    json={"email": "budi@test.id", "password": "rahasia123"})
    assert r.status_code == 201
    assert r.get_json()["customer_code"].startswith("C")
    r = client.post("/api/v1/auth/login",
                    json={"email": "budi@test.id", "password": "rahasia123"})
    assert r.status_code == 200
    tok = r.get_json()["token"]
    assert len(tok) >= 32
    # token valid -> /auth/me mengenali customer
    me = client.get("/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {tok}"})
    assert me.status_code == 200
    assert me.get_json()["customer"]["code"].startswith("C")


def test_register_email_duplikat_ditolak(client):
    client.post("/api/v1/auth/register",
                json={"email": "dua@test.id", "password": "rahasia123"})
    r = client.post("/api/v1/auth/register",
                    json={"email": "dua@test.id", "password": "rahasia456"})
    assert r.status_code == 409


def test_register_password_terlalu_pendek(client):
    r = client.post("/api/v1/auth/register",
                    json={"email": "pendek@test.id", "password": "123"})
    assert r.status_code == 400


def test_login_password_salah(client):
    client.post("/api/v1/auth/register",
                json={"email": "salah@test.id", "password": "rahasia123"})
    r = client.post("/api/v1/auth/login",
                    json={"email": "salah@test.id", "password": "bukan"})
    assert r.status_code == 401


def test_login_email_tidak_ada(client):
    r = client.post("/api/v1/auth/login",
                    json={"email": "hantu@test.id", "password": "rahasia123"})
    assert r.status_code == 401


def test_password_hash_tidak_plaintext(client, cfg, conn):
    client.post("/api/v1/auth/register",
                json={"email": "hash@test.id", "password": "rahasia123"})
    row = conn.execute("SELECT password_hash FROM users").fetchone()
    assert "rahasia123" not in row["password_hash"]
    # werkzeug 3.x: scrypt (default) atau pbkdf2:sha256 — keduanya salted KDF
    assert row["password_hash"].startswith(("scrypt:", "pbkdf2:"))


# ------------------------------------ #3 token invalid / expired / revoked
def test_token_invalid_ditolak(client):
    r = client.get("/api/v1/journal/stats",
                   headers={"Authorization": "Bearer BUKANTOKEN"})
    assert r.status_code == 401


def test_token_expired_ditolak(client, cfg):
    hdrs = register_and_login(client)
    # kedaluwarsakan SEMUA token langsung di DB
    import db as dbmod
    conn = dbmod.get_db(cfg)
    conn.execute("UPDATE auth_tokens SET expires_at='2000-01-01T00:00:00Z'")
    conn.commit()
    r = client.get("/api/v1/auth/me", headers=hdrs)
    assert r.status_code == 401


def test_logout_merevoke_token(client):
    hdrs = register_and_login(client)
    assert client.get("/api/v1/auth/me", headers=hdrs).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=hdrs).status_code == 200
    # token yang sudah logout -> 401
    assert client.get("/api/v1/auth/me", headers=hdrs).status_code == 401


def test_token_disimpan_sebagai_hash(client, cfg):
    hdrs = register_and_login(client)
    token = hdrs["Authorization"].split(" ", 1)[1]
    import db as dbmod
    conn = dbmod.get_db(cfg)
    rows = [r["token_hash"] for r in conn.execute(
        "SELECT token_hash FROM auth_tokens")]
    assert rows, "token harus tercatat"
    assert token not in rows                       # asli tidak pernah disimpan
    assert authmod._hash_token(token) in rows      # hash-nya yang disimpan


# ------------------------------------------------------- rate limit/lockout
def test_rate_limit_login(client):
    for _ in range(10):  # batas 10/5 menit per IP
        client.post("/api/v1/auth/login",
                    json={"email": "takada@test.id", "password": "x" * 12})
    r = client.post("/api/v1/auth/login",
                    json={"email": "takada@test.id", "password": "x" * 12})
    assert r.status_code == 429


def test_lockout_setelah_login_gagal_berulang(client):
    client.post("/api/v1/auth/register",
                json={"email": "lock@test.id", "password": "rahasia123"})
    for _ in range(5):  # max_failed_logins = 5
        r = client.post("/api/v1/auth/login",
                        json={"email": "lock@test.id", "password": "bukan"})
        assert r.status_code == 401
    # password BENAR pun ditolak selama lockout
    r = client.post("/api/v1/auth/login",
                    json={"email": "lock@test.id", "password": "rahasia123"})
    assert r.status_code == 429


# ------------------------------------------------- batas kapasitas (§12)
def test_registrasi_tidak_memakai_slot_mt5(make_client, cfg):
    """Keputusan produk #1: registrasi publik tetap aktif dan TIDAK
    memakai slot MT5 (max_customers=4 kini = slot terminal)."""
    c = make_client()
    for i in range(6):
        r = c.post("/api/v1/auth/register",
                   json={"email": f"cust{i}@test.id",
                         "password": "rahasia123"},
                   headers={"X-Real-IP": f"198.51.{i}.10"})
        assert r.status_code == 201, r.get_json()
    import db as dbmod
    conn = dbmod.get_db(cfg)
    assert conn.execute("SELECT COUNT(*) AS n FROM mt5_terminals"
                        ).fetchone()["n"] == 0


def test_maks_akun_portal_bila_dikonfigurasi(make_client, cfg):
    """Batas akun portal opsional (max_registered_customers)."""
    cfg.max_registered_customers = 4
    c = make_client()
    for i in range(4):
        r = c.post("/api/v1/auth/register",
                   json={"email": f"cust{i}@test.id",
                         "password": "rahasia123"},
                   headers={"X-Real-IP": f"198.51.{i}.10"})
        assert r.status_code == 201
    r = c.post("/api/v1/auth/register",
               json={"email": "cust5@test.id", "password": "rahasia123"},
               headers={"X-Real-IP": "198.51.9.10"})
    assert r.status_code == 403
