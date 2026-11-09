"""Provider klien untuk layanan RPC MT5 READ-ONLY (mt5client.ro_service).

Dipilih dengan MT5_PROVIDER=mt5ro. Tidak memakai RPyC classic: hanya
memanggil metode whitelist layanan, hasil berupa JSON (tanpa netref).
Token per terminal = HMAC-SHA256(MT5_RPC_SECRET, "code:port:provision_id")
— helper provisioning menulis token yang sama ke berkas 0600 milik user
layanan terminal; token TIDAK pernah lewat argumen proses / log.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from .provider import MT5ConnectionError, MT5Error, MT5ProviderBase


def derive_token(secret: str, code: str, port: int,
                 provision_id: str | None) -> str:
    if not secret or len(secret) < 32:
        raise MT5ConnectionError("MT5_RPC_SECRET belum dikonfigurasi "
                                 "(minimal 32 karakter)")
    msg = f"{code}:{int(port)}:{provision_id or ''}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class MT5ReadOnlyServiceProvider(MT5ProviderBase):
    def __init__(self, config):
        self._cfg = config
        self._conn = None
        self._connected = False

    def connect(self) -> None:
        if self._connected and self._conn is not None:
            return
        from .ro_service import client_connect
        try:
            self._conn = client_connect(self._cfg.mt5_port,
                                        self._cfg.mt5_token)
            ok = self._conn.root.initialize(int(self._cfg.mt5_login),
                                            str(self._cfg.mt5_password),
                                            str(self._cfg.mt5_server))
        except MT5Error:
            raise
        except Exception as e:  # noqa: BLE001
            self._close()
            raise MT5ConnectionError(
                f"Tidak bisa terhubung ke layanan MT5 read-only "
                f"127.0.0.1:{self._cfg.mt5_port}: {type(e).__name__}") from e
        if not ok:
            err = ""
            try:
                err = json.loads(self._conn.root.last_error())
            except Exception:  # noqa: BLE001
                pass
            self._close()
            raise MT5ConnectionError(f"MT5 initialize gagal ({err})")
        self._connected = True

    def _close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None
        self._connected = False

    def disconnect(self) -> None:
        try:
            if self._conn is not None:
                self._conn.root.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self._close()

    def is_connected(self) -> bool:
        return self._connected and self._conn is not None

    def _call(self, name: str, *args):
        if not self.is_connected():
            raise MT5ConnectionError("Provider belum connect()")
        try:
            return json.loads(getattr(self._conn.root, name)(*args))
        except Exception as e:  # noqa: BLE001
            raise MT5Error(f"Panggilan {name} gagal: {type(e).__name__}") \
                from e

    def account_info(self):
        return self._call("account_info")

    def terminal_info(self):
        return self._call("terminal_info")

    def symbol_info(self, symbol: str):
        return self._call("symbol_info", symbol)

    def positions_get(self, symbol: str | None = None):
        return self._call("positions_get", symbol) if symbol else \
            self._call("positions_get")

    def history_deals_get(self, date_from: datetime, date_to: datetime):
        return self._call("history_deals_get", date_from.timestamp(),
                          date_to.timestamp())

    def history_orders_get(self, date_from: datetime, date_to: datetime):
        return self._call("history_orders_get", date_from.timestamp(),
                          date_to.timestamp())
