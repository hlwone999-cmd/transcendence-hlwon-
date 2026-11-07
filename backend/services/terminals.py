"""Isolasi terminal MT5 per customer (brief §4; perbaikan audit #1).

Setiap customer mendapat:
- 1 akun MT5, 1 Wine prefix, 1 terminal, 1 direktori data,
- 1 server RPyC (mt5server) di 1 port localhost unik (18812 + index)
  — unit systemd menjalankan terminal64.exe DAN server RPyC (yang
  dibutuhkan MT5LinuxProvider), bukan terminal saja,
- 1 unit systemd sendiri (restart satu terminal tidak menyentuh yang lain),
- 1 provider READ-ONLY + 1 lock sinkronisasi per akun.

Mode provisioning:
- "real" (VPS) : buat direktori + tulis unit systemd + skrip wrapper
                 (run-terminal.sh / stop-terminal.sh) + petunjuk;
                 admin mengaktifkan unit (dokumen: docs/MULTI-CUSTOMER-
                 INSTALASI-PROVISIONING.md).
- "mock" (dev/test): hanya buat direktori kosong + catat unit — tanpa
                 Wine/MT5, sehingga test berjalan di mana saja.

Health check: health_check() melakukan koneksi TCP NYATA ke 127.0.0.1:
<port> (server RPyC), lalu — bila paket rpyc tersedia — HANDSHAKE RPyC
nyata (probe_rpyc_session; TCP probe saja TIDAK dianggap cukup, audit
round-2 #1), opsional deep (mode real) membuka sesi RPyC read-only via
provider dan memanggil terminal_info(). Status yang dilaporkan TIDAK
PERNAH mengklaim "live MT5 berhasil" tanpa koneksi sungguhan — tanpa
Wine/terminal, hasilnya reachable=False.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from pathlib import Path

from db import CAPACITY_LOCK_TERMINAL, INTEGRITY_ERRORS, capacity_guard
from mt5client.provider import MT5Error
from security import crypto

log = logging.getLogger(__name__)

RPYC_HOST = "127.0.0.1"   # WAJIB localhost — RPyC tidak punya auth kuat

# ---------------------------------------------------------------------
# CLI RESMI server RPyC (audit round-2 #1).
#
# Binary mt5server.exe (rilis repo mt5linux) adalah server RPyC klasik —
# entrypoint yang sama dengan `python -m mt5linux` (rpyc_classic):
#
#     mt5server.exe --host <host> --port <port>
#
# - Argumen adalah SWITCH (--host / -p --port), BUKAN posisional.
#   Bentuk posisional `mt5server.exe <host> <port>` TIDAK pernah
#   diterima CLI resmi (plumbum menolak positional tanpa handler).
# - Default --port 18812; default --host "" -> 127.0.0.1 (localhost).
#   Wrapper tetap mengirim --host 127.0.0.1 SECARA EKSPLISIT agar bind
#   localhost dijamin terlepas dari default versi rpyc apa pun.
# - Sumber: rpyc/cli/rpyc_classic.py (SwitchAttr ["-p","--port"],
#   SwitchAttr ["--host"]), mt5linux/__main__.py (0.2.4 & 1.1.1),
#   README mt5linux ("wine mt5server.exe [-p/--port <port>]"), dan
#   docs/MT5-UBUNTU-NO-DOCKER.md §4/§8 di repo ini.
# ---------------------------------------------------------------------
MT5SERVER_SWITCHES = ("--host", "--port")


def mt5server_command(server_bin: str, host: str, port: int) -> list[str]:
    """Argv resmi peluncuran server RPyC (sumber tunggu wrapper + test).

    Contoh: mt5server_command('mt5server.exe', '127.0.0.1', 18813)
         -> ['mt5server.exe', '--host', '127.0.0.1', '--port', '18813']
    """
    return [str(server_bin), "--host", str(host), "--port", str(int(port))]


SYSTEMD_UNIT_TEMPLATE = """# Unit systemd per customer — Transcendence MT5 Terminal + RPyC mt5server (read-only)
# Pasang:  sudo cp {unit_path} /etc/systemd/system/{service_name}.service
#          sudo systemctl daemon-reload && sudo systemctl enable --now {service_name}
# Restart SATU terminal TIDAK memengaruhi terminal customer lain:
#          sudo systemctl restart {service_name}
# Status/health check:
#          systemctl status {service_name}
#          curl -sS --max-time 3 127.0.0.1:{rpyc_port} >/dev/null && echo port-up
#
# ExecStart memanggil wrapper yang meluncurkan (1) terminal64.exe di
# background dan (2) server RPyC (mt5server) di FOREGROUND dengan CLI
# resmi rpyc-classic: `mt5server --host 127.0.0.1 --port {rpyc_port}`
# (switch --host/--port, BUKAN argumen posisional) — jalur data yang
# dipakai MT5LinuxProvider — sehingga systemd mengawasi keduanya:
# bila server RPyC mati, unit di-restart (terminal ikut di-launch ulang).
[Unit]
Description=Transcendence MT5 Terminal + RPyC ({code}, localhost:{rpyc_port})
After=network.target

[Service]
Type=simple
User=transcendence
Environment=WINEPREFIX={wine_prefix}
Environment=WINEDEBUG=-all
Environment=MT5SERVER_BIN={mt5server_bin}
WorkingDirectory={terminal_dir}
ExecStart=/bin/sh {terminal_dir}/run-terminal.sh
ExecStop=/bin/sh {terminal_dir}/stop-terminal.sh
Restart=on-failure
RestartSec=10
# RPyC mt5server HANYA bind 127.0.0.1:{rpyc_port} — JANGAN buka ke publik.
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""

RUN_TERMINAL_TEMPLATE = """#!/bin/sh
# Wrapper per customer — Transcendence MT5 (READ-ONLY; audit #1).
# 1) terminal64.exe diluncurkan di background (Wine, prefix terisolasi).
# 2) server RPyC (mt5server) berjalan di FOREGROUND dan HANYA bind
#    127.0.0.1:{port} — systemd mengawal proses ini.
# CLI resmi mt5server (rpyc-classic): SWITCH --host/--port, BUKAN
# posisional — lihat mt5server_command() di services/terminals.py.
# Ganti binary server via env MT5SERVER_BIN bila memakai python di Wine
# — CLI-nya SAMA (`python -m mt5linux` = rpyc_classic):
#   MT5SERVER_BIN="C:\\\\python\\\\python.exe -m mt5linux"
# (argumen --host/--port ditambahkan wrapper di bawah; jangan menulis
#  argumen posisional "<host> <port>" — tidak diterima CLI resmi.)
set -eu
export WINEPREFIX="{wine_prefix}"
export WINEDEBUG=-all
WINE_BIN="{wine_bin}"
TERMINAL_DIR="{terminal_dir}"
RPYC_HOST="{host}"
RPYC_PORT="{port}"

# --- (1) terminal MT5 (background) ---
# /portable: profil terminal di dalam terminal_dir (isolasi per customer).
if ! pgrep -f "terminal64.exe.*$TERMINAL_DIR" >/dev/null 2>&1; then
    "$WINE_BIN" "$TERMINAL_DIR/terminal64.exe" /portable &
fi

# --- (2) server RPyC (foreground; dikelola systemd) ---
SERVER_BIN="${{MT5SERVER_BIN:-$TERMINAL_DIR/mt5server.exe}}"
# shellcheck disable=SC2086  -- MT5SERVER_BIN boleh berisi argumennya sendiri
exec "$WINE_BIN" $SERVER_BIN --host "$RPYC_HOST" --port "$RPYC_PORT"
"""

STOP_TERMINAL_TEMPLATE = """#!/bin/sh
# Stop elegan proses customer ini SAJA — terminal customer lain tidak disentuh
# (pola cocok dibatasi direktori terminal + port, bukan nama generik).
TERMINAL_DIR="{terminal_dir}"
RPYC_PORT="{port}"
pkill -TERM -f "terminal64.exe.*$TERMINAL_DIR" 2>/dev/null || true
pkill -TERM -f "mt5server.*$RPYC_PORT" 2>/dev/null || true
sleep 2
pkill -KILL -f "terminal64.exe.*$TERMINAL_DIR" 2>/dev/null || true
pkill -KILL -f "mt5server.*$RPYC_PORT" 2>/dev/null || true
exit 0
"""


class TerminalError(Exception):
    pass


def probe_rpyc_port(port: int, host: str = RPYC_HOST,
                    timeout: float = 2.0) -> bool:
    """Probe transport: TCP connect ke 127.0.0.1:<port> (socket nyata).

    Hanya membuktikan port terbuka — TIDAK membuktikan server RPyC sah.
    Untuk kebenaran penuh gunakan probe_rpyc_session() (audit round-2 #1:
    "TCP probe saja tidak cukup").
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe_rpyc_session(port: int, host: str = RPYC_HOST,
                       timeout: float = 2.0) -> bool | None:
    """Handshake RPyC NYATA (bukan sekadar TCP probe).

    - True  : sesi RPyC nyata terbuka (handshake servis berjalan) lalu
              ditutup rapi — server RPyC yang SAH di port ini.
    - False : port terbuka tapi bukan server RPyC (handshake gagal),
              atau koneksi gagal.
    - None  : paket rpyc tidak terpasang di backend (jalur terbatas:
              hasil TCP probe tidak pernah diperlakukan sebagai bukti
              server RPyC — field rpyc_handshake=None berarti tak terukur).

    Host selalu loopback: connect localhost sukses/refused seketika,
    tidak ada blackhole jaringan; rpyc.connect tanpa arg timeout pun
    berbatas wajar di 127.0.0.1.
    """
    try:
        import rpyc  # noqa: PLC0415 — lazy: backend tetap jalan tanpa rpyc
    except ImportError:
        return None
    try:
        conn = rpyc.connect(host, int(port),
                            config={"sync_request_timeout":
                                    max(float(timeout), 5.0)})
        try:
            # paksa satu round-trip nyata agar handshake benar-benar terjadi
            _ = conn.root
        finally:
            conn.close()
        return True
    except Exception:  # noqa: BLE001 — probe tak boleh meledak
        return False


class AccountRuntime:
    """State runtime per akun: provider, lock, backoff (brief §6)."""

    def __init__(self, account_id: int, customer_id: int, provider):
        self.account_id = account_id
        self.customer_id = customer_id
        self.provider = provider
        self.lock = threading.Lock()
        self.consecutive_failures = 0
        self.next_allowed_ts = 0.0
        self.last_error: str | None = None

    def backoff_penalty(self, base: int = 30, cap: int = 1800) -> float:
        """Detik tunggu berikutnya: base * 2^n (cap 30 menit) + jitter."""
        wait = min(cap, base * (2 ** min(self.consecutive_failures, 6)))
        return wait * (0.9 + 0.2 * (time.monotonic() % 1.0) / 1.0)


class TerminalRegistry:
    """Registri runtime per akun + provisioning terminal per customer."""

    def __init__(self, config, provider_builder=None):
        self._cfg = config
        # provider_builder(acc_cfg) -> provider (dipakai test utk mock;
        # default: MT5LinuxProvider nyata READ-ONLY)
        self._provider_builder = provider_builder
        self._lock = threading.Lock()
        self._runtimes: dict[int, AccountRuntime] = {}

    # ------------------------------------------------------- provisioning
    def provision_terminal(self, conn, customer_id: int) -> dict:
        """Alokasikan port + direktori + unit systemd utk 1 customer.

        Dipanggil saat bind akun. Port = port rendah yang masih bebas
        dalam jendela [base, base+max_customers).

        Race condition (audit #4): pemilihan port + INSERT diserialisasi
        capacity_guard (PG advisory lock / SQLite BEGIN IMMEDIATE) dan
        dibackstop UNIQUE(rpyc_port) di skema — bila dua bind bersamaan
        memilih port sama, yang kalah di-retry (maks 5x) lalu gagal
        rapi dengan TerminalError, bukan 500.
        """
        cfg = self._cfg
        code_row = conn.execute("SELECT code FROM customers WHERE id=?",
                                (customer_id,)).fetchone()
        if code_row is None:
            raise TerminalError("customer tidak ditemukan")
        code = code_row["code"]

        base = Path(cfg.term_base_dir) / code
        wine_prefix = base / "wine-prefix"
        terminal_dir = base / "terminal"
        data_dir = base / "data"
        service_name = f"transcendence-mt5-{code.lower()}"
        mt5server_bin = f"{terminal_dir}/mt5server.exe"
        wine_bin = getattr(cfg, "wine_bin", "/usr/bin/wine") or "/usr/bin/wine"

        last_err: Exception | None = None
        for _attempt in range(5):
            try:
                with capacity_guard(conn, CAPACITY_LOCK_TERMINAL):
                    n_terms = conn.execute(
                        "SELECT COUNT(*) AS n FROM mt5_terminals"
                    ).fetchone()["n"]
                    if int(n_terms) >= cfg.max_customers:
                        raise TerminalError(
                            f"Kapasitas maksimal {cfg.max_customers} "
                            "terminal tercapai")
                    taken = {int(r["rpyc_port"]) for r in conn.execute(
                        "SELECT rpyc_port FROM mt5_terminals")}
                    port = None
                    for cand in range(cfg.rpyc_port_base,
                                      cfg.rpyc_port_base + cfg.max_customers):
                        if cand not in taken:
                            port = cand
                            break
                    if port is None:
                        raise TerminalError(
                            "Tidak ada port RPyC bebas di jendela alokasi")

                    try:
                        for d in (wine_prefix, terminal_dir, data_dir):
                            d.mkdir(parents=True, exist_ok=True)
                        unit_path = base / f"{service_name}.service"
                        unit_path.write_text(SYSTEMD_UNIT_TEMPLATE.format(
                            unit_path=unit_path, service_name=service_name,
                            code=code, rpyc_port=port,
                            wine_prefix=wine_prefix,
                            terminal_dir=terminal_dir,
                            mt5server_bin=mt5server_bin), encoding="utf-8")
                        run_sh = terminal_dir / "run-terminal.sh"
                        run_sh.write_text(RUN_TERMINAL_TEMPLATE.format(
                            wine_prefix=wine_prefix, wine_bin=wine_bin,
                            terminal_dir=terminal_dir, host=RPYC_HOST,
                            port=port), encoding="utf-8")
                        stop_sh = terminal_dir / "stop-terminal.sh"
                        stop_sh.write_text(STOP_TERMINAL_TEMPLATE.format(
                            terminal_dir=terminal_dir, port=port),
                            encoding="utf-8")
                        for sh in (run_sh, stop_sh):
                            sh.chmod(0o755)
                    except OSError as e:
                        raise TerminalError(
                            f"Gagal menyiapkan direktori terminal {base}: "
                            f"{e}") from e

                    status = "ready" if cfg.terminal_provision_mode == "mock" \
                        else "provisioning"
                    conn.execute(
                        """INSERT INTO mt5_terminals (customer_id, rpyc_port,
                             wine_prefix, terminal_dir, data_dir,
                             service_name, status)
                           VALUES (?,?,?,?,?,?,?)""",
                        (customer_id, port, str(wine_prefix),
                         str(terminal_dir), str(data_dir), service_name,
                         status))
                    log.info("terminal %s diprovisikan: port=%d mode=%s "
                             "(terminal64.exe + mt5server RPyC)",
                             service_name, port, cfg.terminal_provision_mode)
                    return {"rpyc_port": port, "rpyc_host": RPYC_HOST,
                            "service_name": service_name,
                            "wine_prefix": str(wine_prefix),
                            "terminal_dir": str(terminal_dir),
                            "data_dir": str(data_dir),
                            "mt5server_bin": mt5server_bin,
                            "status": status}
            except INTEGRITY_ERRORS as e:  # balapan port antar request
                last_err = e
                conn.rollback()   # buang transaksi bermasalah, coba lagi
                continue
        raise TerminalError(
            f"Alokasi port terminal gagal setelah beberapa percobaan: "
            f"{last_err}")

    # ------------------------------------------------------- health check
    def health_check(self, conn, customer_id: int, deep: bool = False,
                     timeout: float = 3.0) -> dict:
        """Health check koneksi NYATA ke server RPyC customer.

        - Selalu   : koneksi TCP ke 127.0.0.1:<rpyc_port> (socket nyata).
        - deep=True: bila mode "real" dan akun ter-bind, buka sesi RPyC
          read-only via provider dan panggil terminal_info().

        Jujur terhadap status: tanpa listener/MT5 sungguhan hasilnya
        reachable=False (TIDAK mengklaim live MT5 berhasil).
        """
        from mt5client.provider import utcnow_iso
        row = conn.execute(
            "SELECT * FROM mt5_terminals WHERE customer_id=?",
            (customer_id,)).fetchone()
        if row is None:
            return {"customer_id": customer_id, "status": "missing",
                    "reachable": False, "checked_at": utcnow_iso()}
        port = int(row["rpyc_port"])
        reachable = probe_rpyc_port(port, timeout=timeout)
        # Audit round-2 #1: TCP probe SAJA tidak cukup — bila rpyc
        # tersedia, lakukan handshake RPyC nyata sebelum menyebut
        # server RPyC hidup. None = rpyc tidak terpasang (tak terukur).
        handshake = probe_rpyc_session(port, timeout=timeout) \
            if reachable else False
        detail: dict = {"customer_id": customer_id, "port": port,
                        "host": RPYC_HOST, "reachable": reachable,
                        "rpyc_handshake": handshake,
                        "checked_at": utcnow_iso(),
                        "mode": self._cfg.terminal_provision_mode}
        if reachable and deep and self._cfg.terminal_provision_mode == "real":
            acc = conn.execute(
                "SELECT id FROM mt5_accounts WHERE customer_id=?",
                (customer_id,)).fetchone()
            if acc is not None:
                try:
                    rt = self.runtime_for(conn, int(acc["id"]))
                    rt.provider.connect()
                    ti = rt.provider.terminal_info()
                    detail["mt5"] = {"connected": True,
                                     "terminal_info": ti}
                except MT5Error as e:
                    detail["mt5"] = {"connected": False,
                                     "error": str(e)[:200]}
        # catat waktu health terakhir (kolom last_health_at tersedia)
        try:
            conn.execute(
                "UPDATE mt5_terminals SET last_health_at=? "
                "WHERE customer_id=?", (detail["checked_at"], customer_id))
            conn.commit()
        except Exception:  # noqa: BLE001 - health check tak boleh meledak
            conn.rollback()
        return detail

    # ----------------------------------------------------------- provider
    def _account_config(self, conn, account_id: int):
        """Config provider per akun: host/port terminal + kredensial terdekripsi."""
        acc = conn.execute(
            """SELECT a.id, a.customer_id, a.login, a.server,
                      t.rpyc_port, t.status AS term_status
               FROM mt5_accounts a
               JOIN mt5_terminals t ON t.customer_id = a.customer_id
               WHERE a.id=?""", (account_id,)).fetchone()
        if acc is None:
            raise TerminalError(f"akun {account_id} tidak ditemukan")
        # RLS defense-in-depth (audit #2): scope koneksi ke customer akun
        # SEBELUM membaca mt5_credentials (tabel ber-RLS di PostgreSQL).
        #
        # AUDIT ROUND-4 #1: scope DIPULIHKAN ke kondisi pemanggil setelah
        # kredensial terbaca. Versi lama membiarkan scope terpasang —
        # runtime_for (cache miss) meninggalkan scope customer di koneksi
        # thread-local, jadi tugas berikutnya pada thread itu mewarisi
        # scope (leak satu kelas dengan urutan lock/context di sync.py;
        # regression test test_sync_lock_busy_tidak_meninggalkan_scope
        # membuktikan keduanya). Pemulihan memakai set_customer_context
        # (soft-clear bila pemanggil tanpa scope) — TANPA rollback, aman
        # di tengah transaksi pemanggil, dan bila pemanggil SUDAH di-
        # scope customer lain, scope itu dipasang kembali apa adanya.
        from db import is_postgres, set_customer_context
        prev = (conn.current_customer_context()
                if is_postgres(conn) else None)
        set_customer_context(conn, int(acc["customer_id"]))
        try:
            cred = conn.execute(
                "SELECT enc_password, key_version FROM mt5_credentials"
                " WHERE account_id=?", (account_id,)).fetchone()
            if cred is None:
                raise TerminalError(f"kredensial akun {account_id} tidak ada")
            password = crypto.decrypt_password(self._cfg, cred["enc_password"],
                                               cred["key_version"])
        finally:
            set_customer_context(conn, prev)

        class _AccCfg:  # namespace ringan — antarmuka sama dengan Config
            mt5_host = "127.0.0.1"
            mt5_port = int(acc["rpyc_port"])
            mt5_login = str(acc["login"])
            mt5_password = password          # SECRET — tidak di-log
            mt5_server = acc["server"]
            mt5_provider = "mt5linux"

        return _AccCfg()

    def _new_provider(self, conn, account_id: int):
        acc_cfg = self._account_config(conn, account_id)
        if self._provider_builder is not None:
            return self._provider_builder(acc_cfg)
        from mt5client.mt5linux_provider import MT5LinuxProvider
        return MT5LinuxProvider(acc_cfg)

    # ------------------------------------------------------------ runtime
    def runtime_for(self, conn, account_id: int) -> AccountRuntime:
        """Ambil (atau buat) runtime per akun. Provider dibuat lazy."""
        with self._lock:
            rt = self._runtimes.get(account_id)
            if rt is not None:
                return rt
        provider = self._new_provider(conn, account_id)
        acc = conn.execute(
            "SELECT customer_id FROM mt5_accounts WHERE id=?",
            (account_id,)).fetchone()
        rt = AccountRuntime(account_id, int(acc["customer_id"]), provider)
        with self._lock:
            self._runtimes[account_id] = rt
        return rt

    def evict(self, account_id: int) -> None:
        """Buang runtime akun (unbind) — terminal lain tidak terganggu."""
        with self._lock:
            rt = self._runtimes.pop(account_id, None)
        if rt is not None:
            try:
                rt.provider.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def active_account_ids(self, conn, limit: int | None = None) -> list[int]:
        """Akun customer aktif, terurut (basis scheduler per-akun)."""
        rows = conn.execute(
            """SELECT a.id FROM mt5_accounts a
               JOIN customers c ON c.id = a.customer_id
               WHERE c.status='active' ORDER BY a.id""").fetchall()
        ids = [int(r["id"]) for r in rows]
        return ids[:limit] if limit else ids

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"account_id": r.account_id,
                     "customer_id": r.customer_id,
                     "failures": r.consecutive_failures}
                    for r in self._runtimes.values()]
