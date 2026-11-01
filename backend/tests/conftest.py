"""Fixture pytest backend Jurnal MT5 — semua pakai MockMT5Client."""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

import db as dbmod  # noqa: E402
from app import create_app  # noqa: E402
from config import Config  # noqa: E402
from mt5client.mock import MockMT5Client, scenario_dataset  # noqa: E402
from services.sync import SyncService  # noqa: E402


@pytest.fixture()
def cfg(tmp_path):
    c = Config()
    c.db_path = str(tmp_path / "journal.db")
    c.api_token = "TESTTOKEN"
    c.mt5_enabled = False
    c.mt5_password = "SUPER_SECRET_PASSWORD"  # untuk test kebocoran
    return c


@pytest.fixture()
def make_client(cfg):
    def _make(dataset=None, **mock_kw):
        app = create_app(
            cfg,
            provider_factory=lambda: MockMT5Client(dataset, **mock_kw),
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
