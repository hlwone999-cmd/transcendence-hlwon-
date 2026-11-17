"""Provider MT5 READ-ONLY.

Aturan mutlak:
1. Hanya method baca yang diekspos (whitelist).
2. Pemanggilan method lain (order_send, order_check, eval, execute, ...)
   melewati __getattr__ -> raise ReadOnlyViolationError.
3. Tidak ada state order; tidak ada penulisan ke terminal.

Implementasi nyata: MT5LinuxProvider (mt5client/mt5linux_provider.py).
Untuk test: MockMT5Client (mt5client/mock.py) — schema field sama dengan
namedtuple resmi package MetaTrader5.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone


class MT5Error(Exception):
    """Kesalahan umum komunikasi/MT5."""


class MT5ConnectionError(MT5Error):
    """mt5server/terminal tidak terjangkau atau initialize gagal."""


class ReadOnlyViolationError(MT5Error):
    """Dicoba akses method tulis/eksekusi — diblokir by design."""


READ_METHODS = (
    "connect",
    "disconnect",
    "is_connected",
    "account_info",
    "terminal_info",
    "symbol_info",
    "positions_get",
    "history_deals_get",
    "history_orders_get",
)

# Nama yang DILARANG keras muncul sebagai method provider.
FORBIDDEN = (
    "order_send",
    "order_check",
    "order_calc_margin",
    "order_calc_profit",
    "eval",
    "execute",
    "positions_modify",
    "positions_close",
    "positions_close_by",
    "order_modify",
    "order_delete",
    "login",  # login terminal ditangani di dalam connect(), bukan API publik
)


# Marker teks penolakan kredensial oleh BROKER (bukan masalah jaringan).
# Hanya frasa spesifik (remediasi R2): kata generik seperti "password" /
# "denied" TIDAK dipakai agar error lain (mis. "Permission denied" OS)
# tidak salah diklasifikasi sebagai kredensial ditolak.
BROKER_AUTH_MARKERS = (
    "invalid login", "invalid password", "login or password",
    "invalid account", "authorization failed", "auth failed",
    "otorisasi gagal",
)


def looks_like_broker_auth_error(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in BROKER_AUTH_MARKERS)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plain(obj):
    """Ubah namedtuple/netref RPyC -> struktur Python murni (JSON-safe).

    CATATAN: cek _asdict HARUS sebelum isinstance(tuple) — namedtuple
    adalah tuple, tapi harus jadi dict, bukan list.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    asdict = getattr(obj, "_asdict", None)
    if callable(asdict):
        return {str(k): plain(v) for k, v in asdict().items()}
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [plain(x) for x in obj]
    # netref rpyc / objek asing -> coba konversi dasar, kalau gagal stringkan
    try:
        return float(obj)  # pragma: no cover
    except Exception:
        return str(obj)  # pragma: no cover


class MT5ProviderBase(ABC):
    """Kontrak provider baca-saja."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def account_info(self): ...

    @abstractmethod
    def terminal_info(self): ...

    @abstractmethod
    def symbol_info(self, symbol: str): ...

    @abstractmethod
    def positions_get(self, symbol: str | None = None): ...

    @abstractmethod
    def history_deals_get(self, date_from: datetime, date_to: datetime): ...

    @abstractmethod
    def history_orders_get(self, date_from: datetime, date_to: datetime): ...

    # ---- perisai read-only -------------------------------------------
    def __getattr__(self, name: str):
        # Hanya atribut yang TIDAK ada di class yang jatuh ke sini.
        raise ReadOnlyViolationError(
            f"Method '{name}' diblokir: provider MT5 ini READ-ONLY. "
            f"Method baca yang diizinkan: {', '.join(READ_METHODS)}"
        )
