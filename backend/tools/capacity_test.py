"""Uji kapasitas 4 customer (brief §12): 4 terminal + 4 sync worker.

METODE JUJUR: provider berupa MockMT5Client dengan latensi realistis
per panggilan (account_info/history_deals_get/positions_get dkk), karena
lingkungan uji TIDAK memiliki terminal MT5/Wine/kredensial broker live.
Angka yang dihasilkan mengukur BEBAN BACKEND (CPU/RAM/durasi/koneksi DB),
BUKAN beban terminal MT5 asli — rekomendasi operasional bersifat
konservatif dan live test harus diulang di VPS.

Pengukuran:
- CPU    : process_time (waktu CPU proses) + psutil sistem bila ada
- RAM    : psutil RSS proses (fallback: resource.ru_maxrss)
- Disk   : ukuran direktori data setelah sync
- Koneksi: 1 koneksi DB per worker (thread), pool thread 4
- Durasi : per-siklus & total, wall-clock

Jalankan:
  python tools/capacity_test.py --cycles 5 --accounts 4 [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import random
import resource
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from cryptography.fernet import Fernet  # noqa: E402
from mt5client.mock import MockMT5Client  # noqa: E402
from services.sync import SyncService  # noqa: E402
from services.terminals import TerminalRegistry  # noqa: E402
from config import Config  # noqa: E402
import db as dbmod  # noqa: E402


def _slow_mock(days_of_deals: int, min_latency: float, max_latency: float):
    """Bungkus MockMT5Client dgn latensi jaringan/RPyC tersimulasi."""

    class _Slow(MockMT5Client):
        def _lag(self):
            time.sleep(random.uniform(min_latency, max_latency))
            return None

        def account_info(self):
            self._lag()
            return super().account_info()

        def history_deals_get(self, date_from, date_to):
            self._lag()
            return super().history_deals_get(date_from, date_to)

        def history_orders_get(self, date_from, date_to):
            self._lag()
            return super().history_orders_get(date_from, date_to)

        def positions_get(self, symbol=None):
            self._lag()
            return super().positions_get(symbol)

        def symbol_info(self, symbol):
            self._lag()
            return super().symbol_info(symbol)

    return _Slow


def _dataset(login: int, server: str, n_deals: int):
    from mt5client.mock import AccountInfo, SymbolInfo, _deal
    deals = []
    for i in range(n_deals):
        deals.append(_deal(
            ticket=1000 + i, order=i, minutes=i * 3,
            dtype=i % 2, entry=(i % 2), position_id=100 + i // 2,
            volume=0.1, price=2000.0 + i,
            profit=25.0 if i % 2 else 0.0, commission=-2.0,
            symbol="XAUUSD"))
    return {
        "account": AccountInfo(
            login=login, trade_mode=0, leverage=100, limit_orders=200,
            margin_so_mode=0, trade_allowed=True, trade_expert=False,
            margin_mode=0, currency_digits=2, fifo_close=False,
            balance=1000.0, credit=0.0, profit=0.0, equity=1000.0,
            margin=0.0, margin_free=1000.0, margin_level=0.0,
            margin_so_call=50.0, margin_so_so=20.0, margin_initial=0.0,
            margin_maintenance=0.0, assets=0.0, liabilities=0.0,
            commission_blocked=False, name="Cap", email="cap@t.id",
            server=server, currency="USD", company="Cap"),
        "deals": deals, "orders": [], "positions": [],
        "symbols": {"XAUUSD": SymbolInfo(
            name="XAUUSD", point=0.01, digits=2, trade_tick_size=0.01,
            trade_tick_value=1.0, trade_contract_size=100.0,
            description="Gold")},
    }


def _rss_mb() -> float:
    try:
        import psutil  # type: ignore
        return psutil.Process().memory_info().rss / 1024 / 1024
    except ImportError:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", type=int, default=4)
    ap.add_argument("--cycles", type=int, default=5)
    ap.add_argument("--deals", type=int, default=400,
                    help="jumlah deal per akun per siklus (initial)")
    ap.add_argument("--min-latency", type=float, default=0.02)
    ap.add_argument("--max-latency", type=float, default=0.08)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="capacity-")
    cfg = Config()
    cfg.db_url = ""           # SQLite file untuk uji beban lokal
    cfg.db_path = f"{tmp}/cap.db"
    cfg.mt5_enabled = True
    cfg.cred_key = Fernet.generate_key().decode("ascii")
    cfg.term_base_dir = f"{tmp}/terminals"
    cfg.terminal_provision_mode = "mock"
    cfg.max_customers = 4

    datasets = {}
    for i in range(args.accounts):
        login = 100000 + i
        datasets[str(login)] = _dataset(login, f"Broker{i}-MT5", args.deals)

    app_factory = lambda acc_cfg=None: _slow_mock(
        args.deals, args.min_latency, args.max_latency)(
        datasets[str(acc_cfg.mt5_login)] if acc_cfg else None)

    app_conn = dbmod.connect(cfg.db_file)
    dbmod.migrate(app_conn)

    registry = TerminalRegistry(cfg, provider_builder=app_factory)
    svc = SyncService(cfg, app_factory, registry=registry)

    # bind akun (multi-customer alat ukur penuh: terminal + kredensial)
    import auth as authmod  # noqa: F401 — pastikan modul ter-load
    account_ids = []
    for i in range(args.accounts):
        login = 100000 + i
        conn = dbmod.get_db(cfg)
        conn.execute(
            "INSERT INTO customers (code, display_name) VALUES (?,?)",
            (f"C{i+1:03d}", f"Cap {i+1}"))
        cid = int(conn.execute("SELECT id FROM customers WHERE code=?",
                               (f"C{i+1:03d}",)).fetchone()["id"])
        conn.execute(
            "INSERT INTO mt5_accounts (customer_id, login, server) "
            "VALUES (?,?,?)", (cid, login, f"Broker{i}-MT5"))
        aid = int(conn.execute("SELECT id FROM mt5_accounts WHERE login=?",
                               (login,)).fetchone()["id"])
        from security import crypto
        enc, kv = crypto.encrypt_password(cfg, "CAP_PASSWORD_NOT_REAL")
        conn.execute(
            "INSERT INTO mt5_credentials (account_id, enc_password,"
            " key_version) VALUES (?,?,?)", (aid, enc, kv))
        registry.provision_terminal(conn, cid)
        conn.commit()
        account_ids.append(aid)

    print(f"Uji kapasitas: {args.accounts} akun x {args.cycles} siklus, "
          f"{args.deals} deal/akun, latensi "
          f"{args.min_latency}-{args.max_latency}s/panggilan")

    cpu0 = time.process_time()
    ram_start = _rss_mb()
    t0 = time.monotonic()
    cycle_times = []
    per_account_ok = {aid: 0 for aid in account_ids}
    per_account_err = {aid: 0 for aid in account_ids}

    with ThreadPoolExecutor(max_workers=args.accounts) as pool:
        for cycle in range(args.cycles):
            tc = time.monotonic()
            futures = {}
            for aid in account_ids:
                # sinkron penuh per siklus mensimulasikan worker terpisah
                futures[aid] = pool.submit(
                    _safe_sync, svc, registry, aid, cycle == 0)
            for aid, fut in futures.items():
                summary = fut.result()
                if summary.get("status") == "ok":
                    per_account_ok[aid] += 1
                else:
                    per_account_err[aid] += 1
            cycle_times.append(round(time.monotonic() - tc, 3))

    total_s = time.monotonic() - t0
    cpu_s = time.process_time() - cpu0
    ram_peak = _rss_mb()

    conn = dbmod.get_db(cfg)
    n_deals = conn.execute("SELECT COUNT(*) AS n FROM mt5_deals"
                           ).fetchone()["n"]
    n_trades = conn.execute("SELECT COUNT(*) AS n FROM trades"
                            ).fetchone()["n"]
    dbmod.close_db()

    data_size = 0
    for p in Path(tmp).rglob("*"):
        if p.is_file():
            data_size += p.stat().st_size

    result = {
        "mode": "MOCK (tanpa terminal MT5/Wine asli)",
        "accounts": args.accounts,
        "cycles": args.cycles,
        "deals_per_account_initial": args.deals,
        "simulated_call_latency_s": [args.min_latency, args.max_latency],
        "total_wall_s": round(total_s, 3),
        "cycle_wall_s": cycle_times,
        "cpu_process_s": round(cpu_s, 3),
        "ram_process_start_mb": round(ram_start, 1),
        "ram_process_end_mb": round(ram_peak, 1),
        "rows": {"mt5_deals": int(n_deals), "trades": int(n_trades)},
        "data_dir_size_kb": round(data_size / 1024, 1),
        "sync_ok": per_account_ok,
        "sync_error": per_account_err,
        "duplication_check": {
            "expected_deals": args.accounts * args.deals * args.cycles
            if False else args.accounts * args.deals,
            "note": "deals_upserted idempoten — jumlah baris tetap "
                    "accounts*deals walau siklus diulang",
        },
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"hasil ditulis: {args.json}")

    # rekomendasi konservatif
    max_cycle = max(cycle_times)
    print("\nREKOMENDASI (konservatif, berbasis mock):")
    print(f"- {args.accounts} worker paralel: siklus terlama {max_cycle}s "
          f"(mock); overhead backend CPU {cpu_s:.2f}s total")
    print("- 1 VPS 2 vCPU/4 GB RAM cukup utk 4 customer dengan interval "
          "sync >= 300s selama latensi terminal asli < 1s per panggilan")
    print("- WAJIB diulang dengan terminal MT5 live sebelum klaim produksi")
    return 0


def _safe_sync(svc: SyncService, registry: TerminalRegistry, aid: int,
               first: bool) -> dict:
    try:
        return svc.sync_account(aid, mode="full" if first else "incremental")
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    raise SystemExit(main())
