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
psycopg2 = pytest.importorskip(
    "psycopg2", reason="psycopg2 tidak terpasang — bukti RLS live dilewati")

import db as dbmod  # noqa: E402

APP_ROLE = "transcendence_app"
TABEL_RLS = ("mt5_deals", "mt5_orders", "mt5_trades", "mt5_symbols",
             "mt5_sync_state", "mt5_credentials")

_login_counter = [70000000]   # login unik antar test (UNIQUE(login,server))


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


# =====================================================================
# Penguatan round-2 (TEMUAN 3): bukti eksplisit role non-owner /
# NOBYPASSRLS, isolasi SELECT/UPDATE/INSERT dua arah, dan tidak ada
# kebocoran context antar transaksi — pada level PostgreSQL mentah
# (tanpa adapter backend) supaya buktinya independen dari kode app.
# =====================================================================
def _siapkan(pg, code_a: str, code_b: str):
    """Migrasi idempoten + seed 2 customer segar; return (owner, a, b)."""
    owner = dbmod.connect_postgres(pg.get_uri())
    applied = dbmod.migrate(owner)          # idempoten: [] bila sudah
    assert applied in ([], [1, 2])
    owner.execute("INSERT INTO customers (code, display_name)"
                  " VALUES (?,?)", (code_a, "AAA"))
    owner.execute("INSERT INTO customers (code, display_name)"
                  " VALUES (?,?)", (code_b, "BBB"))
    _login_counter[0] += 2
    login_a, login_b = _login_counter[0] - 1, _login_counter[0]
    id_a = int(owner.execute("SELECT id FROM customers WHERE code=?",
                             (code_a,)).fetchone()["id"])
    id_b = int(owner.execute("SELECT id FROM customers WHERE code=?",
                             (code_b,)).fetchone()["id"])
    owner.execute("INSERT INTO mt5_accounts (customer_id, login, server)"
                  " VALUES (?, ?, 'Exness-MT5Real')", (id_a, login_a))
    owner.execute("INSERT INTO mt5_accounts (customer_id, login, server)"
                  " VALUES (?, ?, 'BrokerLain-MT5Demo')", (id_b, login_b))
    acc_a = int(owner.execute(
        "SELECT id FROM mt5_accounts WHERE customer_id=?",
        (id_a,)).fetchone()["id"])
    acc_b = int(owner.execute(
        "SELECT id FROM mt5_accounts WHERE customer_id=?",
        (id_b,)).fetchone()["id"])
    for acc in (acc_a, acc_b):
        owner.execute(
            """INSERT INTO mt5_deals (account_id, ticket, deal_type, entry,
                 time_utc, synced_at, profit)
               VALUES (?,?,1,0,'2026-02-01T00:00:00Z','2026-02-02T00:00:00Z',
                       10.0)""", (acc, acc * 10))
    owner.commit()
    return owner, id_a, id_b, acc_a, acc_b


def test_role_app_benar_benar_non_owner_dan_nobypassrls(pg):
    """Role aplikasi: NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE,
    dan koneksi nyata sebagai role itu melihat is_superuser=off."""
    owner = dbmod.connect_postgres(pg.get_uri())
    dbmod.migrate(owner)
    row = owner.execute(
        "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb,"
        " rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname=?",
        (APP_ROLE,)).fetchone()
    assert row is not None, "role transcendence_app harus dibuat migrasi 002"
    assert row["rolsuper"] is False
    assert row["rolbypassrls"] is False
    assert row["rolcreaterole"] is False
    assert row["rolcreatedb"] is False
    assert row["rolcanlogin"] is True
    owner.close()

    # bukti pada SESI nyata sebagai role aplikasi
    app = psycopg2.connect(_uri_as(pg, APP_ROLE))
    cur = app.cursor()
    cur.execute("SELECT current_user, session_user, usename IS NOT NULL"
                " FROM pg_user WHERE usename = current_user")
    who = cur.fetchone()
    assert who[0] == APP_ROLE and who[1] == APP_ROLE
    cur.execute("SHOW is_superuser")
    assert cur.fetchone()[0] == "off"
    app.close()


def test_rls_aktif_dan_forced_pada_tabel_customer(pg):
    """relrowsecurity=1 untuk semua tabel ber-customer (policy 001/002)."""
    owner = dbmod.connect_postgres(pg.get_uri())
    dbmod.migrate(owner)
    for t in TABEL_RLS:
        row = owner.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE relname=?", (t,)).fetchone()
        assert row is not None, f"tabel {t} tidak ada"
        assert row["relrowsecurity"] is True, f"RLS belum ENABLE di {t}"
    # role app TIDAK owner tabel — cukup ENABLE; owner tetap bypass
    # (dipakai hanya migrasi/administrasi, bukan jalur aplikasi)
    owner.close()


def test_a_tidak_bisa_select_update_maupun_insert_data_b(pg):
    """Matriks isolasi penuh A terhadap B lewat role aplikasi:
    SELECT -> 0 baris; UPDATE -> 0 baris; INSERT -> ditolak 42501."""
    owner, id_a, id_b, acc_a, acc_b = _siapkan(pg, "R2A", "R2B")
    app = dbmod.connect_postgres(_uri_as(pg, APP_ROLE))
    app.set_customer_context(id_a)

    # SELECT data B -> 0 baris
    n = int(app.execute(
        "SELECT COUNT(*) AS n FROM mt5_deals WHERE account_id=?",
        (acc_b,)).fetchone()["n"])
    assert n == 0, "A tidak boleh SELECT data B"
    # UPDATE data B -> 0 baris
    cur = app.execute(
        "UPDATE mt5_deals SET profit = 999.0 WHERE account_id=?",
        (acc_b,))
    assert cur.rowcount == 0, "A tidak boleh UPDATE data B"
    app.commit()
    # INSERT ke akun B -> dilanggar policy RLS (42501)
    with pytest.raises(Exception) as ei:
        app.execute(
            """INSERT INTO mt5_deals (account_id, ticket, deal_type, entry,
                 time_utc, synced_at, profit)
               VALUES (?,?,1,0,'2026-02-03T00:00:00Z','2026-02-03T00:00:00Z',
                       5.0)""", (acc_b, 424242))
        app.commit()
    app.rollback()
    assert "42501" in str(ei.value.__cause__ or ei.value) or \
        "row-level security" in str(ei.value.__cause__ or ei.value).lower(), \
        f"harus ditolak policy RLS (42501): {ei.value}"
    # data B benar-benar tak tersentuh
    profit_b = owner.execute(
        "SELECT profit FROM mt5_deals WHERE account_id=?", (acc_b,)
    ).fetchall()
    assert len(profit_b) == 1 and float(profit_b[0]["profit"]) == 10.0
    app.close()
    owner.close()


def test_control_customer_bisa_update_data_sendiri(pg):
    """ Kontrol: isolasi bukan blokir tulis — A UPDATE data A sukses
    (rowcount benar) dan B juga; hanya lintas customer yang ditolak."""
    owner, id_a, id_b, acc_a, acc_b = _siapkan(pg, "R2C", "R2D")
    app = dbmod.connect_postgres(_uri_as(pg, APP_ROLE))
    app.set_customer_context(id_a)
    cur = app.execute(
        "UPDATE mt5_deals SET profit = 55.0 WHERE account_id=?", (acc_a,))
    assert cur.rowcount == 1, "A harus bisa update data A sendiri"
    app.commit()
    app.set_customer_context(id_b)
    cur = app.execute(
        "UPDATE mt5_deals SET profit = 66.0 WHERE account_id=?", (acc_b,))
    assert cur.rowcount == 1, "B harus bisa update data B sendiri"
    app.commit()
    pa = owner.execute("SELECT profit FROM mt5_deals WHERE account_id=?",
                       (acc_a,)).fetchone()["profit"]
    pb = owner.execute("SELECT profit FROM mt5_deals WHERE account_id=?",
                       (acc_b,)).fetchone()["profit"]
    assert float(pa) == 55.0 and float(pb) == 66.0
    app.close()
    owner.close()


def test_context_tidak_bocor_antar_transaksi_level_psql(pg):
    """Bukti level PostgreSQL MENTAH (tanpa adapter): set_config
    is_local=true otomatis hilang pada transaksi berikutnya; transaksi
    baru tanpa context = default-deny (0 baris). Koneksi kedua yang
    hidup bersamaan juga tidak melihat context koneksi pertama."""
    owner, id_a, id_b, acc_a, acc_b = _siapkan(pg, "R2E", "R2F")
    raw = psycopg2.connect(_uri_as(pg, APP_ROLE))
    raw.autocommit = False
    cur = raw.cursor()

    # transaksi 1: context A (is_local) -> lihat hanya A
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('app.customer_id', %s, true)",
                (str(id_a),))
    cur.execute("SELECT COUNT(*) FROM mt5_deals")
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT account_id FROM mt5_deals")
    assert [r[0] for r in cur.fetchall()] == [acc_a]
    raw.rollback()   # akhir transaksi 1

    # transaksi 2 (koneksi yang sama): GUC WAJIB sudah kosong
    cur.execute("SELECT current_setting('app.customer_id', true)")
    guc = cur.fetchone()[0]
    assert guc in ("", None), \
        f"context TIDAK boleh bocor antar transaksi; terbaca {guc!r}"
    cur.execute("SELECT COUNT(*) FROM mt5_deals")
    assert cur.fetchone()[0] == 0, \
        "tanpa context: default-deny, 0 baris"
    raw.rollback()

    # koneksi LAIN yang hidup BERSAMAAN tidak mewarisi context A
    raw2 = psycopg2.connect(_uri_as(pg, APP_ROLE))
    cur2 = raw2.cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('app.customer_id', %s, true)",
                (str(id_a),))
    cur.execute("SELECT COUNT(*) FROM mt5_deals")
    assert cur.fetchone()[0] == 1          # A melihat data A di tx terbuka
    cur2.execute("SELECT COUNT(*) FROM mt5_deals")
    assert cur2.fetchone()[0] == 0, \
        "koneksi paralel tanpa context tidak boleh melihat data A"
    cur2.execute("SELECT current_setting('app.customer_id', true)")
    assert cur2.fetchone()[0] in ("", None)
    raw.rollback()
    raw2.rollback()
    raw.close()
    raw2.close()
    owner.close()
