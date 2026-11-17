"""Audit RPyC classic — klien API tidak boleh dapat dikendalikan server.

Server classic SUNGGUHAN (rpyc SlaveService, semantik `python -m mt5linux`
/ mt5server.exe) berjalan di PROSES TERPISAH dengan modul MetaTrader5
palsu yang — meniru terminal terkompromi — mencoba mengeksekusi kode
BALIK di proses klien (os.getpid()) saat account_info() dipanggil.

- Klien `rpyc.classic.connect` (pola paket mt5linux): serangan BERHASIL
  (bukti risiko eksekusi kode terminal -> proses API).
- MT5LinuxProvider (ClassicClient + helper whitelist + JSON): serangan
  GAGAL; data baca tetap diperoleh; metode non-baca ditolak.
"""
import json
import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

rpyc = pytest.importorskip("rpyc", reason="rpyc belum terpasang")

FAKE_MT5 = textwrap.dedent('''
    import builtins, json, os
    from collections import namedtuple
    A = namedtuple("A", "login server trade_allowed trade_mode")
    OUT = os.environ["TR_OUT"]

    def _attack():
        conn = getattr(builtins, "_tr_evil_conn", None)
        try:
            pid = conn.root.getmodule("os").getpid()  # kode di proses KLIEN
            res = {"attack": "OK", "pid": pid}
        except Exception as e:
            res = {"attack": "BLOCKED", "err": type(e).__name__}
        with open(OUT, "a") as f:
            f.write(json.dumps(res) + "\\n")

    def initialize(login=None, password=None, server=None):
        return password == "investor-ok"

    def shutdown():
        return True

    def last_error():
        return (-6, "Terminal: Authorization failed")

    def account_info():
        _attack()
        return A(7001, "Exness-MT5Real", False, 2)

    def history_deals_get(a, b):
        return ({"ticket": 1, "time": int(a.timestamp())},)

    def order_send(*a, **k):
        open(OUT + ".order", "w").write("x")
''')

SERVER = textwrap.dedent('''
    import builtins, sys
    from rpyc.core.service import SlaveService
    from rpyc.utils.server import ThreadedServer

    class EvilSlave(SlaveService):
        def on_connect(self, conn):
            super().on_connect(conn)
            builtins._tr_evil_conn = conn

    srv = ThreadedServer(EvilSlave, hostname="127.0.0.1",
                         port=int(sys.argv[1]))
    srv.start()
''')


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def evil_server(tmp_path):
    (tmp_path / "MetaTrader5.py").write_text(FAKE_MT5)
    out = tmp_path / "attack.jsonl"
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "TR_OUT": str(out)}
    p = subprocess.Popen([sys.executable, "-c", SERVER, str(port)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield port, out, p.pid
    p.kill()
    p.wait()


def _attacks(out):
    return [json.loads(x) for x in out.read_text().splitlines()] \
        if out.exists() else []


def test_klien_classic_mt5linux_dapat_dikendalikan_server(evil_server):
    """BUKTI RISIKO pola lama (rpyc.classic.connect)."""
    port, out, srv_pid = evil_server
    c = rpyc.classic.connect("127.0.0.1", port)
    c.modules.MetaTrader5.account_info()
    c.close()
    att = _attacks(out)
    assert att and att[0]["attack"] == "OK"
    assert att[0]["pid"] == os.getpid() != srv_pid   # kode di proses klien


class _Cfg:
    mt5_host = "127.0.0.1"
    mt5_login = "7001"
    mt5_password = "investor-ok"
    mt5_server = "Exness-MT5Real"


def test_provider_aman_server_tidak_bisa_memanggil_balik(evil_server):
    from datetime import datetime, timedelta, timezone

    from mt5client.mt5linux_provider import MT5LinuxProvider
    port, out, _ = evil_server
    cfg = _Cfg()
    cfg.mt5_port = port
    p = MT5LinuxProvider(cfg)
    p.connect()
    info = p.account_info()
    assert info == {"login": 7001, "server": "Exness-MT5Real",
                    "trade_allowed": False, "trade_mode": 2}
    now = datetime.now(timezone.utc)
    deals = p.history_deals_get(now - timedelta(days=1), now)
    assert deals and deals[0]["ticket"] == 1
    p.disconnect()
    att = _attacks(out)
    assert att and all(a["attack"] == "BLOCKED" for a in att), att


def test_provider_menolak_metode_non_baca(evil_server):
    from mt5client.mt5linux_provider import MT5LinuxProvider
    from mt5client.provider import MT5Error, ReadOnlyViolationError
    port, out, _ = evil_server
    cfg = _Cfg()
    cfg.mt5_port = port
    p = MT5LinuxProvider(cfg)
    p.connect()
    with pytest.raises(MT5Error):
        p._invoke("order_send")
    with pytest.raises(ReadOnlyViolationError):
        p.order_send({})
    # helper sisi server juga menolak nama di luar whitelist
    with pytest.raises(Exception):
        p._remote_call("order_send", "{}")
    p.disconnect()
    assert not os.path.exists(str(out) + ".order")


def test_provider_password_salah_ditolak(evil_server):
    from mt5client.mt5linux_provider import MT5LinuxProvider
    from mt5client.provider import MT5ConnectionError
    port, _, _ = evil_server
    cfg = _Cfg()
    cfg.mt5_port = port
    cfg.mt5_password = "salah"
    with pytest.raises(MT5ConnectionError) as ei:
        MT5LinuxProvider(cfg).connect()
    assert "Authorization failed" in str(ei.value)
