"""Flask app factory Jurnal MT5."""
from __future__ import annotations

import logging

from flask import Flask

import db as dbmod
from api.routes import api_bp
from config import Config
from services.scheduler import SyncScheduler
from services.sync import SyncService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Jangan pernah biarkan traceback log menyertakan argumen kredensial.
logging.getLogger("rpyc").setLevel(logging.WARNING)


def create_app(config: Config | None = None,
               provider_factory=None) -> Flask:
    from config import load_config
    config = config or load_config()

    app = Flask(__name__)
    app.config["APP_CFG"] = config

    with dbmod.connect(config.db_file) as conn:
        applied = dbmod.migrate(conn)
    if applied:
        logging.getLogger(__name__).info("migrasi diterapkan: %s", applied)

    app.register_blueprint(api_bp)

    factory = provider_factory or _provider_factory(config)
    app.extensions["mt5_provider_factory"] = factory
    sync_service = SyncService(config, factory)
    app.extensions["mt5_sync"] = sync_service
    scheduler = SyncScheduler(config, sync_service)
    app.extensions["mt5_scheduler"] = scheduler

    @app.teardown_appcontext
    def _close_db(_exc):  # koneksi per-thread dipertahankan antar request
        return None

    return app


def _provider_factory(config: Config):
    def factory():
        if config.mt5_provider == "json":
            from mt5client.json_provider import JSONProvider
            return JSONProvider(config)

        from mt5client.mt5linux_provider import MT5LinuxProvider
        return MT5LinuxProvider(config)
    return factory
