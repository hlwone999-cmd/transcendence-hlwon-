"""Provider nyata via mt5linux (RPyC ke mt5server.exe di Wine).

Import mt5linux bersifat LAZY — backend tetap start & API tetap hidup
(dengan status disconnected) walau paket/binary tidak terpasang.

Catatan keamanan:
- Koneksi RPyC hanya ke host lokal (default 127.0.0.1).
- Hanya method baca dari MT5ProviderBase yang dipanggil. order_send dkk
  tidak pernah dipakai di seluruh kode backend (dicek test otomatis).
"""
from __future__ import annotations

import logging
from datetime import datetime

from .provider import (
    MT5ConnectionError,
    MT5Error,
    MT5ProviderBase,
    plain,
)

log = logging.getLogger(__name__)


class MT5LinuxProvider(MT5ProviderBase):
    """Bungkus mt5linux.MetaTrader5 — whitelist baca saja."""

    def __init__(self, config):
        self._cfg = config
        self._mt5 = None          # instance mt5linux.MetaTrader5
        self._connected = False

    # ---- lifecycle ----------------------------------------------------
    def connect(self) -> None:
        if self._connected and self._mt5 is not None:
            return
        try:
            from mt5linux import MetaTrader5  # lazy: boleh gagal import
        except ImportError as e:  # pragma: no cover - lingkungan VPS
            raise MT5ConnectionError(
                "Paket 'mt5linux' tidak terpasang di backend "
                f"(pip install mt5linux): {e}"
            ) from e

        try:
            self._mt5 = MetaTrader5(host=self._cfg.mt5_host, port=self._cfg.mt5_port)
            kwargs = {}
            if self._cfg.mt5_login:
                kwargs["login"] = int(self._cfg.mt5_login)
            if self._cfg.mt5_password:
                kwargs["password"] = self._cfg.mt5_password  # tidak di-log
            if self._cfg.mt5_server:
                kwargs["server"] = self._cfg.mt5_server
            ok = bool(self._mt5.initialize(**kwargs))
            if not ok:
                err = ""
                try:
                    err = str(self._mt5.last_error())
                except Exception:  # pragma: no cover
                    pass
                raise MT5ConnectionError(f"MT5 initialize gagal ({err or 'tanpa pesan'})")
            self._connected = True
        except MT5ConnectionError:
            self._connected = False
            raise
        except Exception as e:
            self._connected = False
            raise MT5ConnectionError(
                f"Tidak bisa terhubung ke mt5server "
                f"{self._cfg.mt5_host}:{self._cfg.mt5_port}: {e}"
            ) from e

    def disconnect(self) -> None:
        try:
            if self._mt5 is not None:
                self._mt5.shutdown()
        except Exception:  # pragma: no cover
            pass
        self._mt5 = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._mt5 is not None

    # ---- panggilan baca ------------------------------------------------
    def _call(self, method: str, *args, **kwargs):
        if not self.is_connected():
            raise MT5ConnectionError("Provider belum connect()")
        try:
            return plain(getattr(self._mt5, method)(*args, **kwargs))
        except MT5Error:
            raise
        except Exception as e:
            raise MT5Error(f"Panggilan {method} gagal: {e}") from e

    def account_info(self):
        return self._call("account_info")

    def terminal_info(self):
        return self._call("terminal_info")

    def symbol_info(self, symbol: str):
        return self._call("symbol_info", symbol)

    def positions_get(self, symbol: str | None = None):
        if symbol:
            return self._call("positions_get", symbol=symbol)
        return self._call("positions_get")

    def history_deals_get(self, date_from: datetime, date_to: datetime):
        return self._call("history_deals_get", date_from, date_to)

    def history_orders_get(self, date_from: datetime, date_to: datetime):
        return self._call("history_orders_get", date_from, date_to)
