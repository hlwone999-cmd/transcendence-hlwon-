"""Flask app factory Jurnal MT5 — multi-customer (PostgreSQL).

Wire-up:
- TerminalRegistry : runtime provider + terminal per customer (brief §4)
- SyncService      : sync legacy + sync_account per-akun (brief §6)
- MultiAccountScheduler: scheduler per-akun (isolasi gagal + backoff)
Kompatibilitas: provider_factory injeksi test tetap didukung (mock).
"""
from __future__ import annotations

import logging

from flask import Flask

import db as dbmod
from api.routes import api_bp
from config import Config
from services.scheduler import MultiAccountScheduler, SyncScheduler
from services.sync import SyncService
from services.terminals import TerminalRegistry

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

    with dbmod.get_db(config) as conn:
        applied = dbmod.migrate(conn)
    if applied:
        logging.getLogger(__name__).info("migrasi diterapkan: %s", applied)

    app.register_blueprint(api_bp)

    factory = provider_factory or _provider_factory(config)
    app.extensions["mt5_provider_factory"] = factory  # kompatibilitas lama
    # sync awal otomatis hanya di produksi (tanpa injeksi test) agar
    # perilaku test deterministik
    app.extensions["auto_initial_sync"] = provider_factory is None

    registry = TerminalRegistry(config,
                                provider_builder=factory if provider_factory
                                else None)
    app.extensions["mt5_registry"] = registry

    # Keputusan produk #3: auto-provisioning terminal lewat helper
    # privileged terbatas (mode real) / MockProvisioner (dev/test).
    from services.provisioner import build_provisioner
    from services.provisioning import ConnectCoordinator
    provisioner = build_provisioner(config)
    app.extensions["mt5_provisioner"] = provisioner
    coordinator = ConnectCoordinator(app, config, registry, provisioner)
    app.extensions["mt5_coordinator"] = coordinator
    try:  # pemulihan job yang terputus (restart) & melewati deadline
        with app.app_context():
            n = coordinator.recover_expired(dbmod.get_db(config))
            if n:
                logging.getLogger(__name__).warning(
                    "%d job provisioning terputus dipulihkan", n)
            # F2: terminal yang tertahan karena timeout/sibuk helper
            m = coordinator.retry_uncertain_removals(dbmod.get_db(config))
            if m:
                logging.getLogger(__name__).warning(
                    "%d terminal tertahan (timeout helper) dibersihkan", m)
    except Exception:  # noqa: BLE001 - startup tidak boleh gagal karenanya
        logging.getLogger(__name__).exception("pemulihan job gagal")

    sync_service = SyncService(config, factory, registry=registry)
    app.extensions["mt5_sync"] = sync_service

    if provider_factory is not None:
        # konteks test/legacy: scheduler lama satu-akun (perilaku sama)
        scheduler = SyncScheduler(config, sync_service)
    else:
        scheduler = MultiAccountScheduler(config, sync_service, registry)
    app.extensions["mt5_scheduler"] = scheduler

    @app.teardown_appcontext
    def _release_customer_scope(_exc):
        """Audit round-3: bersihkan scope RLS tiap selesai request.

        Koneksi per-thread DIPERTAHANKAN antar request (perf), tetapi
        scope customer WAJIB lepas di akhir setiap request — kalau
        tidak, request berikutnya (unauth: health/register/login/401)
        mewarisi GUC customer sebelumnya saat transaksi berikutnya di-
        commit (lihat PostgresConn.clear_customer_context dan test
        test_rls_no_leak_requests).

        Aman terhadap transaksi yang sudah berhasil: endpoint sukses
        sudah commit sebelum teardown; cleanup hanya menutup transaksi
        tersisa (rollback no-op pada koneksi idle) + reset GUC.
        Kegagalan cleanup tidak pernah mengubah status response.
        """
        try:
            dbmod.clear_customer_context(dbmod.get_db(config))
        except Exception:  # noqa: BLE001 - teardown tak boleh meledak
            logging.getLogger(__name__).debug(
                "cleanup scope RLS gagal (koneksi mungkin sudah tertutup)",
                exc_info=True)
        # B12.1: koneksi ROLE SISTEM thread ini (bila dipakai request) juga
        # dikembalikan ke keadaan bersih: tanpa transaksi, tanpa customer
        # context — tidak ada scope tertinggal untuk request berikutnya.
        try:
            dbmod.clear_system_context(config)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "cleanup koneksi sistem gagal — koneksi dibuang",
                exc_info=True)
            try:
                dbmod._drop_system_slot(dbmod._local.sysconn)
            except Exception:  # noqa: BLE001
                pass
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
