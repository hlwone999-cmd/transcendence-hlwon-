"""Uji race condition (audit #4) — registrasi kapasitas maks 4 dan
alokasi port terminal RPyC, dengan thread benar-benar bersamaan.

Kapabilitas yang diuji (SQLite nyata; pola lock identik di PostgreSQL
via pg_advisory_xact_lock — diuji terpisah oleh test_rls_postgres utk
RLS dan tetap satu kode capacity_guard di db.py).
"""
import sqlite3
import threading

import db as dbmod
import auth as authmod
from config import Config
from cryptography.fernet import Fernet
from services.terminals import TerminalRegistry


def _mk_cfg(tmp_path, max_customers=4) -> Config:
    c = Config()
    # registrasi portal diaktifkan eksplisit (default produksi juga aktif)
    c.public_register_enabled = True
    c.db_path = str(tmp_path / "race.db")
    c.cred_key = Fernet.generate_key().decode("ascii")
    c.term_base_dir = str(tmp_path / "terminals")
    c.terminal_provision_mode = "mock"
    c.max_customers = max_customers
    return c


def _new_conn(cfg):
    return dbmod.connect(cfg.db_file)


# ---------------------------------------------------------- registrasi
def test_registrasi_bersamaan_slot_terakhir_tidak_dobel(tmp_path):
    """8 thread mendaftar bersamaan saat slot aktif tinggal 1 dari 4:
    TEPAT 1 yang berhasil; jumlah customer aktif final == 4 (maks).

    Sejak keputusan produk 2026-11 batas registrasi adalah
    max_registered_customers (opsional) — guard serialisasi yang sama."""
    cfg = _mk_cfg(tmp_path)
    cfg.max_registered_customers = 4
    seed = _new_conn(cfg)
    dbmod.migrate(seed)
    # isi 3 customer dulu secara berurutan
    for i in range(3):
        authmod.register_customer(seed, cfg, f"seed{i}@test.id",
                                  "rahasia123")
        seed.commit()
    n_active = int(seed.execute(
        "SELECT COUNT(*) AS n FROM customers WHERE status='active'"
    ).fetchone()["n"])
    assert n_active == 3

    barrier = threading.Barrier(8)
    results = {"ok": [], "kapasitas": 0, "bentrok": 0, "error": []}
    mtx = threading.Lock()

    def worker(i):
        conn = _new_conn(cfg)          # koneksi per thread (wajib SQLite)
        try:
            barrier.wait()
            created = authmod.register_customer(
                conn, cfg, f"race{i}@test.id", "rahasia123")
            conn.commit()              # commit eksplisit pemanggil guard
            with mtx:
                results["ok"].append(created["code"])
        except authmod.AuthError as e:
            with mtx:
                if e.code == 403:
                    results["kapasitas"] += 1
                elif e.code == 409:
                    results["bentrok"] += 1
                else:
                    results["error"].append(f"AuthError {e.code}: {e.message}")
        except Exception as e:  # noqa: BLE001
            with mtx:
                results["error"].append(f"{type(e).__name__}: {e}")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results["error"] == [], f"tidak boleh ada error 5xx: {results}"
    assert len(results["ok"]) == 1, \
        f"slot terakhir hanya boleh dimenangkan 1 thread: {results}"
    assert results["kapasitas"] == 7
    final = int(seed.execute(
        "SELECT COUNT(*) AS n FROM customers WHERE status='active'"
    ).fetchone()["n"])
    assert final == 4, "kapasitas maks 4 customer aktif (brief §2)"


def test_registrasi_email_sama_bersamaan_satu_sukses(tmp_path):
    """Dua thread dengan email SAMA bersamaan: tepat 1 sukses,
    lainnya 409 (bukan 500, bukan dobel)."""
    cfg = _mk_cfg(tmp_path)
    seed = _new_conn(cfg)
    dbmod.migrate(seed)
    barrier = threading.Barrier(2)
    outcomes = {"ok": 0, "konflik": 0, "error": []}
    mtx = threading.Lock()

    def worker():
        conn = _new_conn(cfg)
        try:
            barrier.wait()
            authmod.register_customer(conn, cfg, "sama@test.id",
                                      "rahasia123")
            conn.commit()
            with mtx:
                outcomes["ok"] += 1
        except authmod.AuthError as e:
            with mtx:
                if e.code == 409:
                    outcomes["konflik"] += 1
                else:
                    outcomes["error"].append(f"AuthError {e.code}")
        except Exception as e:  # noqa: BLE001
            with mtx:
                outcomes["error"].append(f"{type(e).__name__}: {e}")
        finally:
            conn.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert outcomes["error"] == []
    assert outcomes["ok"] == 1 and outcomes["konflik"] == 1, outcomes
    n = int(seed.execute(
        "SELECT COUNT(*) AS n FROM users WHERE email='sama@test.id'"
    ).fetchone()["n"])
    assert n == 1


# ------------------------------------------------ alokasi port terminal
def test_alokasi_port_terminal_bersamaan_unik_per_customer(tmp_path):
    """4 customer provisioning BERSAMAAN: semua dapat port berbeda dari
    jendela 18812..18815; tidak ada dobel, tidak ada 500."""
    cfg = _mk_cfg(tmp_path)
    seed = _new_conn(cfg)
    dbmod.migrate(seed)
    cids = []
    for i in range(4):
        created = authmod.register_customer(seed, cfg, f"p{i}@test.id",
                                            "rahasia123")
        seed.commit()
        cids.append(created["customer_id"])

    reg = TerminalRegistry(cfg)
    barrier = threading.Barrier(4)
    results = {}
    mtx = threading.Lock()

    def worker(cid):
        conn = _new_conn(cfg)
        try:
            barrier.wait()
            info = reg.provision_terminal(conn, cid)
            conn.commit()            # INSERT terminal ikut tx pemanggil
            with mtx:
                results[cid] = info
        except Exception as e:  # noqa: BLE001
            with mtx:
                results[cid] = {"error": f"{type(e).__name__}: {e}"}
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(c,)) for c in cids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all("error" not in results[c] for c in cids), results
    ports = sorted(results[c]["rpyc_port"] for c in cids)
    assert ports == [18812, 18813, 18814, 18815], \
        f"port harus unik & sesuai jendela brief §4: {ports}"
    rows = seed.execute("SELECT customer_id, rpyc_port FROM mt5_terminals"
                        ).fetchall()
    assert len(rows) == 4
    assert len({int(r["rpyc_port"]) for r in rows}) == 4


def test_provisi_customer_sama_dua_kali_idempoten(tmp_path):
    """Provision ulang customer yang SUDAH punya terminal: idempoten
    (remediasi S5/T4) — terminal yang sama dikembalikan dengan
    created=False, tanpa baris/port baru, tanpa transaksi menggantung."""
    cfg = _mk_cfg(tmp_path)
    conn = _new_conn(cfg)
    dbmod.migrate(conn)
    created = authmod.register_customer(conn, cfg, "dua@test.id",
                                        "rahasia123")
    conn.commit()
    reg = TerminalRegistry(cfg)
    info = reg.provision_terminal(conn, created["customer_id"])
    conn.commit()
    assert info["rpyc_port"] == 18812 and info["created"] is True
    again = reg.provision_terminal(conn, created["customer_id"])
    assert again["created"] is False
    assert again["rpyc_port"] == 18812
    assert again["terminal_id"] == info["terminal_id"]
    # kegagalan tidak meninggalkan transaksi menggantung
    assert not conn.in_transaction if hasattr(conn, "in_transaction") else True
    rows = conn.execute("SELECT COUNT(*) AS n FROM mt5_terminals"
                        ).fetchone()["n"]
    assert int(rows) == 1
