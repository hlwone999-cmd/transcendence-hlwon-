"""Konfigurasi backend Jurnal MT5 — dibaca dari environment / file .env.

Prinsip keamanan:
- Password MT5 hanya lewat environment variable. Tidak pernah di-log,
  tidak masuk database, tidak dikirim ke klien API.
- Default binding 127.0.0.1: backend tidak pernah terbuka ke internet
  tanpa reverse-proxy HTTPS yang dipasang admin.
"""
from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def _bool(v: str | None) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # --- provider ---
    mt5_provider: str = "mt5linux"
    mt5_enabled: bool = False
    mt5_host: str = "127.0.0.1"
    mt5_port: int = 18812
    mt5_login: str = ""       # kosong = pakai login yang sudah aktif di terminal
    mt5_password: str = ""    # SECRET — jangan log!
    mt5_server: str = ""

    # --- sinkronisasi ---
    mt5_sync_interval_seconds: int = 300
    mt5_history_initial_days: int = 90
    mt5_sync_overlap_seconds: int = 86400

    # --- API ---
    api_token: str = ""
    api_bind: str = "127.0.0.1"
    api_port: int = 8000

    # --- database ---
    db_path: str = str(BASE_DIR / "data" / "journal.db")

    # --- definisi pip per simbol (override), sisanya dari symbol_info ---
    symbol_config: dict = field(default_factory=dict)

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = BASE_DIR / p
        return p

    def pip_size_for(self, symbol: str, point: float | None, digits: int | None) -> float | None:
        """Definisi pip aplikasi.

        Prioritas: override SYMBOL_CONFIG -> aturan default (10 * point,
        benar untuk XAUUSD 0.01->0.1, forex 5 digit, JPY 3 digit).
        Tidak pernah mengasumsikan semua simbol sama.
        """
        cfg = self.symbol_config.get(symbol) or {}
        if isinstance(cfg, dict) and cfg.get("pip_size"):
            try:
                return float(cfg["pip_size"])
            except (TypeError, ValueError):
                pass
        if point and point > 0:
            return float(point) * 10.0
        return None


def load_config(env_file: str | None = None) -> Config:
    """Baca .env (jika ada) lalu environment, tanpa menimpa env nyata."""
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(BASE_DIR / ".env")
        if env_file:
            load_dotenv(env_file, override=True)
    except ImportError:  # pragma: no cover - dotenv opsional
        pass

    cfg = Config(
        mt5_provider=os.getenv("MT5_PROVIDER", "mt5linux").strip(),
        mt5_enabled=_bool(os.getenv("MT5_ENABLED")),
        mt5_host=os.getenv("MT5_HOST", "127.0.0.1").strip(),
        mt5_port=int(os.getenv("MT5_PORT", "18812")),
        mt5_login=os.getenv("MT5_LOGIN", "").strip(),
        mt5_password=os.getenv("MT5_PASSWORD", ""),  # sengaja tidak di-strip
        mt5_server=os.getenv("MT5_SERVER", "").strip(),
        mt5_sync_interval_seconds=max(30, int(os.getenv("MT5_SYNC_INTERVAL_SECONDS", "300"))),
        mt5_history_initial_days=max(1, int(os.getenv("MT5_HISTORY_INITIAL_DAYS", "90"))),
        mt5_sync_overlap_seconds=max(60, int(os.getenv("MT5_SYNC_OVERLAP_SECONDS", "86400"))),
        api_token=os.getenv("API_TOKEN", "").strip(),
        api_bind=os.getenv("API_BIND", "127.0.0.1").strip(),
        api_port=int(os.getenv("API_PORT", "8000")),
        db_path=os.getenv("JOURNAL_DB_PATH", "data/journal.db"),
    )

    raw = os.getenv("SYMBOL_CONFIG", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                cfg.symbol_config = parsed
        except json.JSONDecodeError:
            log.warning("SYMBOL_CONFIG bukan JSON valid — diabaikan")

    if not cfg.api_token:
        log.warning(
            "API_TOKEN kosong: autentikasi API NONAKTIF (mode dev). "
            "WAJIB diisi saat backend diakses selain localhost."
        )
    return cfg
