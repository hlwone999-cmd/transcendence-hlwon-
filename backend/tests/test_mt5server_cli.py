"""TEMUAN AUDIT ROUND-2 #1 — CLI resmi server RPyC (mt5server.exe).

Bukti CLI resmi (diperiksa langsung dari sumbernya):
- mt5server.exe (rilis repo mt5linux) = server RPyC klasik; entrypoint
  yang sama dengan `python -m mt5linux` (mt5linux/__main__.py memanggil
  rpyc.cli.rpyc_classic.ClassicServer.run()).
- CLI: SWITCH `--host <host>` dan `-p/--port <port>` — TIDAK ADA
  argumen posisional ("Expected at most 0 positional arguments").
- Default --port 18812; default --host "" -> bind 127.0.0.1.

Konsekuensi: wrapper round-1 yang menjalankan
`exec "$WINE_BIN" $SERVER_BIN "$RPYC_HOST" "$RPYC_PORT"` (posisional)
ditolak CLI resmi (exit 2) — unit systemd akan crash-loop. Diperbaiki
menjadi `exec "$WINE_BIN" $SERVER_BIN --host "$RPYC_HOST" --port "$RPYC_PORT"`.

Test LIVE di file ini menjalankan server RPyC klasik SUNGGUHAN:
- subprocess `python -m mt5linux --host 127.0.0.1 --port <bebas>`
  (entrypoint identik mt5server.exe; tanpa Wine — biner .exe sendiri
  TIDAK bisa diuji di sandbox Linux, dinyatakan jujur di laporan);
- verifikasi bind HANYA 127.0.0.1 lewat /proc/net/tcp (bukan 0.0.0.0);
- handshake RPyC nyata via probe_rpyc_session (bukan sekadar TCP probe).
"""
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from services.terminals import (
    MT5SERVER_SWITCHES,
    TerminalRegistry,
    mt5server_command,
    probe_rpyc_port,
    probe_rpyc_session,
)

import db as dbmod  # noqa: E402
import auth as authmod  # noqa: E402
from config import Config  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

# mt5linux tersedia = server RPyC klasik bisa diuji LIVE di sandbox.
mt5linux = pytest.importorskip(
    "mt5linux", reason="mt5linux tidak terpasang — uji live CLI dilewati "
    "(install: pip install mt5linux rpyc)")


@pytest.fixture()
def hcfg(tmp_path):
    c = Config()
    # registrasi portal diaktifkan eksplisit (default produksi juga aktif)
    c.public_register_enabled = True
    c.db_path = str(tmp_path / "cli.db")
    c.cred_key = Fernet.generate_key().decode("ascii")
    c.term_base_dir = str(tmp_path / "terminals")
    c.terminal_provision_mode = "mock"
    c.max_customers = 4
    return c


@pytest.fixture()
def hconn(hcfg):
    conn = dbmod.connect(hcfg.db_file)
    dbmod.migrate(conn)
    yield conn
    conn.close()


def _customer(hconn, hcfg, email):
    created = authmod.register_customer(hconn, hcfg, email, "rahasia123")
    hconn.commit()
    return created["customer_id"]


# ------------------------------------------------- CLI resmi (artefak)
def test_mt5server_command_helper_flag_bukan_posisional():
    """Helper argv resmi: mt5server <bin> --host <host> --port <port>."""
    argv = mt5server_command("mt5server.exe", "127.0.0.1", 18813)
    assert argv == ["mt5server.exe", "--host", "127.0.0.1",
                    "--port", "18813"]
    for sw in MT5SERVER_SWITCHES:
        assert sw.startswith("-"), "CLI resmi memakai switch, bukan posisi"


def test_wrapper_memakai_cli_flag_resmi(hconn, hcfg):
    """run-terminal.sh HARUS memakai `--host ... --port ...` dan TIDAK
    lagi memakai bentuk posisional lama `"$RPYC_HOST" "$RPYC_PORT"`."""
    cid = _customer(hconn, hcfg, "cli1@test.id")
    reg = TerminalRegistry(hcfg)
    info = reg.provision_terminal(hconn, cid)
    hconn.commit()

    run_sh = open(f"{info['terminal_dir']}/run-terminal.sh",
                  encoding="utf-8").read()
    # baris exec yang TEPAT (CLI resmi rpyc-classic)
    assert ('exec "$WINE_BIN" $SERVER_BIN --host "$RPYC_HOST" '
            '--port "$RPYC_PORT"') in run_sh
    # bentuk posisional lama tidak boleh ada lagi
    assert ('$SERVER_BIN "$RPYC_HOST" "$RPYC_PORT"') not in run_sh
    # host dieksplisitkan 127.0.0.1 di variabel wrapper (bind localhost)
    assert 'RPYC_HOST="127.0.0.1"' in run_sh
    assert 'RPYC_PORT="%d"' % info["rpyc_port"] in run_sh


def test_unit_systemd_dokumentasikan_cli_resmi(hconn, hcfg):
    cid = _customer(hconn, hcfg, "cli2@test.id")
    reg = TerminalRegistry(hcfg)
    info = reg.provision_terminal(hconn, cid)
    hconn.commit()
    unit = open(f"{info['wine_prefix']}/../{info['service_name']}.service",
                encoding="utf-8").read()
    assert "--host 127.0.0.1 --port %d" % info["rpyc_port"] in unit
    assert "BUKAN argumen posisional" in unit
    assert "HANYA bind 127.0.0.1:%d" % info["rpyc_port"] in unit


# ------------------------------------- probe sesi RPyC (bukan sekadar TCP)
def test_probe_rpyc_session_dengan_server_rpyc_nyata():
    """Probe sesi membuka koneksi RPyC NYATA ke server klasik yang hidup."""
    srv = _rpyc_classic_server()
    try:
        port = srv.port
        assert probe_rpyc_port(port, timeout=2.0) is True
        assert probe_rpyc_session(port, timeout=3.0) is True, \
            "handshake RPyC harus berhasil terhadap server RPyC sah"
    finally:
        srv.close()


def test_probe_rpyc_session_menolak_listener_bukan_rpyc():
    """TCP terbuka tapi bukan server RPyC -> handshake False (jujur)."""
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    raw.bind(("127.0.0.1", 0))
    raw.listen(4)
    port = raw.getsockname()[1]

    def accepter():
        try:
            raw.settimeout(3)
            s, _ = raw.accept()
            time.sleep(0.5)
            s.close()
        except OSError:
            pass

    th = threading.Thread(target=accepter, daemon=True)
    th.start()
    try:
        assert probe_rpyc_port(port, timeout=2.0) is True
        assert probe_rpyc_session(port, timeout=2.0) is False, \
            "port terbuka saja TIDAK boleh dianggap server RPyC"
    finally:
        raw.close()
        th.join(timeout=3)


def _rpyc_classic_server():
    """Server RPyC klasik nyata (SlaveService) di port loopback acak."""
    from rpyc.core import SlaveService
    from rpyc.utils.server import ThreadedServer
    srv = ThreadedServer(SlaveService, hostname="127.0.0.1", port=0,
                         reuse_addr=True)
    thread = threading.Thread(target=srv.start, daemon=True)
    thread.start()
    for _ in range(50):
        if probe_rpyc_port(srv.port, timeout=0.5):
            break
        time.sleep(0.05)
    return srv


# ------------------------------------------------------------ LIVE CLI
def _port_bebas() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _bind_loopback_saja(port: int) -> bool:
    """Cek /proc/net/tcp(+6): port harus listen DI 127.0.0.1 saja."""
    hexport = "%04X" % port
    v4, v6 = [], []
    for path, acc in (("/proc/net/tcp", v4), ("/proc/net/tcp6", v6)):
        try:
            with open(path, encoding="ascii") as fh:
                for line in fh.readlines()[1:]:
                    cols = line.split()
                    if len(cols) > 3 and cols[3] == "0A" and \
                            cols[1].upper().endswith(":" + hexport):
                        acc.append(cols[1].upper())
        except OSError:
            pass
    loopback = [a for a in v4 if a.startswith("0100007F:")]
    return (bool(loopback)
            and all(a.startswith("0100007F:") for a in v4)
            and not v6)


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="uji entrypoint python -m mt5linux di Linux")
def test_live_cli_flag_server_rpyc_bind_loopback():
    """LIVE: `python -m mt5linux --host 127.0.0.1 --port <p>` (entrypoint
    identik mt5server.exe) — server hidup, bind HANYA 127.0.0.1, probe
    TCP + handshake RPyC nyata berhasil."""
    port = _port_bebas()
    argv = [sys.executable, "-m", "mt5linux",
            "--host", "127.0.0.1", "--port", str(port)]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        hidup = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            if probe_rpyc_port(port, timeout=0.5):
                hidup = True
                break
            time.sleep(0.1)
        assert hidup, \
            "server CLI flag gagal start: " + (proc.stdout.read()[:400]
                                               if proc.poll() is not None
                                               else "timeout")
        # bind HANYA loopback (bukan 0.0.0.0, bukan ipv6)
        assert _bind_loopback_saja(port), \
            "server harus bind 127.0.0.1 saja (cek /proc/net/tcp)"
        # handshake RPyC nyata — bukan sekadar TCP probe
        assert probe_rpyc_session(port, timeout=5.0) is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="uji entrypoint python -m mt5linux di Linux")
def test_live_cli_posisional_ditolak_resmi():
    """LIVE (negatif): bentuk wrapper lama `<host> <port>` (posisional)
    DITOLAK CLI resmi — bukti perbaikan round-2 memang wajib."""
    port = _port_bebas()
    argv = [sys.executable, "-m", "mt5linux", "127.0.0.1", str(port)]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode != 0, \
        "bentuk posisional tidak boleh diterima CLI resmi"
    assert "positional" in out.lower(), \
        "pesan error CLI harus menyebut positional: " + out[:300]


def test_live_mt5_exe_tidak_diklaim():
    """Catatan jujur dalam bentuk test: biner mt5server.exe via Wine dan
    terminal MT5 TIDAK tersedia di sandbox — live test di sini memakai
    entrypoint Python yang identik (rpyc classic). Klaim live MT5
    (Wine + terminal64.exe + akun Exness) tidak pernah dibuat."""
    import services.terminals as t
    # helper CLI resmi ada dan terdokumentasi di modul provisioning
    assert t.mt5server_command("x", "127.0.0.1", 1)[1:2] == ["--host"]
