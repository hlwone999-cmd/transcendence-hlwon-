"""Perbaikan audit #1: provisioning menjalankan terminal64.exe DAN
server RPyC (mt5server) per customer + health check koneksi NYATA.

Unit systemd diuji pada level artefak (isi unit + wrapper), dan health
check diuji dengan socket TCP sungguhan di localhost. Uji LIVE MT5
(Wine + terminal64.exe + mt5server asli) TIDAK dilakukan di sandbox —
dinyatakan eksplisit; tidak ada klaim live berhasil.
"""
import socket
import threading

import pytest

import db as dbmod
import auth as authmod
from config import Config
from cryptography.fernet import Fernet
from services.terminals import (
    TerminalRegistry,
    probe_rpyc_port,
)


@pytest.fixture()
def hcfg(tmp_path):
    c = Config()
    c.db_path = str(tmp_path / "health.db")
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


# --------------------------------------------------- artefak unit systemd
def test_unit_menjalankan_terminal_dan_server_rpyc(hconn, hcfg):
    """Unit systemd v2: ExecStart wrapper; wrapper meluncurkan terminal64.exe
    DAN mt5server RPyC; bind HANYA 127.0.0.1:<port>; ada ExecStop."""
    cid = _customer(hconn, hcfg, "unit1@test.id")
    reg = TerminalRegistry(hcfg)
    info = reg.provision_terminal(hconn, cid)
    hconn.commit()

    assert info["rpyc_host"] == "127.0.0.1"
    assert info["rpyc_port"] == 18812
    unit = open(f"{info['wine_prefix']}/../{info['service_name']}.service",
                encoding="utf-8").read()
    # wrapper dipakai (bukan wine terminal64.exe langsung)
    assert "ExecStart=/bin/sh " in unit
    assert "run-terminal.sh" in unit
    assert "ExecStop=/bin/sh " in unit and "stop-terminal.sh" in unit
    assert "Restart=on-failure" in unit
    assert "WINEPREFIX=" in unit
    assert "MT5SERVER_BIN=" in unit

    base = f"{info['terminal_dir']}"
    run_sh = open(f"{base}/run-terminal.sh", encoding="utf-8").read()
    # (1) terminal64.exe diluncurkan (background)
    assert "terminal64.exe" in run_sh and "/portable" in run_sh
    # (2) server RPyC FOREGROUND dengan host+port eksplisit
    assert "mt5server" in run_sh
    assert 'RPYC_HOST="127.0.0.1"' in run_sh
    assert 'RPYC_PORT="18812"' in run_sh
    assert "exec " in run_sh
    # stop script: hanya proses customer ini (pola port + direktori)
    stop_sh = open(f"{base}/stop-terminal.sh", encoding="utf-8").read()
    assert "18812" in stop_sh and "pkill" in stop_sh

    import os
    assert os.access(f"{base}/run-terminal.sh", os.X_OK)
    assert os.access(f"{base}/stop-terminal.sh", os.X_OK)


def test_port_berturut_per_customer(hconn, hcfg):
    reg = TerminalRegistry(hcfg)
    ports = []
    for i in range(4):
        cid = _customer(hconn, hcfg, f"seq{i}@test.id")
        info = reg.provision_terminal(hconn, cid)
        hconn.commit()
        ports.append(info["rpyc_port"])
    assert ports == [18812, 18813, 18814, 18815]


# ------------------------------------------------------- health check
def test_health_check_port_mati_jujur_gagal(hconn, hcfg):
    """Tanpa listener: reachable=False (TIDAK mengklaim live MT5)."""
    cid = _customer(hconn, hcfg, "dead@test.id")
    reg = TerminalRegistry(hcfg)
    info = reg.provision_terminal(hconn, cid)
    hconn.commit()
    assert probe_rpyc_port(info["rpyc_port"], timeout=0.5) is False
    detail = reg.health_check(hconn, cid, timeout=0.5)
    assert detail["reachable"] is False
    assert detail["host"] == "127.0.0.1"
    assert detail["port"] == info["rpyc_port"]
    # last_health_at tercatat
    row = hconn.execute(
        "SELECT last_health_at FROM mt5_terminals WHERE customer_id=?",
        (cid,)).fetchone()
    assert row["last_health_at"] == detail["checked_at"]


def test_health_check_port_hidup_koneksi_tcp_nyata(hconn, hcfg):
    """Dengan listener TCP sungguhan di port terminal: reachable=True.
    Ini bukti probe melakukan koneksi nyata, bukan stub."""
    cid = _customer(hconn, hcfg, "alive@test.id")
    reg = TerminalRegistry(hcfg)
    info = reg.provision_terminal(hconn, cid)
    hconn.commit()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", info["rpyc_port"]))
    srv.listen(4)
    accepted = threading.Event()

    def accepter():
        try:
            srv.settimeout(3)
            conn_sock, _ = srv.accept()
            conn_sock.close()
            accepted.set()
        except OSError:
            pass

    th = threading.Thread(target=accepter, daemon=True)
    th.start()
    try:
        assert probe_rpyc_port(info["rpyc_port"], timeout=2.0) is True
        assert accepted.wait(3), "listener harus benar-benar menerima koneksi"
        detail = reg.health_check(hconn, cid, timeout=2.0)
        assert detail["reachable"] is True
    finally:
        srv.close()
        th.join(timeout=3)


def test_health_check_customer_tanpa_terminal(hconn, hcfg):
    reg = TerminalRegistry(hcfg)
    detail = reg.health_check(hconn, 999999)
    assert detail["status"] == "missing" and detail["reachable"] is False


# ------------------------------------------------- catatan uji live MT5
def test_live_mt5_tidak_diklaim(hconn, hcfg):
    """Dokumentasi eksplisit dalam bentuk test: mode mock TIDAK pernah
    menghasilkan klaim koneksi MT5 live; deep check hanya di mode real."""
    cid = _customer(hconn, hcfg, "honest@test.id")
    reg = TerminalRegistry(hcfg)
    info = reg.provision_terminal(hconn, cid)
    hconn.commit()
    detail = reg.health_check(hconn, cid, deep=True, timeout=0.5)
    assert "mt5" not in detail or detail.get("mt5") is None, \
        "mode mock tidak boleh mengklaim koneksi MT5"
    assert detail["reachable"] is False
