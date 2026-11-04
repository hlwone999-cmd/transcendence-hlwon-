"""Bukti RLS PostgreSQL nyata (audit #2) — dijalankan pada PostgreSQL
SUNGGUHAN (pgserver, embedded; bukan mock/simulasi).

Yang dibuktikan:
1. Migrasi 001+002 berjalan di PostgreSQL kosong.
2. Role aplikasi non-owner (transcendence_app) TANPA customer context
   tidak melihat baris apa pun pada tabel ber-RLS (default-deny).
3. Dengan context customer A: hanya data A; dengan context B: hanya B.
4. Customer A TIDAK BISA membaca/mengubah data B WALAU menjalankan
   query langsung melalui role aplikasi (tanpa lewat API backend).
5. Context tidak bocor antar transaksi (set_config is_local=true).
6. Owner database bypass RLS (semantik dokumentasi — bukan jalur app).

RLS adalah defense-in-depth (brief §5) — BUKAN pengganti authorization
backend; filter customer_id pada query tetap wajib (diuji test lain).
"""
import pytest

pgserver = pytest.importorskip(
    "pgserver", reason="pgserver tidak terpasang — bukti RLS live dilewati")

import db as dbmod  # noqa: E402

APP_ROLE = "transcendence_app"


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    """Server PostgreSQL embedded segar (1 modul test = 1 server)."""
    data_dir = tmp_path_factory.mktemp("pgdata-rls")
    server = pgserver.get_server(data_dir)
    yield server
    server.cleanup()


def _uri_as(server, user: str) -> str:
    """URI server dengan user diganti (auth trust di unix socket lokal)."""
    return server.get_uri().replace("postgres:@", f"{user}:@")


def _seed_owner_data(owner):
    """Seed 2 customer + akun + deals + credentials (sebagai owner)."""
    owner.execute("INSERT INTO customers (code, display_name)"
                  " VALUES ('C001','A'), ('C002','B')")
    id_a = int(owner.execute("SELECT id FROM customers WHERE code='C001'"
                             ).fetchone()["id"])
    id_b = int(owner.execute("SELECT id FROM customers WHERE code='C002'"
                             ).fetchone()["id"])
    owner.execute("INSERT INTO mt5_accounts (customer_id, login, server)"
                  " VALUES (?, 11111111, 'Exness-MT5Real')", (id_a,))
    acc_a = int(owner.execute("SELECT id FROM mt5_accounts WHERE customer_id=?",
                              (id_a,)).fetchone()["id"])
    owner.execute("INSERT INTO mt5_accounts (customer_id, login, server)"
                  " VALUES (?, 22222222, 'BrokerLain-MT5Demo')", (id_b,))
    acc_b = int(owner.execute("SELECT id FROM mt5_accounts WHERE customer_id=?",
                              (id_b,)).fetchone()["id"])
    for acc, n in ((acc_a, 2), (acc_b, 3)):
        for t in range(1, n + 1):
            owner.execute(
                """INSERT INTO mt5_deals (account_id, ticket, deal_type,
                     entry, time_utc, synced_at, profit)
                   VALUES (?,?,?,?,?,?,?)""",
                (acc, t, 1, 0, "2026-01-01T00:00:0%dZ" % t, "2026-01-02T00:00:00Z", 1.5 * t))
    owner.execute(
        """INSERT INTO mt5_credentials (account_id, enc_password, key_version)
           VALUES (?, 'enc-secret-A', 1), (?, 'enc-secret-B', 1)""",
        (acc_a, acc_b))
    owner.commit()
    return id_a, id_b, acc_a, acc_b


def test_rls_isolasi_antar_customer(pg):
    # -- 1. migrasi pada PostgreSQL kosong: 001 + 002 --------------------
    owner = dbmod.connect_postgres(pg.get_uri())
    applied = dbmod.migrate(owner)
    assert 1 in applied and 2 in applied

    id_a, id_b, acc_a, acc_b = _seed_owner_data(owner)

    # -- 2. role aplikasi non-owner: default-deny tanpa context ----------
    app = dbmod.connect_postgres(_uri_as(pg, APP_ROLE))
    n = int(app.execute("SELECT COUNT(*) AS n FROM mt5_deals").fetchone()["n"])
    assert n == 0, "tanpa context, role aplikasi harus melihat 0 baris"

    # -- 3. context A: hanya data A --------------------------------------
    app.set_customer_context(id_a)
    rows = app.execute("SELECT account_id FROM mt5_deals").fetchall()
    assert len(rows) == 2
    assert {int(r["account_id"]) for r in rows} == {acc_a}

    # kredensial customer B juga tersembunyi
    n_cred = int(app.execute(
        "SELECT COUNT(*) AS n FROM mt5_credentials").fetchone()["n"])
    assert n_cred == 1  # hanya milik A

    # -- 4a. A tidak bisa MEMBACA data B lewat query langsung ------------
    n_b = int(app.execute(
        "SELECT COUNT(*) AS n FROM mt5_deals WHERE account_id=?",
        (acc_b,)).fetchone()["n"])
    assert n_b == 0, "customer A TIDAK boleh membaca deals customer B"

    # -- 4b. A tidak bisa MENGUBAH data B (write isolation) --------------
    cur = app.execute(
        "UPDATE mt5_deals SET profit = 999.0 WHERE account_id=?",
        (acc_b,))
    assert cur.rowcount == 0, "UPDATE lintas customer harus 0 baris"
    app.commit()

    # -- 5. context tidak bocor antar transaksi (is_local semantics) -----
    assert app.current_customer_context() == id_a   # di-reapply otomatis
    app.commit()
    val = app.execute(
        "SELECT current_setting(%s) AS v", (dbmod.CUSTOMER_GUC,)
    ).fetchone()["v"]
    assert str(val) == str(id_a)
    # koneksi BERBEDA (request lain) tanpa context: tetap 0 baris
    app2 = dbmod.connect_postgres(_uri_as(pg, APP_ROLE))
    assert int(app2.execute(
        "SELECT COUNT(*) AS n FROM mt5_deals").fetchone()["n"]) == 0
    app2.close()

    # -- 6. owner bypass RLS (dokumentasi semantik, bukan jalur aplikasi) --
    n_owner = int(owner.execute(
        "SELECT COUNT(*) AS n FROM mt5_deals").fetchone()["n"])
    assert n_owner == 5, "owner melihat semua baris (RLS di-bypass)"

    # -- ganti context ke B pada koneksi yang sama: sekarang lihat B -----
    app.set_customer_context(id_b)
    rows_b = app.execute("SELECT account_id FROM mt5_deals").fetchall()
    assert {int(r["account_id"]) for r in rows_b} == {acc_b}
    assert len(rows_b) == 3
    app.close()
    owner.close()
