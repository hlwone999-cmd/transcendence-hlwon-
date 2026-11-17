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
    mt5_password: str = field(default="", repr=False)  # SECRET
    mt5_server: str = ""

    # --- sinkronisasi ---
    mt5_sync_interval_seconds: int = 300
    mt5_history_initial_days: int = 90
    mt5_sync_overlap_seconds: int = 86400

    # --- API ---
    api_token: str = field(default="", repr=False)    # SECRET
    api_bind: str = "127.0.0.1"
    api_port: int = 8000

    # --- database ---
    db_path: str = str(BASE_DIR / "data" / "journal.db")
    # PostgreSQL produksi (brief multi-customer §2/§5). Jika diisi
    # (postgres://...) engine PG dipakai; selain itu SQLite (dev/test).
    db_url: str = field(default="", repr=False)  # DSN bisa berisi password
    # B12.1: DSN role SISTEM PostgreSQL (transcendence_system, migrasi 008)
    db_system_url: str = field(default="", repr=False)

    # --- auth multi-customer (brief §7) ---
    auth_token_ttl_hours: int = 168        # 7 hari
    max_failed_logins: int = 5             # lockout per email
    lockout_seconds: int = 900
    # SLOT MT5 (terminal) maksimum — kapasitas MVP (brief §12). Sejak
    # keputusan produk 2026-11 ini membatasi TERMINAL/akun MT5 aktif,
    # bukan jumlah akun portal (lihat max_registered_customers).
    max_customers: int = 4

    # --- registrasi portal publik (keputusan produk #1: TETAP AKTIF) ---
    # Registrasi TIDAK memakai slot MT5 (slot = max_customers terminal,
    # dialokasikan hanya saat bind/connect terverifikasi). Perlindungan:
    # validasi input, rate limit per IP klien tepercaya (per jam & per
    # hari) dan per subnet (/24 IPv4, /64 IPv6) — persisten di DB.
    # max_registered_customers: batas total akun PORTAL (0 = tanpa batas).
    # Rekomendasi produksi 200 (default): 4 slot MT5 -> akun portal di atas
    # itu hanya daftar tunggu; batas mencegah pertumbuhan DB tak terbatas
    # oleh registrasi massal multi-subnet. Tidak berlaku utk self-link MT5.
    public_register_enabled: bool = True
    max_registered_customers: int = 200
    register_per_ip_hour: int = 5
    register_per_ip_day: int = 10
    register_per_subnet_day: int = 30

    # --- IP klien di balik reverse proxy (remediasi T2) ---
    # Header IP klien HANYA dipercaya bila koneksi TCP datang dari proxy
    # tepercaya (REMOTE_ADDR ada di trusted_proxies). Default cocok dgn
    # nginx terdokumentasi (proxy_set_header X-Real-IP $remote_addr):
    # nginx MENIMPA X-Real-IP, sehingga klien tidak bisa memalsukannya.
    # X-Forwarded-For dari klien TIDAK pernah dipercaya; bila header yang
    # dipilih adalah X-Forwarded-For, hanya entri PALING KANAN (yang
    # ditambahkan proxy tepercaya) yang dipakai.
    trusted_proxies: tuple = ("127.0.0.1", "::1")
    client_ip_header: str = "X-Real-IP"

    # --- investor-only (read-only) ditegakkan di jalur sync (remediasi T5)
    # Sesi investor MT5 selalu account_info().trade_allowed == False.
    # Sync per-akun MENOLAK sesi trade-capable (password MASTER) atau
    # account_info yang tidak memuat trade_allowed (fail-closed).
    # (field: mt5_enforce_investor_readonly — didefinisikan di bawah,
    # dipakai bersama verifikasi live /mt5/connect.)

    # --- self-link akun MT5 (redesign UI Jurnal MT5, keputusan D2) ---
    # Customer boleh self-link mandiri via POST /api/v1/mt5/connect
    # (identitas = nomor akun MT5 + server + password investor).
    # Admin dapat menonaktifkan lewat env MT5_SELF_LINK_ENABLED=false —
    # saat itu provisioning customer hanya via /auth/register (admin/API).
    # Kapasitas tetap dibatasi max_customers (default 4 customer aktif).
    mt5_self_link_enabled: bool = True
    # Remediasi T3 (penyalahgunaan kapasitas): akun demo gratis dapat
    # menduduki slot secara permanen. Self-link akun BARU hanya untuk
    # akun REAL (account_info().trade_mode == ACCOUNT_TRADE_MODE_REAL),
    # fail-closed bila trade_mode tidak tersedia. Opsional: allowlist
    # nama server (pola fnmatch, case-insensitive), mis.
    # MT5_SELF_LINK_SERVER_ALLOWLIST="Exness-MT5Real*".
    mt5_self_link_require_real: bool = True
    mt5_self_link_server_allowlist: tuple = ()

    # --- verifikasi live kredensial (hardening post-redesign) ----------
    # POST /mt5/connect TIDAK menyatakan akun TERHUBUNG sebelum kredensial
    # benar-benar terverifikasi terminal MT5 (login nyata ke broker via
    # provider read-only). mt5_verify_timeout_seconds = batas waktu total
    # verifikasi (retry sampai deadline; terminal butuh waktu start di
    # mode "real"). WAJIB lebih kecil dari timeout HTTP klien Flutter (25s).
    mt5_verify_timeout_seconds: float = 12.0
    # --- auto-provisioning terminal (keputusan produk #3, Tahap 3) ---
    # Batas waktu TOTAL job aktivasi (install+start+health+verifikasi).
    mt5_provision_timeout_seconds: float = 120.0
    # Request /mt5/connect menunggu job selesai maks N detik; bila belum,
    # respons 202 + job_id/job_secret untuk polling /mt5/connect/status.
    # WAJIB < timeout HTTP klien Flutter (25 s).
    mt5_connect_sync_wait_seconds: float = 10.0
    # Helper privileged (mode real) — dipanggil `sudo -n <helper> ...`.
    provisioner_helper: str = "/usr/local/sbin/transcendence-mt5-provision"
    provisioner_sudo: str = "/usr/bin/sudo"
    provisioner_timeout_seconds: float = 60.0
    # F2: setelah timeout, SIGTERM ke sudo (diteruskan ke helper) lalu tunggu
    # grace ini; keadaan nyata diperiksa dengan `helper inspect`.
    provisioner_term_grace_seconds: float = 10.0
    provisioner_inspect_timeout_seconds: float = 15.0
    # Tahap 4: secret HMAC token layanan RPC read-only (MT5_PROVIDER=mt5ro).
    # SECRET — hanya di .env (0600) & konfigurasi helper root-only.
    mt5_rpc_secret: str = field(default="", repr=False)
    # Mode RPC terminal BARU: 'classic' (mt5server RPyC, kompatibilitas)
    # atau 'ro' (layanan read-only bertoken). Terminal lama tidak berubah.
    mt5_rpc_mode_default: str = "classic"
    # Deteksi password MASTER: MetaTrader5 menerima password master ATAU
    # investor di initialize() tanpa parameter pembeda. Sesi investor
    # read-only selalu melaporkan account_info().trade_allowed == False;
    # sesi password master = True. Bila True (default), kredensial yang
    # menghasilkan sesi trade-capable DITOLAK dan tidak pernah disimpan.
    mt5_enforce_investor_readonly: bool = True

    # --- enkripsi kredensial MT5 (brief §5/§7) ---
    # Kunci Fernet. TIDAK boleh di repo/db; WAJIB env di produksi.
    cred_key: str = field(default="", repr=False)     # SECRET
    cred_key_old: str = field(default="", repr=False)  # opsional, rotasi kunci

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
        db_system_url=os.getenv("DATABASE_SYSTEM_URL", "").strip(),
        auth_token_ttl_hours=max(1, int(os.getenv("AUTH_TOKEN_TTL_HOURS", "168"))),
        max_failed_logins=max(1, int(os.getenv("AUTH_MAX_FAILED_LOGINS", "5"))),
        lockout_seconds=max(30, int(os.getenv("AUTH_LOCKOUT_SECONDS", "900"))),
        max_customers=max(1, int(os.getenv("MAX_CUSTOMERS", "4"))),
        public_register_enabled=_bool(
            os.getenv("AUTH_PUBLIC_REGISTER_ENABLED", "1")),
        max_registered_customers=max(
            0, int(os.getenv("MAX_REGISTERED_CUSTOMERS", "200"))),
        register_per_ip_hour=max(1, int(os.getenv("REGISTER_PER_IP_HOUR",
                                                  "5"))),
        register_per_ip_day=max(1, int(os.getenv("REGISTER_PER_IP_DAY",
                                                 "10"))),
        register_per_subnet_day=max(1, int(os.getenv(
            "REGISTER_PER_SUBNET_DAY", "30"))),
        trusted_proxies=tuple(
            p.strip() for p in os.getenv(
                "TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if p.strip()),
        client_ip_header=os.getenv("CLIENT_IP_HEADER", "X-Real-IP").strip(),
        mt5_self_link_enabled=_bool(os.getenv("MT5_SELF_LINK_ENABLED", "1")),
        mt5_self_link_require_real=_bool(
            os.getenv("MT5_SELF_LINK_REQUIRE_REAL", "1")),
        mt5_self_link_server_allowlist=tuple(
            p.strip() for p in os.getenv(
                "MT5_SELF_LINK_SERVER_ALLOWLIST", "").split(",")
            if p.strip()),
        mt5_verify_timeout_seconds=max(
            1.0, float(os.getenv("MT5_VERIFY_TIMEOUT_SECONDS", "12"))),
        mt5_provision_timeout_seconds=max(
            10.0, float(os.getenv("MT5_PROVISION_TIMEOUT_SECONDS", "120"))),
        mt5_connect_sync_wait_seconds=min(20.0, max(
            0.0, float(os.getenv("MT5_CONNECT_SYNC_WAIT_SECONDS", "10")))),
        provisioner_helper=os.getenv(
            "PROVISIONER_HELPER",
            "/usr/local/sbin/transcendence-mt5-provision").strip(),
        provisioner_sudo=os.getenv("PROVISIONER_SUDO", "/usr/bin/sudo").strip(),
        provisioner_timeout_seconds=max(5.0, float(os.getenv(
            "PROVISIONER_TIMEOUT_SECONDS", "60"))),
        provisioner_term_grace_seconds=min(60.0, max(1.0, float(os.getenv(
            "PROVISIONER_TERM_GRACE_SECONDS", "10")))),
        provisioner_inspect_timeout_seconds=min(60.0, max(2.0, float(
            os.getenv("PROVISIONER_INSPECT_TIMEOUT_SECONDS", "15")))),
        mt5_rpc_secret=os.getenv("MT5_RPC_SECRET", ""),
        mt5_rpc_mode_default=(
            "ro" if os.getenv("MT5_RPC_MODE_DEFAULT", "classic").strip()
            .lower() == "ro" else "classic"),
        mt5_enforce_investor_readonly=_bool(
            os.getenv("MT5_ENFORCE_INVESTOR_READONLY", "1")),
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
            "Endpoint /admin/* (termasuk pencabutan sesi oleh admin) "
            "tidak dapat dipakai."
        )
    if cfg.db_url and not cfg.cred_key:
        log.warning(
            "MT5_CRED_KEY kosong: bind akun MT5 akan DITOLAK sampai kunci "
            "enkripsi kredensial diisi (Fernet key 32-byte url-safe base64)."
        )
    return cfg
