"""Fixture pytest backend Jurnal MT5.

- Test lama (svc/conn/client) tetap jalan: SQLite + MockMT5Client.
- Test multi-customer memakai register/login/bind via HTTP (alur nyata).
- Rate limiter & lockout direset tiap test agar deterministik.
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

import db as dbmod  # noqa: E402
from app import create_app  # noqa: E402
from config import Config  # noqa: E402
from mt5client.mock import MockMT5Client, scenario_dataset  # noqa: E402
from services.sync import SyncService  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limiting():
    """Rate limiter & lockout kini PERSISTEN di database (auth_throttle /
    auth_lockouts) — tiap test memakai DB segar sehingga otomatis
    terisolasi; tidak ada state in-memory yang perlu direset."""
    yield


@pytest.fixture()
def cfg(tmp_path):
    c = Config()
    # registrasi portal diaktifkan eksplisit (default produksi juga aktif)
    c.public_register_enabled = True
    c.db_path = str(tmp_path / "journal.db")
    c.api_token = "TESTTOKEN"          # hanya dipakai test endpoint /admin
    c.mt5_enabled = False
    c.mt5_password = "SUPER_SECRET_PASSWORD"  # untuk test kebocoran
    c.cred_key = Fernet.generate_key().decode("ascii")
    c.term_base_dir = str(tmp_path / "terminals")
    c.terminal_provision_mode = "mock"
    c.max_customers = 4
    return c


@pytest.fixture()
def make_client(cfg):
    def _make(dataset=None, **mock_kw):
        app = create_app(
            cfg,
            provider_factory=lambda *a, **k: MockMT5Client(dataset, **mock_kw),
        )
        app.config["TESTING"] = True
        return app.test_client()
    return _make


@pytest.fixture()
def client(make_client):
    return make_client()


@pytest.fixture()
def svc(cfg):
    return SyncService(cfg, lambda: MockMT5Client())


@pytest.fixture()
def conn(cfg):
    dbmod.close_db()
    c = dbmod.connect(cfg.db_file)
    dbmod.migrate(c)
    yield c
    c.close()
    dbmod.close_db()


@pytest.fixture()
def synced(svc, conn):
    """Jalankan sync awal dengan skenario standar; return summary."""
    summary = svc.sync(mode="initial")
    assert summary["status"] == "ok", summary
    return summary


# ------------------------------------------------- helper multi-customer
def register_and_login(client, email="c1@test.id",
                       password="rahasia123") -> dict:
    """Register + login via endpoint nyata. Return headers sesi."""
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": password})
    assert r.status_code == 201, r.get_json()
    r = client.post("/api/v1/auth/login",
                    json={"email": email, "password": password})
    assert r.status_code == 200, r.get_json()
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def bind_account(client, headers, login=12345678, server="Exness-MT5Real",
                 password="MT5_PASSWORD_X") -> dict:
    """Bind 1 akun MT5 via endpoint nyata. Return body (tanpa password)."""
    r = client.post("/api/v1/accounts/bind", headers=headers,
                    json={"login": login, "server": server,
                          "password": password})
    assert r.status_code == 201, r.get_json()
    return r.get_json()
