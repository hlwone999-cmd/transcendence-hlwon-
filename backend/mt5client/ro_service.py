"""Layanan RPC MT5 READ-ONLY terbatas — pengganti RPyC *classic*.

MASALAH (Tahap 4, lihat docs/RPYC-SECURITY.md): mt5server.exe /
`python -m mt5linux` adalah server RPyC CLASSIC (SlaveService). Setiap
proses lokal yang dapat membuka 127.0.0.1:<port> mendapat EKSEKUSI KODE
ARBITRER di dalam Wine (conn.modules, conn.eval, conn.execute) —
termasuk fungsi pengiriman order MT5. Pembatasan READ-ONLY di MT5LinuxProvider hanya
berlaku di sisi klien.

LAYANAN INI (dijalankan di Python Windows di dalam Wine, berdampingan
dengan terminal64.exe):
- rpyc.Service biasa (BUKAN classic): hanya metode `exposed_*` di
  whitelist READ_ONLY_METHODS yang dapat dipanggil; tidak ada modules /
  eval / execute / getattr bebas; fungsi eksekusi order TIDAK ada.
- Autentikasi token per terminal (dibaca dari berkas 0600) SEBELUM
  handshake RPyC; perbandingan konstanta-waktu.
- Bind HANYA 127.0.0.1 (dipaksa, bukan argumen).
- Argumen divalidasi bertipe primitif (int/float/str); hasil dikirim
  sebagai JSON string — tidak ada netref/objek yang bocor ke klien.
- Konfigurasi protokol: allow_public_attrs/all_attrs/setattr/delattr/
  pickle = False, tanpa instansiasi exception kustom.

Jalankan:  python ro_service.py --port 18812 --token-file C:\\...\\rpc.token
"""
from __future__ import annotations

import argparse
import hmac
import json
import socket
from datetime import datetime, timezone

READ_ONLY_METHODS = (
    "initialize", "shutdown", "last_error", "account_info", "terminal_info",
    "symbol_info", "positions_get", "history_deals_get",
    "history_orders_get",
)

SAFE_PROTOCOL_CONFIG = {
    "allow_public_attrs": False,
    "allow_all_attrs": False,
    "allow_setattr": False,
    "allow_delattr": False,
    "allow_pickle": False,
    "import_custom_exceptions": False,
    "instantiate_custom_exceptions": False,
    "instantiate_oldstyle_exceptions": False,
    "propagate_SystemExit_locally": False,
    "include_local_traceback": False,
    "sync_request_timeout": 60,
}

_TOKEN_MAX = 256
BIND_HOST = "127.0.0.1"


def _plain(obj):
    asdict = getattr(obj, "_asdict", None)
    if callable(asdict):
        return {str(k): _plain(v) for k, v in asdict().items()}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


def _int(v, name):
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"{name} harus int")
    return v


def _str(v, name, maxlen=128):
    if not isinstance(v, str) or len(v) > maxlen:
        raise TypeError(f"{name} harus str <= {maxlen}")
    return v


def _epoch(v, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"{name} harus epoch (int/float)")
    return datetime.fromtimestamp(float(v), tz=timezone.utc)


def make_service(mt5):
    """Bangun kelas Service yang membungkus modul MetaTrader5 [mt5]."""
    import functools
    import threading

    import rpyc

    # Modul MetaTrader5 TIDAK thread-safe; ThreadedServer melayani koneksi
    # paralel -> semua pemanggilan MT5 diserialisasi satu lock global.
    mt5_lock = threading.Lock()

    def _serial(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with mt5_lock:
                return fn(*a, **kw)
        return wrapper

    class MT5ReadOnlyService(rpyc.Service):
        # ---- lifecycle sesi terminal (login dgn password investor)
        @_serial
        def exposed_initialize(self, login, password, server):
            return bool(mt5.initialize(login=_int(login, "login"),
                                       password=_str(password, "password",
                                                     256),
                                       server=_str(server, "server")))

        @_serial
        def exposed_shutdown(self):
            mt5.shutdown()
            return True

        @_serial
        def exposed_last_error(self):
            return json.dumps(_plain(mt5.last_error()))

        # ---- baca saja
        @_serial
        def exposed_account_info(self):
            return json.dumps(_plain(mt5.account_info()))

        @_serial
        def exposed_terminal_info(self):
            return json.dumps(_plain(mt5.terminal_info()))

        @_serial
        def exposed_symbol_info(self, symbol):
            return json.dumps(_plain(mt5.symbol_info(_str(symbol, "symbol",
                                                         32))))

        @_serial
        def exposed_positions_get(self, symbol=None):
            if symbol is None:
                return json.dumps(_plain(mt5.positions_get()))
            return json.dumps(_plain(mt5.positions_get(
                symbol=_str(symbol, "symbol", 32))))

        @_serial
        def exposed_history_deals_get(self, date_from, date_to):
            return json.dumps(_plain(mt5.history_deals_get(
                _epoch(date_from, "date_from"), _epoch(date_to, "date_to"))))

        @_serial
        def exposed_history_orders_get(self, date_from, date_to):
            return json.dumps(_plain(mt5.history_orders_get(
                _epoch(date_from, "date_from"), _epoch(date_to, "date_to"))))

    # metode bawaan rpyc.Service (get_service_name/aliases: metadata saja)
    base = {n for n in dir(rpyc.Service) if n.startswith("exposed_")}
    exposed = {n[len("exposed_"):] for n in dir(MT5ReadOnlyService)
               if n.startswith("exposed_") and n not in base}
    assert exposed == set(READ_ONLY_METHODS), exposed   # whitelist pasti
    return MT5ReadOnlyService


class TokenAuthenticator:
    """Klien WAJIB mengirim '<token>\\n' sebelum handshake RPyC."""

    def __init__(self, token: str):
        if not token or len(token) < 32:
            raise ValueError("token layanan minimal 32 karakter")
        self._token = token.encode("utf-8")

    def __call__(self, sock):
        from rpyc.utils.authenticators import AuthenticationError
        sock.settimeout(5)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(1)
            if not chunk or len(buf) > _TOKEN_MAX:
                raise AuthenticationError("token tidak valid")
            buf += chunk
        sock.settimeout(None)
        if not hmac.compare_digest(buf[:-1], self._token):
            raise AuthenticationError("token tidak valid")
        return sock, None


def build_server(mt5, port: int, token: str):
    from rpyc.utils.server import ThreadedServer
    return ThreadedServer(make_service(mt5), hostname=BIND_HOST,
                          port=int(port),
                          authenticator=TokenAuthenticator(token),
                          protocol_config=SAFE_PROTOCOL_CONFIG,
                          listener_timeout=None)


def main() -> None:  # pragma: no cover - dijalankan di dalam Wine
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token-file", required=True)
    a = ap.parse_args()
    with open(a.token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    import MetaTrader5 as mt5  # type: ignore
    build_server(mt5, a.port, token).start()


def client_connect(port: int, token: str, timeout: float = 10.0):
    """Klien: kirim token lalu handshake RPyC (VoidService, tanpa izin)."""
    import rpyc
    from rpyc.core.stream import SocketStream
    sock = socket.create_connection((BIND_HOST, int(port)), timeout=timeout)
    sock.sendall(token.encode("utf-8") + b"\n")
    sock.settimeout(None)
    return rpyc.connect_stream(SocketStream(sock), service=rpyc.VoidService,
                               config=dict(SAFE_PROTOCOL_CONFIG))


if __name__ == "__main__":  # pragma: no cover
    main()
