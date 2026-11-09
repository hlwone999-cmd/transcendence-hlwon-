#!/usr/bin/env python3
"""BUKTI RLS PostgreSQL nyata — audit round-2 TEMUAN 3.

Menjalankan pada PostgreSQL SUNGGUHAN (pgserver, embedded — binary resmi
PostgreSQL, bukan mock) rangkaian bukti berikut dan mencetak transkrip:

  1. Migrasi 001+002 di DB kosong; role transcendence_app dibuat
     NON-OWNER: NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOBYPASSRLS.
  2. RLS ENABLE pada semua tabel ber-customer (pg_class.relrowsecurity).
  3. Role aplikasi TANPA context  : default-deny — 0 baris.
  4. Context customer A           : hanya data A (SELECT).
  5. A -> SELECT data B           : 0 baris.
  6. A -> UPDATE data B           : 0 baris.
  7. A -> INSERT ke akun B        : ditolak 42501 (row-level security).
  8. Kontrol A/B update data sendiri : BERHASIL (bukan blokir tulis).
  9. Context TIDAK bocor antar transaksi (level psql mentah:
     set_config is_local=true hilang setelah COMMIT/ROLLBACK;
     koneksi paralel tanpa context tetap 0 baris).
 10. Owner bypass RLS (semantik migrasi; bukan jalur aplikasi).

Exit code 0 = SEMUA bukti lulus; 1 = ada yang gagal.

Pemakaian:
    python3 backend/tools/rls_evidence.py [> laporan.txt]
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import db as dbmod  # noqa: E402

APP_ROLE = "transcendence_app"
GUC = "app.customer_id"

_hasil: list[tuple[bool, str]] = []


def cek(ok: bool, label: str, detail: str = "") -> None:
    _hasil.append((ok, label))
    print(f"  [{'LULUS' if ok else 'GAGAL'}] {label}"
          + (f" — {detail}" if detail else ""))


def judul(no: int, teks: str) -> None:
    print(f"\nBUKTI {no}: {teks}")
    print("-" * 72)


def main() -> int:
    import pgserver  # embedded PostgreSQL resmi
    import psycopg2

    print("=" * 72)
    print("BUKTI RLS POSTGRESQL — role non-owner/NOBYPASSRLS + isolasi"
          " antar customer")
    print("PostgreSQL : pgserver (embedded, binary resmi PostgreSQL)")
    print("=" * 72)

    data_dir = tempfile.mkdtemp(prefix="rls-evidence-")
    server = pgserver.get_server(data_dir)

    def uri_as(user: str) -> str:
        return server.get_uri().replace("postgres:@", f"{user}:@")

    try:
        # ---------------------------------------------------------- 1
        judul(1, "Migrasi 001+002 pada PostgreSQL kosong")
        owner = dbmod.connect_postgres(server.get_uri())
        applied = dbmod.migrate(owner)
        cek(applied == [1, 2], "migrasi 001_multi_customer + "
            "002_rls_roles diterapkan", f"applied={applied}")
        row = owner.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls,"
            " rolcanlogin FROM pg_roles WHERE rolname=?", (APP_ROLE,)
        ).fetchone()
        cek(row is not None and
            (row["rolsuper"], row["rolcreatedb"], row["rolcreaterole"],
             row["rolbypassrls"]) == (False, False, False, False)
            and row["rolcanlogin"] is True,
            f"role {APP_ROLE}: NON-OWNER + NOBYPASSRLS + LOGIN",
            f"rolsuper={row['rolsuper']} rolbypassrls={row['rolbypassrls']}")

        # ---------------------------------------------------------- 2
        judul(2, "RLS ENABLE pada tabel ber-customer")
        for t in ("mt5_deals", "mt5_orders", "mt5_trades", "mt5_symbols",
                  "mt5_sync_state", "mt5_credentials"):
            r = owner.execute(
                "SELECT relrowsecurity FROM pg_class WHERE relname=?",
                (t,)).fetchone()
            cek(r is not None and r["relrowsecurity"] is True,
                f"relrowsecurity {t}")

        # ------------------------------------------------- seed A & B
        owner.execute("INSERT INTO customers (code, display_name)"
                      " VALUES ('EVA','Customer A'), ('EVB','Customer B')")
        id_a = int(owner.execute(
            "SELECT id FROM customers WHERE code='EVA'").fetchone()["id"])
        id_b = int(owner.execute(
            "SELECT id FROM customers WHERE code='EVB'").fetchone()["id"])
        owner.execute("INSERT INTO mt5_accounts (customer_id, login, server)"
                      " VALUES (?, 80000001, 'Exness-MT5Real')", (id_a,))
        owner.execute("INSERT INTO mt5_accounts (customer_id, login, server)"
                      " VALUES (?, 80000002, 'BrokerLain-MT5Demo')", (id_b,))
        acc_a = int(owner.execute(
            "SELECT id FROM mt5_accounts WHERE customer_id=?",
            (id_a,)).fetchone()["id"])
        acc_b = int(owner.execute(
            "SELECT id FROM mt5_accounts WHERE customer_id=?",
            (id_b,)).fetchone()["id"])
        for acc in (acc_a, acc_b):
            owner.execute(
                """INSERT INTO mt5_deals (account_id, ticket, deal_type,
                     entry, time_utc, synced_at, profit)
                   VALUES (?,?,1,0,'2026-03-01T00:00:00Z',
                           '2026-03-02T00:00:00Z', 20.0)""", (acc, acc * 10))
        owner.commit()
        print(f"  seed: customer A id={id_a} (akun {acc_a}), "
              f"customer B id={id_b} (akun {acc_b}); 1 deal per akun")

        # ---------------------------------------------------------- 3
        judul(3, "Role aplikasi TANPA context: default-deny")
        app = dbmod.connect_postgres(uri_as(APP_ROLE))
        n = int(app.execute(
            "SELECT COUNT(*) AS n FROM mt5_deals").fetchone()["n"])
        cek(n == 0, "SELECT COUNT(*) mt5_deals tanpa context = 0", f"n={n}")

        # ---------------------------------------------------------- 4
        judul(4, "Context customer A: hanya data A")
        app.set_customer_context(id_a)
        rows = app.execute(
            "SELECT account_id FROM mt5_deals").fetchall()
        cek(len(rows) == 1 and int(rows[0]["account_id"]) == acc_a,
            "SELECT mt5_deals dgn context A -> 1 baris milik A",
            f"rows={ [int(r['account_id']) for r in rows] }")

        # ---------------------------------------------------------- 5
        judul(5, "A tidak dapat SELECT data B")
        n = int(app.execute(
            "SELECT COUNT(*) AS n FROM mt5_deals WHERE account_id=?",
            (acc_b,)).fetchone()["n"])
        cek(n == 0, f"SELECT deals akun B (id={acc_b}) = 0 baris", f"n={n}")

        # ---------------------------------------------------------- 6
        judul(6, "A tidak dapat UPDATE data B")
        cur = app.execute(
            "UPDATE mt5_deals SET profit = 999.0 WHERE account_id=?",
            (acc_b,))
        app.commit()
        cek(cur.rowcount == 0, "UPDATE deals akun B -> 0 baris terubah",
            f"rowcount={cur.rowcount}")

        # ---------------------------------------------------------- 7
        judul(7, "A tidak dapat INSERT ke akun B (policy 42501)")
        ditolak = False
        try:
            app.execute(
                """INSERT INTO mt5_deals (account_id, ticket, deal_type,
                     entry, time_utc, synced_at, profit)
                   VALUES (?,?,1,0,'2026-03-03T00:00:00Z',
                           '2026-03-03T00:00:00Z', 5.0)""", (acc_b, 999999))
            app.commit()
        except Exception as e:  # noqa: BLE001 — memang diharapkan gagal
            msg = str(getattr(e, "__cause__", None) or e)
            ditolak = ("42501" in msg or
                       "row-level security" in msg.lower())
            print(f"  pesan server: {msg.splitlines()[0][:100]}")
        app.rollback()
        cek(ditolak, "INSERT lintas customer ditolak policy RLS (42501)")

        # ---------------------------------------------------------- 8
        judul(8, "Kontrol: customer tetap bisa kelola data SENDIRI")
        cur = app.execute(
            "UPDATE mt5_deals SET profit = 21.0 WHERE account_id=?",
            (acc_a,))
        app.commit()
        cek(cur.rowcount == 1, "A UPDATE data A sendiri -> 1 baris",
            f"rowcount={cur.rowcount}")
        app.set_customer_context(id_b)
        cur = app.execute(
            "UPDATE mt5_deals SET profit = 22.0 WHERE account_id=?",
            (acc_b,))
        app.commit()
        cek(cur.rowcount == 1, "B UPDATE data B sendiri -> 1 baris",
            f"rowcount={cur.rowcount}")

        # ---------------------------------------------------------- 9
        judul(9, "Context TIDAK bocor antar transaksi (psycopg2 mentah)")
        raw = psycopg2.connect(uri_as(APP_ROLE))
        raw.autocommit = False
        cur = raw.cursor()
        cur.execute("BEGIN")
        cur.execute("SELECT set_config(%s, %s, true)", (GUC, str(id_a)))
        cur.execute("SELECT COUNT(*) FROM mt5_deals")
        n_open = cur.fetchone()[0]
        raw.rollback()
        cur.execute("SELECT current_setting(%s, true)", (GUC,))
        guc = cur.fetchone()[0]
        cek(guc in ("", None),
            "setelah ROLLBACK, GUC app.customer_id kosong kembali",
            f"nilai terbaca={guc!r}")
        cur.execute("SELECT COUNT(*) FROM mt5_deals")
        n_tanpa = cur.fetchone()[0]
        cek(n_open == 1 and n_tanpa == 0,
            "tx dgn context A lihat 1 baris; tx baru tanpa context = 0",
            f"n_open={n_open} n_baru={n_tanpa}")
        raw2 = psycopg2.connect(uri_as(APP_ROLE))
        cur2 = raw2.cursor()
        cur.execute("BEGIN")
        cur.execute("SELECT set_config(%s, %s, true)", (GUC, str(id_a)))
        cur.execute("SELECT COUNT(*) FROM mt5_deals")
        n_a = cur.fetchone()[0]
        cur2.execute("SELECT COUNT(*) FROM mt5_deals")
        n_paralel = cur2.fetchone()[0]
        cur2.execute("SELECT current_setting(%s, true)", (GUC,))
        guc2 = cur2.fetchone()[0]
        raw.rollback()
        raw2.rollback()
        raw.close()
        raw2.close()
        cek(n_a == 1 and n_paralel == 0 and guc2 in ("", None),
            "koneksi paralel tanpa context tidak mewarisi context A",
            f"n_A={n_a} n_paralel={n_paralel} guc_paralel={guc2!r}")

        # ---------------------------------------------------------- 10
        judul(10, "Owner bypass RLS (semantik migrasi, bukan jalur app)")
        n = int(owner.execute(
            "SELECT COUNT(*) AS n FROM mt5_deals").fetchone()["n"])
        cek(n == 2, "owner melihat seluruh 2 deal", f"n={n}")

        app.close()
        owner.close()
    finally:
        server.cleanup()

    gagal = [lbl for ok, lbl in _hasil if not ok]
    print("\n" + "=" * 72)
    print(f"RINGKASAN: {len(_hasil) - len(gagal)}/{len(_hasil)} bukti LULUS")
    if gagal:
        print("GAGAL:")
        for g in gagal:
            print(" -", g)
        return 1
    print("SEMUA BUKTI RLS LULUS pada PostgreSQL sungguhan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
