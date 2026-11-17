"""Provider ke server RPyC CLASSIC (mt5server.exe / `python -m mt5linux`
di Wine) — dengan klien yang TIDAK mengekspos apa pun ke server.

Production readiness (audit RPyC):
- `rpyc.classic.connect()` (dipakai paket mt5linux) membuat koneksi
  SIMETRIS: sisi klien juga menjalankan SlaveService, sehingga server
  terminal yang terkompromi dapat mengeksekusi kode DI DALAM PROSES API
  (memegang kunci Fernet & akses DB semua customer). Dibuktikan di
  tests/test_rpyc_classic_client.py.
- Provider ini memakai `rpyc.connect(service=ClassicClient)` —
  MasterService + FakeSlaveService bawaan rpyc: klien tetap dapat memakai
  server classic (kompatibel dengan terminal yang sudah ada), tetapi
  server TIDAK dapat memanggil balik klien; konfigurasi protokol klien
  menolak pickle/atribut publik.
- Pemanggilan MT5 dijalankan lewat helper sisi server ber-WHITELIST
  (hanya metode baca) yang mengembalikan JSON string — tidak ada netref
  maupun pickle yang dibuka di proses API.
- Koneksi HANYA ke loopback (fail-closed).

Server classic sendiri tetap mengizinkan eksekusi kode bagi siapa pun
yang dapat terhubung ke port-nya — lihat docs/RPYC-SECURITY.md untuk
firewall owner-match & migrasi ke layanan read-only bertoken.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from .provider import (
    MT5ConnectionError,
    MT5Error,
    MT5ProviderBase,
)

log = logging.getLogger(__name__)

READ_ONLY_CALLS = (
    "initialize", "shutdown", "last_error", "account_info", "terminal_info",
    "symbol_info", "positions_get", "history_deals_get", "history_orders_get",
)

# Konfigurasi protokol SISI KLIEN: server tidak boleh membaca atribut,
# mem-pickle, atau menginstansiasi exception kustom di proses API.
CLIENT_PROTOCOL_CONFIG = {
    "allow_public_attrs": False,
    "allow_all_attrs": False,
    "allow_setattr": False,
    "allow_delattr": False,
    "allow_pickle": False,
    "import_custom_exceptions": False,
    "instantiate_custom_exceptions": False,
    "instantiate_oldstyle_exceptions": False,
    "include_local_traceback": False,
    "sync_request_timeout": 60,
}

# Helper yang dipasang di NAMESPACE SERVER (proses terminal) — hanya
# metode baca; hasil dikonversi menjadi JSON di sisi server.
_SERVER_HELPER = '''
import json as _tr_json, datetime as _tr_dt
import MetaTrader5 as _tr_mt5
_TR_READ = %r
def _tr_plain(o):
    a = getattr(o, "_asdict", None)
    if callable(a):
        return {str(k): _tr_plain(v) for k, v in a().items()}
    if isinstance(o, dict):
        return {str(k): _tr_plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_tr_plain(x) for x in o]
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)
def _tr_call(name, payload):
    if name not in _TR_READ:
        raise ValueError("metode MT5 tidak diizinkan")
    p = _tr_json.loads(payload)
    args, kwargs = list(p.get("a", [])), dict(p.get("k", {}))
    if name.startswith("history_"):
        args = [_tr_dt.datetime.fromtimestamp(float(x), tz=_tr_dt.timezone.utc)
                for x in args]
    return _tr_json.dumps(_tr_plain(getattr(_tr_mt5, name)(*args, **kwargs)))
''' % (READ_ONLY_CALLS,)


def is_loopback_host(host) -> bool:
    import ipaddress
    h = str(host or "").strip().lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class MT5LinuxProvider(MT5ProviderBase):
    """Klien baca-saja ke server RPyC classic (kompatibel mt5server)."""

    def __init__(self, config):
        self._cfg = config
        self._conn = None
        self._remote_call = None
        self._connected = False

    # ---- lifecycle ----------------------------------------------------
    def connect(self) -> None:
        if self._connected and self._conn is not None:
            return
        if not is_loopback_host(self._cfg.mt5_host):
            raise MT5ConnectionError(
                "MT5_HOST wajib loopback (127.0.0.1/::1/localhost) — RPyC "
                "classic tidak boleh dipakai lewat jaringan")
        try:
            import rpyc  # lazy: backend tetap hidup tanpa paket ini
            from rpyc.core.service import ClassicClient
        except ImportError as e:  # pragma: no cover - lingkungan VPS
            raise MT5ConnectionError(
                f"Paket 'rpyc' tidak terpasang di backend: {e}") from e
        try:
            self._conn = rpyc.connect(
                self._cfg.mt5_host, int(self._cfg.mt5_port),
                service=ClassicClient, config=dict(CLIENT_PROTOCOL_CONFIG))
            self._conn.execute(_SERVER_HELPER)
            self._remote_call = self._conn.namespace["_tr_call"]
        except Exception as e:
            self._close()
            raise MT5ConnectionError(
                f"Tidak bisa terhubung ke mt5server "
                f"{self._cfg.mt5_host}:{self._cfg.mt5_port}: "
                f"{type(e).__name__}") from e
        kwargs = {}
        if self._cfg.mt5_login:
            kwargs["login"] = int(self._cfg.mt5_login)
        if self._cfg.mt5_password:
            kwargs["password"] = self._cfg.mt5_password  # tidak di-log
        if self._cfg.mt5_server:
            kwargs["server"] = self._cfg.mt5_server
        try:
            ok = bool(self._invoke("initialize", k=kwargs))
        except MT5Error as e:
            self._close()
            raise MT5ConnectionError(str(e)) from e
        if not ok:
            err = ""
            try:
                err = str(self._invoke("last_error"))
            except Exception:  # pragma: no cover
                pass
            self._close()
            raise MT5ConnectionError(
                f"MT5 initialize gagal ({err or 'tanpa pesan'})")
        self._connected = True

    def _close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # pragma: no cover
            pass
        self._conn = None
        self._remote_call = None
        self._connected = False

    def disconnect(self) -> None:
        try:
            if self._remote_call is not None:
                self._invoke("shutdown")
        except Exception:  # pragma: no cover
            pass
        self._close()

    def is_connected(self) -> bool:
        return self._connected and self._conn is not None

    # ---- panggilan baca ------------------------------------------------
    def _invoke(self, name: str, a=(), k=None):
        if name not in READ_ONLY_CALLS:
            raise MT5Error(f"metode {name} tidak diizinkan (read-only)")
        try:
            out = self._remote_call(name, json.dumps({"a": list(a),
                                                      "k": k or {}}))
            return json.loads(str(out))
        except MT5Error:
            raise
        except Exception as e:
            raise MT5Error(f"Panggilan {name} gagal: {type(e).__name__}") \
                from e

    def _call(self, method: str, *args, **kwargs):
        if not self.is_connected():
            raise MT5ConnectionError("Provider belum connect()")
        return self._invoke(method, a=args, k=kwargs)

    def account_info(self):
        return self._call("account_info")

    def terminal_info(self):
        return self._call("terminal_info")

    def symbol_info(self, symbol: str):
        return self._call("symbol_info", str(symbol))

    def positions_get(self, symbol: str | None = None):
        if symbol:
            return self._call("positions_get", symbol=str(symbol))
        return self._call("positions_get")

    def history_deals_get(self, date_from: datetime, date_to: datetime):
        return self._call("history_deals_get", date_from.timestamp(),
                          date_to.timestamp())

    def history_orders_get(self, date_from: datetime, date_to: datetime):
        return self._call("history_orders_get", date_from.timestamp(),
                          date_to.timestamp())
