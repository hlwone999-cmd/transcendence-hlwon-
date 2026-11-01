"""Entry point backend Jurnal MT5.

Menjalankan: migrasi DB -> Flask app -> scheduler sync -> waitress WSGI.
Tanpa terminal MT5, backend tetap hidup (status disconnected via API).

Jalankan:  python run.py          (atau via systemd, lihat
docs/MT5-UBUNTU-NO-DOCKER.md)
"""
from __future__ import annotations

from app import create_app
from config import load_config


def main() -> None:
    cfg = load_config()
    app = create_app(cfg)
    sched = app.extensions["mt5_scheduler"]
    sched.start()
    try:
        from waitress import serve
        print(f"* Jurnal MT5 backend berjalan di http://{cfg.api_bind}:{cfg.api_port}")
        serve(app, host=cfg.api_bind, port=cfg.api_port, threads=4)
    except ImportError:  # fallback dev
        app.run(host=cfg.api_bind, port=cfg.api_port, debug=False)
    finally:
        sched.stop()


if __name__ == "__main__":
    main()
