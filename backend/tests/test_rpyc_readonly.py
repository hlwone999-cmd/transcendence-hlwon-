"""Tahap 4 — layanan RPC MT5 READ-ONLY (pengganti RPyC classic).

Server sungguhan (rpyc ThreadedServer) di 127.0.0.1 dengan modul
MetaTrader5 PALSU yang mencatat setiap pemanggilan — termasuk
order_send. Dibuktikan: tanpa token ditolak; hanya metode whitelist;
tidak ada eksekusi kode / akses atribut bebas; order_send tidak pernah
dapat dipanggil; argumen non-primitif ditolak; provider klien bekerja.
"""
import hashlib
import hmac
import json
import socket
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

rpyc = pytest.importorskip("rpyc", reason="rpyc belum terpasang")

from mt5client import ro_service as RS  # noqa: E402
from mt5client.provider import MT5ConnectionError  # noqa: E402

TOKEN = "t" * 48
Acc = namedtuple("Acc", "login server trade_allowed trade_mode currency")


class FakeMT5:
    def __init__(self):
        self.calls = []
        self.ok_password = "investor-pw"

    def initialize(self, login=None, password=None, server=None):
        self.calls.append("initialize")
        return password == self.ok_password

    def shutdown(self):
        self.calls.append("shutdown")

    def last_error(self):
        return (-6, "Terminal: Authorization failed")

    def account_info(self):
        self.calls.append("account_info")
        return Acc(123, "Exness-MT5Real", False, 2, "USD")

    def terminal_info(self):
        return {"connected": True}

    def symbol_info(self, s):
        return {"name": s}

    def positions_get(self, symbol=None):
        return ()

    def history_deals_get(self, a, b):
        self.calls.append(("deals", a.year, b.year))
        return ()

    def history_orders_get(self, a, b):
        return ()

    def order_send(self, *a, **k):            # TIDAK BOLEH terjangkau
        self.calls.append("ORDER_SEND")
        raise AssertionError("order_send terpanggil!")

    def order_check(self, *a, **k):
        self.calls.append("ORDER_CHECK")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def server():
    mt5 = FakeMT5()
    port = _free_port()
    srv = RS.build_server(mt5, port, TOKEN)
    th = threading.Thread(target=srv.start, daemon=True)
    th.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield mt5, port, srv
    srv.close()


def test_bind_hanya_localhost(server):
    _, _, srv = server
    assert srv.host in ("127.0.0.1",)


def test_tanpa_token_atau_token_salah_ditolak(server):
    mt5, port, _ = server
    with pytest.raises(Exception):
        c = RS.client_connect(port, "salah" * 10, timeout=2)
        c.root.account_info()
    with pytest.raises(Exception):
        c = rpyc.connect("127.0.0.1", port,
                         config={"sync_request_timeout": 2})
        c.root.account_info()
    assert "account_info" not in mt5.calls


def test_hanya_metode_whitelist_dan_tanpa_eksekusi_kode(server):
    mt5, port, _ = server
    c = RS.client_connect(port, TOKEN)
    assert json.loads(c.root.account_info())["trade_allowed"] is False
    for name in ("order_send", "order_check", "exposed_order_send",
                 "_mt5", "__class__", "__dict__", "_conn"):
        with pytest.raises(Exception):
            getattr(c.root, name)(1)
    # kapabilitas RPyC classic TIDAK tersedia
    for attr in ("modules", "eval", "execute", "builtins"):
        assert not hasattr(c, attr), attr
    with pytest.raises(Exception):
        c.root.getmodule("os")
    with pytest.raises(Exception):
        rpyc.classic.obtain(c.root)
    assert "ORDER_SEND" not in mt5.calls and "ORDER_CHECK" not in mt5.calls
    c.close()


def test_whitelist_metode_terkunci():
    svc = RS.make_service(FakeMT5())
    base = {n for n in dir(rpyc.Service) if n.startswith("exposed_")}
    assert base <= {"exposed_get_service_name", "exposed_get_service_aliases"}
    exposed = sorted(n[len("exposed_"):] for n in dir(svc)
                     if n.startswith("exposed_") and n not in base)
    assert exposed == sorted(RS.READ_ONLY_METHODS)
    assert not any("order" in n for n in exposed if n != "history_orders_get")


def test_argumen_non_primitif_ditolak(server):
    mt5, port, _ = server
    c = RS.client_connect(port, TOKEN)
    with pytest.raises(Exception):
        c.root.history_deals_get([1], [2])
    with pytest.raises(Exception):
        c.root.initialize("123", "x", "srv")
    c.root.history_deals_get(0, time.time())
    assert ("deals", 1970, datetime.now(timezone.utc).year) in mt5.calls
    c.close()


def test_provider_klien_end_to_end(server):
    from mt5client.ro_provider import (MT5ReadOnlyServiceProvider,
                                       derive_token)
    mt5, port, _ = server

    class Cfg:
        mt5_port = port
        mt5_token = TOKEN
        mt5_login = "123"
        mt5_password = "investor-pw"
        mt5_server = "Exness-MT5Real"

    p = MT5ReadOnlyServiceProvider(Cfg())
    p.connect()
    info = p.account_info()
    assert info["login"] == 123 and info["trade_allowed"] is False
    now = datetime.now(timezone.utc)
    assert p.history_deals_get(now - timedelta(days=1), now) == []
    p.disconnect()
    Cfg.mt5_password = "salah"
    with pytest.raises(MT5ConnectionError) as ei:
        MT5ReadOnlyServiceProvider(Cfg()).connect()
    assert "Authorization failed" in str(ei.value)
    # token deterministik = HMAC(secret, code:port:provision_id)
    sec = "s" * 40
    assert derive_token(sec, "C001", 18812, "a" * 32) == hmac.new(
        sec.encode(), b"C001:18812:" + b"a" * 32, hashlib.sha256).hexdigest()
    with pytest.raises(MT5ConnectionError):
        derive_token("pendek", "C001", 18812, None)
    assert "ORDER_SEND" not in mt5.calls


def test_token_minimal_32_karakter():
    with pytest.raises(ValueError):
        RS.TokenAuthenticator("pendek")


def test_probe_ro_session_butuh_token(server):
    from services.terminals import probe_ro_session, probe_rpyc_session
    _, port, _ = server
    assert probe_ro_session(port, TOKEN) is True
    assert probe_ro_session(port, "x" * 48) is False
    # probe classic (tanpa token) TIDAK boleh dianggap sehat di mode ro
    assert probe_rpyc_session(port, timeout=2) is False


def test_pemanggilan_mt5_diserialisasi_lintas_koneksi():
    class SlowMT5(FakeMT5):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.overlap = False
            self.lock = threading.Lock()

        def account_info(self):
            with self.lock:
                self.active += 1
                if self.active > 1:
                    self.overlap = True
            time.sleep(0.2)
            with self.lock:
                self.active -= 1
            return Acc(1, "S", False, 2, "USD")

    mt5 = SlowMT5()
    port = _free_port()
    srv = RS.build_server(mt5, port, TOKEN)
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(0.3)
    try:
        def call():
            c = RS.client_connect(port, TOKEN)
            c.root.account_info()
            c.close()
        ts = [threading.Thread(target=call) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert mt5.overlap is False
    finally:
        srv.close()


def test_klien_ro_tidak_bisa_dikendalikan_server_terkompromi():
    """Server read-only yang DIGANTI penyerang (terminal terkompromi)
    mencoba memanggil balik klien API — klien VoidService menolak."""
    result = {}

    class EvilService(rpyc.Service):
        def on_connect(self, conn):
            self._c = conn

        def exposed_account_info(self):
            try:
                result["pid"] = self._c.root.getmodule("os").getpid()
            except Exception as e:  # noqa: BLE001
                result["blocked"] = type(e).__name__
            return "{}"

    from rpyc.utils.server import ThreadedServer
    port = _free_port()
    srv = ThreadedServer(EvilService, hostname="127.0.0.1", port=port,
                         authenticator=RS.TokenAuthenticator(TOKEN))
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(0.3)
    try:
        c = RS.client_connect(port, TOKEN)
        c.root.account_info()
        c.close()
        assert "pid" not in result and result.get("blocked"), result
    finally:
        srv.close()
