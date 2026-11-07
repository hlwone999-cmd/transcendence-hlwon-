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
    # PostgreSQL produksi (brief multi-customer §2/§5). Jika diisi
    # (postgres://...) engine PG dipakai; selain itu SQLite (dev/test).
    db_url: str = ""

    # --- auth multi-customer (brief §7) ---
    auth_token_ttl_hours: int = 168        # 7 hari
    max_failed_logins: int = 5             # lockout per email
    lockout_seconds: int = 900
    max_customers: int = 4                 # kapasitas MVP (brief §12)

    # --- self-link akun MT5 (redesign UI Jurnal MT5, keputusan D2) ---
    # Customer boleh self-link mandiri via POST /api/v1/mt5/connect
    # (identitas = nomor akun MT5 + server + password investor).
    # Admin dapat menonaktifkan lewat env MT5_SELF_LINK_ENABLED=false —
    # saat itu provisioning customer hanya via /auth/register (admin/API).
    # Kapasitas tetap dibatasi max_customers (default 4 customer aktif).
    mt5_self_link_enabled: bool = True

    # --- enkripsi kredensial MT5 (brief §5/§7) ---
    # Kunci Fernet. TIDAK boleh di repo/db; WAJIB env di produksi.
    cred_key: str = ""
    cred_key_old: str = ""                 # opsional, utk rotasi kunci

    # --- terminal MT5 per customer (brief §4) ---
    rpyc_port_base: int = 18812            # C001=18812 ... C004=18815
    term_base_dir: str = "/var/lib/transcendence/terminals"
    # "mock" = provisioning tanpa Wine/MT5 (dev/test); "real" = VPS.
    terminal_provision_mode: str = "mock"

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
        db_url=os.getenv("DATABASE_URL", os.getenv("JOURNAL_DB_URL", "")).strip(),
        auth_token_ttl_hours=max(1, int(os.getenv("AUTH_TOKEN_TTL_HOURS", "168"))),
        max_failed_logins=max(1, int(os.getenv("AUTH_MAX_FAILED_LOGINS", "5"))),
        lockout_seconds=max(30, int(os.getenv("AUTH_LOCKOUT_SECONDS", "900"))),
        max_customers=max(1, int(os.getenv("MAX_CUSTOMERS", "4"))),
        mt5_self_link_enabled=_bool(os.getenv("MT5_SELF_LINK_ENABLED", "1")),
        cred_key=os.getenv("MT5_CRED_KEY", "").strip(),
        cred_key_old=os.getenv("MT5_CRED_KEY_OLD", "").strip(),
        rpyc_port_base=max(1024, int(os.getenv("RPYC_PORT_BASE", "18812"))),
        term_base_dir=os.getenv("TERM_BASE_DIR",
                                "/var/lib/transcendence/terminals"),
        terminal_provision_mode=os.getenv("TERMINAL_PROVISION_MODE",
                                          "mock").strip().lower(),
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
            "API_TOKEN kosong: token bootstrap admin NONAKTIF. "
            "Endpoint /admin/* tidak dapat dipakai (opsi: buat customer "
            "pertama lewat /api/v1/auth/register)."
        )
    if cfg.db_url and not cfg.cred_key:
        log.warning(
            "MT5_CRED_KEY kosong: bind akun MT5 akan DITOLAK sampai kunci "
            "enkripsi kredensial diisi (Fernet key 32-byte url-safe base64)."
        )
    return cfg
