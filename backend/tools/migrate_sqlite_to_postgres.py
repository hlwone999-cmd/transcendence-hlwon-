"""Tool migrasi data: SQLite lama -> PostgreSQL multi-customer.

Jalur migrasi teruji (brief §5):
- Skema PG disiapkan otomatis (db.migrate) bila belum ada.
- Semua akun lama dimiliki customer "legacy" baru (skema lama tanpa
  customer). Password MT5 lama hidup di env backend lama — kirim lewat
  --legacy-password agar ikut terenkripsi; tanpa itu akun diimpor tanpa
  kredensial (admin bind ulang manual).
- ID baris dipertahankan; sequence disetel ke max(id).
- Idempoten-guard: menolak mengimpor ke PG yang sudah berisi akun
  (pakai --force bila memang dimaksudkan).
- Dry-run default; --apply untuk benar-benar menulis.

Pemakaian:
  python tools/migrate_sqlite_to_postgres.py --sqlite data/journal.db \
      --pg postgresql://user:pass@localhost/transcendence \
      [--legacy-password SECRET] [--apply]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import db as dbmod  # noqa: E402

TABLES_COPY = [  # (tabel sqlite, tabel pg, kolom)
    ("mt5_accounts", "mt5_accounts",
     "id,customer_id,login,server,company,currency,leverage,balance,"
     "equity,margin_free,first_sync_at,last_sync_at"),
    ("mt5_deals", "mt5_deals",
     "id,account_id,ticket,order_ticket,position_id,symbol,deal_type,"
     "type_name,entry,entry_name,volume,price,profit,commission,swap,fee,"
     "comment,magic,reason,time_utc,synced_at"),
    ("mt5_orders", "mt5_orders",
     "id,account_id,ticket,position_id,symbol,order_type,type_name,"
     "volume_initial,volume_current,price_open,price_sl,price_tp,state,"
     "state_name,time_setup_utc,time_done_utc,comment,magic,synced_at"),
    ("trades", "mt5_trades",
     "id,account_id,position_id,symbol,direction,status,volume_in,"
     "volume_out,entry_price,exit_price,open_time_utc,close_time_utc,"
     "profit,commission,swap,fee,net_profit,sl_initial,tp_initial,"
     "sl_current,tp_current,price_current,exit_reason,magic,comment,"
     "entry_deals,exit_deals,updated_at"),
    ("mt5_symbols", "mt5_symbols",
     "account_id,symbol,point,digits,trade_tick_size,trade_tick_value,"
     "pip_size,updated_at"),
    ("sync_state", "mt5_sync_state",
     "account_id,initial_done,last_deal_time_utc,last_sync_started_at,"
     "last_sync_finished_at,last_sync_status"),
    ("sync_logs", "sync_logs",
     "id,account_id,mode,started_at,finished_at,status,deals_upserted,"
     "orders_upserted,trades_updated,open_positions,range_from_utc,"
     "range_to_utc,error"),
]

LEGACY_CODE = "C-LEGACY"
LEGACY_EMAIL = "legacy@transcendence.local"


def _legacy_customer(conn) -> int:
    row = conn.execute("SELECT id FROM customers WHERE code=?",
                       (LEGACY_CODE,)).fetchone()
    if row is not None:
        return int(row["id"])
    conn.execute("INSERT INTO customers (code, display_name) VALUES (?,?)",
                 (LEGACY_CODE, "Data lama (hasil migrasi SQLite)"))
    cid = int(conn.execute("SELECT id FROM customers WHERE code=?",
                           (LEGACY_CODE,)).fetchone()["id"])
    # user legacy tanpa login aktif (password acak tak-diketahui)
    from werkzeug.security import generate_password_hash
    import secrets
    conn.execute(
        "INSERT INTO users (customer_id, email, password_hash) VALUES (?,?,?)",
        (cid, LEGACY_EMAIL, generate_password_hash(secrets.token_urlsafe(32))))
    conn.commit()
    return cid


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--pg", required=True)
    ap.add_argument("--legacy-password", default=None,
                    help="Password MT5 lama (dari env backend lama) utk "
                         "dienkripsi ke semua akun terimpor")
    ap.add_argument("--apply", action="store_true",
                    help="Tulis ke PostgreSQL (tanpa ini = dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="Lanjut walau PG sudah berisi akun")
    args = ap.parse_args()

    sq = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    sq.row_factory = sqlite3.Row

    pg = dbmod.connect_postgres(args.pg)
    dbmod.migrate(pg)

    existing = pg.execute("SELECT COUNT(*) AS n FROM mt5_accounts"
                          ).fetchone()["n"]
    if int(existing) > 0 and not args.force:
        print(f"PG sudah berisi {existing} akun. "
              "Gunakan --force bila memang dimaksudkan.")
        return 1

    counts_src = {t: sq.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t, _, _ in TABLES_COPY if t != "mt5_symbols"}
    counts_src["mt5_symbols"] = sq.execute(
        "SELECT COUNT(*) FROM mt5_symbols").fetchone()[0]
    print("Sumber (SQLite):", counts_src)

    if not args.apply:
        print("DRY-RUN — tidak ada tulisan. Tambahkan --apply untuk migrasi.")
        return 0

    cust_id = _legacy_customer(pg)
    enc = None
    key_version = 1
    if args.legacy_password:
        import os
        from config import Config
        from security import crypto
        tmp_cfg = Config()
        # kunci HANYA dari env proses ini — tidak lewat argumen CLI
        tmp_cfg.cred_key = os.getenv("MT5_CRED_KEY", "")
        if not tmp_cfg.cred_key:
            print("ERROR: --legacy-password butuh env MT5_CRED_KEY.")
            return 2
        enc, key_version = crypto.encrypt_password(tmp_cfg,
                                                   args.legacy_password)

    total = {}
    with pg:  # satu transaksi besar — gagal di tengah = rollback semua
        for src, dst, cols in TABLES_COPY:
            # deteksi kolom yang benar-benar ada di SQLite lama —
            # skema 001 tidak punya mt5_accounts.customer_id
            have = {r[1] for r in sq.execute(f"PRAGMA table_info({src})")}
            use = [c for c in cols.split(",") if c in have]
            # akun legacy tidak punya kolom customer_id -> isi customer
            # legacy secara eksplisit (PG: NOT NULL)
            extra_cid = (src == "mt5_accounts"
                         and "customer_id" not in have)
            if extra_cid:
                use = use + ["customer_id"]
            rows = sq.execute(f"SELECT {','.join(use if not extra_cid else use[:-1])} FROM {src}").fetchall()
            n = 0
            for row in rows:
                vals = list(row)
                if extra_cid:
                    vals.append(cust_id)
                elif src == "mt5_accounts" and "customer_id" in use:
                    i = use.index("customer_id")
                    if vals[i] is None:
                        vals[i] = cust_id
                ph = ",".join("?" * len(use))
                pg.execute(f"INSERT INTO {dst} ({','.join(use)}) VALUES ({ph})",
                           vals)
                n += 1
            total[dst] = n
            # setel sequence ke max(id) bila tabel punya kolom id serial
            if "id" in use:
                pg.execute(
                    f"SELECT setval(pg_get_serial_sequence('{dst}','id'),"
                    " COALESCE((SELECT MAX(id) FROM " + dst + "), 1))")
        if enc is not None:
            for aid in pg.execute("SELECT id FROM mt5_accounts").fetchall():
                pg.execute(
                    "INSERT INTO mt5_credentials (account_id, enc_password,"
                    " key_version) VALUES (?,?,?)"
                    " ON CONFLICT (account_id) DO NOTHING",
                    (aid["id"], enc, key_version))
    print("Tercopy:", total)
    # verifikasi
    bad = []
    for src, dst, _ in TABLES_COPY:
        n_dst = pg.execute(f"SELECT COUNT(*) AS n FROM {dst}").fetchone()["n"]
        n_src = counts_src[src]
        if int(n_dst) < int(n_src):
            bad.append((src, n_src, n_dst))
    if bad:
        print("GAGAL verifikasi:", bad)
        return 1
    print("OK — verifikasi jumlah baris lolos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
