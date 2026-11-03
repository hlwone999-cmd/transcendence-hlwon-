"""Isolasi terminal MT5 per customer (brief §4).

Setiap customer mendapat:
- 1 akun MT5, 1 Wine prefix, 1 terminal, 1 direktori data,
- 1 koneksi RPyC di 1 port localhost unik (18812 + index),
- 1 unit systemd sendiri (restart satu terminal tidak menyentuh yang lain),
- 1 provider READ-ONLY + 1 lock sinkronisasi per akun.

Mode provisioning:
- "real" (VPS) : buat direktori + tulis unit systemd + petunjuk instalasi;
                 admin mengaktifkan unit (dokumen: docs/MULTI-CUSTOMER-
                 INSTALASI-PROVISIONING.md).
- "mock" (dev/test): hanya buat direktori kosong + catat unit — tanpa
                 Wine/MT5, sehingga test berjalan di mana saja.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from mt5client.provider import MT5Error
from security import crypto

log = logging.getLogger(__name__)

SYSTEMD_UNIT_TEMPLATE = """# Unit systemd per customer —Transcendence MT5 Terminal (read-only)
# Pasang:  sudo cp {unit_path} /etc/systemd/system/{service_name}.service
#          sudo systemctl daemon-reload && sudo systemctl enable --now {service_name}
# Restart SATU terminal TIDAK memengaruhi terminal customer lain:
#          sudo systemctl restart {service_name}
[Unit]
Description=Transcendence MT5 Terminal ({code}, RPyC localhost:{rpyc_port})
After=network.target

[Service]
Type=simple
User=transcendence
Environment=WINEPREFIX={wine_prefix}
Environment=WINEDEBUG=-all
WorkingDirectory={terminal_dir}
ExecStart=/usr/bin/wine {terminal_dir}/terminal64.exe /portable
Restart=on-failure
RestartSec=10
# RPyC mt5server.exe hanya bind 127.0.0.1:{rpyc_port} — JANGAN buka ke publik.
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


class TerminalError(Exception):
    pass


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
        """Alokasikan port + direktori + unit systemd untuk 1 customer.

        Dipanggil saat bind akun. Port = port rendah yang masih bebas
        dalam jendela [base, base+max_customers).
        """
        cfg = self._cfg
        n_terms = conn.execute(
            "SELECT COUNT(*) AS n FROM mt5_terminals").fetchone()["n"]
        if int(n_terms) >= cfg.max_customers:
            raise TerminalError(
                f"Kapasitas maksimal {cfg.max_customers} terminal tercapai")
        code_row = conn.execute("SELECT code FROM customers WHERE id=?",
                                (customer_id,)).fetchone()
        if code_row is None:
            raise TerminalError("customer tidak ditemukan")
        code = code_row["code"]

        taken = {int(r["rpyc_port"]) for r in conn.execute(
            "SELECT rpyc_port FROM mt5_terminals")}
        port = None
        for cand in range(cfg.rpyc_port_base,
                          cfg.rpyc_port_base + cfg.max_customers):
            if cand not in taken:
                port = cand
                break
        if port is None:
            raise TerminalError("Tidak ada port RPyC bebas di jendela alokasi")

        base = Path(cfg.term_base_dir) / code
        wine_prefix = base / "wine-prefix"
        terminal_dir = base / "terminal"
        data_dir = base / "data"
        service_name = f"transcendence-mt5-{code.lower()}"
        try:
            for d in (wine_prefix, terminal_dir, data_dir):
                d.mkdir(parents=True, exist_ok=True)
            unit_path = base / f"{service_name}.service"
            unit_path.write_text(SYSTEMD_UNIT_TEMPLATE.format(
                unit_path=unit_path, service_name=service_name, code=code,
                rpyc_port=port, wine_prefix=wine_prefix,
                terminal_dir=terminal_dir), encoding="utf-8")
        except OSError as e:
            raise TerminalError(
                f"Gagal menyiapkan direktori terminal {base}: {e}") from e

        status = "ready" if cfg.terminal_provision_mode == "mock" \
            else "provisioning"
        conn.execute(
            """INSERT INTO mt5_terminals (customer_id, rpyc_port, wine_prefix,
                 terminal_dir, data_dir, service_name, status)
               VALUES (?,?,?,?,?,?,?)""",
            (customer_id, port, str(wine_prefix), str(terminal_dir),
             str(data_dir), service_name, status))
        log.info("terminal %s diprovisikan: port=%d mode=%s",
                 service_name, port, cfg.terminal_provision_mode)
        return {"rpyc_port": port, "service_name": service_name,
                "wine_prefix": str(wine_prefix),
                "terminal_dir": str(terminal_dir), "data_dir": str(data_dir),
                "status": status}

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
        cred = conn.execute(
            "SELECT enc_password, key_version FROM mt5_credentials"
            " WHERE account_id=?", (account_id,)).fetchone()
        if cred is None:
            raise TerminalError(f"kredensial akun {account_id} tidak ada")
        password = crypto.decrypt_password(self._cfg, cred["enc_password"],
                                           cred["key_version"])

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
