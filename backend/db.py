"""Akses database DUAL-ENGINE: PostgreSQL (produksi) & SQLite (dev/test).

Pemilihan engine (Tahap 3 brief multi-customer):
- config.db_url diawali postgres:// / postgresql://  ->  PostgreSQL (psycopg2)
- selain itu                                         ->  SQLite (path db_path)

Permukaan API TIDAK berubah bagi pemanggil lama:
- connect(db_file)            -> koneksi SQLite (kompatibel test lama)
- get_db(config) / close_db() -> koneksi per-thread (engine sesuai config)
- migrate(conn)               -> jalankan migrations/sqlite/*.sql atau
                                 migrations/postgres/*.sql sesuai engine,
                                 buku besar sama: tabel schema_migrations.
- conn.execute(sql, params)   -> objek cursor: fetchone/fetchall/iterable,
                                 baris bisa r["kolom"] dan dict(r).
- conn.commit(), "with conn:" -> transaksi.

PostgresConn menerjemahkan placeholder '?' -> '%s' (guard: '?' di dalam
string literal SQL tidak diizinkan — diuji test_static_sql_safety).

Nama tabel trades:
- SQLite  : trades      (skema lama, dipertahankan apa adanya)
- Postgres: mt5_trades  (nama wajib brief; lihat trades_table()).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_local = threading.local()

# GUC PostgreSQL yang dibaca policy RLS (migrations/postgres/001/002).
CUSTOMER_GUC = "app.customer_id"

# Kunci advisory lock PostgreSQL (xact-scoped) utk serialisasi cek-dan-
# tulis kapasitas. Konstanta bebas yang tidak dipakai modul lain.
CAPACITY_LOCK_REGISTER = 736451001   # registrasi customer (maks 4 aktif)
CAPACITY_LOCK_TERMINAL = 736451002   # alokasi port terminal RPyC


def is_postgres(conn) -> bool:
    return isinstance(conn, PostgresConn)


def _integrity_errors() -> tuple:
    """Kelas IntegrityError milik engine yang tersedia (lintas engine)."""
    errs = [sqlite3.IntegrityError]
    try:
        import psycopg2.errors  # noqa: PLC0415 - lazy
        errs.append(psycopg2.errors.IntegrityError)
    except ImportError:  # pragma: no cover - PG opsional di dev
        pass
    return tuple(errs)


INTEGRITY_ERRORS = _integrity_errors()


def trades_table(conn) -> str:
    """Nama tabel trades sesuai engine (PG: mt5_trades per brief §5)."""
    return "mt5_trades" if is_postgres(conn) else "trades"


def sync_state_table(conn) -> str:
    """SQLite: sync_state (skema lama). PG: mt5_sync_state (brief §5)."""
    return "mt5_sync_state" if is_postgres(conn) else "sync_state"


# ==================================================================== SQLite
def connect(db_file) -> sqlite3.Connection:
    db_file = Path(db_file)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# ================================================================ PostgreSQL
class PostgresConn:
    """Adapter psycopg2 yang meniru permukaan sqlite3 yang dipakai kode.

    - conn.execute(sql, params) -> cursor (fetchone/fetchall/iterable)
    - baris = RealDictRow: r["kolom"] dan dict(r) keduanya valid
    - '?'  -> '%s' otomatis
    """

    def __init__(self, dsn: str):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        self._psycopg2 = psycopg2
        self._RealDictCursor = RealDictCursor
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = False
        # Customer context utk RLS (audit #2). None = belum di-scope.
        # Diterapkan via set_config(..., is_local=true) sehingga otomatis
        # hilang di akhir transaksi; _reapply_context() memasangnya kembali
        # pada transaksi berikutnya selama koneksi masih di-scope customer
        # yang sama (koneksi per-thread dipakai lintas transaksi).
        self._customer_id: int | None = None

    # -- infrastruktur ------------------------------------------------
    @property
    def raw(self):
        return self._conn

    def cursor(self):
        return self._conn.cursor(cursor_factory=self._RealDictCursor)

    @staticmethod
    def to_pg(sql: str) -> str:
        # '?' placeholder -> '%s'. String literal SQL dengan '?' dilarang
        # (diuji test_static_sql_safety) supaya replace ini selalu aman.
        return sql.replace("?", "%s")

    # -- customer context utk RLS (audit #2) ---------------------------
    def set_customer_context(self, customer_id: int | None) -> None:
        """Pasang `app.customer_id` utk transaksi berjalan (RLS).

        Memakai set_config(..., is_local => true): nilai hanya berlaku
        sampai COMMIT/ROLLBACK lalu otomatis di-reset PostgreSQL — tidak
        bisa bocor ke transaksi/request lain yang memakai koneksi ini.
        Setelah commit/rollback, context dipasang ulang otomatis pada
        transaksi berikutnya (lihat _reapply_context).
        """
        if customer_id is None:
            self._customer_id = None
            return
        cur = self._conn.cursor()
        cur.execute("SELECT set_config(%s, %s, true)",
                    (CUSTOMER_GUC, str(int(customer_id))))
        cur.close()
        self._customer_id = int(customer_id)

    def _reapply_context(self) -> None:
        """Pasang ulang GUC customer pada transaksi yang baru terbuka."""
        if self._customer_id is None:
            return
        cur = self._conn.cursor()
        cur.execute("SELECT set_config(%s, %s, true)",
                    (CUSTOMER_GUC, str(self._customer_id)))
        cur.close()

    def current_customer_context(self) -> int | None:
        return self._customer_id

    @property
    def in_transaction(self) -> bool:
        """True bila transaksi PostgreSQL sedang berjalan."""
        import psycopg2.extensions as _ext  # noqa: PLC0415 - lazy
        return (not self._conn.closed and
                self._conn.info.transaction_status !=
                _ext.TRANSACTION_STATUS_IDLE)

    # -- kompatibilitas sqlite3 ----------------------------------------
    def execute(self, sql: str, params=None):
        cur = self.cursor()
        if params:
            cur.execute(self.to_pg(sql), list(params))
        else:
            cur.execute(self.to_pg(sql))
        return cur

    def commit(self) -> None:
        self._conn.commit()
        self._reapply_context()

    def rollback(self) -> None:
        self._conn.rollback()
        self._reapply_context()

    def executescript(self, script: str) -> None:
        """psycopg2 menerima multi-statement dalam satu execute."""
        cur = self._conn.cursor()
        cur.execute(script)
        cur.close()

    def close(self) -> None:
        self._conn.close()

    # -- context manager: commit/rollback (semantik sama dgn sqlite3) ---
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False


def connect_postgres(dsn: str) -> PostgresConn:
    return PostgresConn(dsn)


# ---------------------------------------------- customer context (RLS)
def set_customer_context(conn, customer_id: int | None) -> None:
    """Scope koneksi ke 1 customer utk RLS (no-op di SQLite).

    Dipanggil setelah customer context diturunkan dari token yang
    terverifikasi server (routes.require_customer) dan oleh worker
    sinkronisasi sebelum menyentuh baris milik akun tertentu.
    RLS adalah defense-in-depth (brief §5) — BUKAN pengganti filter
    customer_id di query backend.
    """
    if is_postgres(conn):
        conn.set_customer_context(customer_id)


@contextmanager
def capacity_guard(conn, key: int):
    """Serialisasi pola cek-dan-tulis (race condition, audit #3 lanjutan).

    - PostgreSQL : pg_advisory_xact_lock(key) — kunci tingkat transaksi,
      otomatis lepas saat COMMIT/ROLLBACK.
    - SQLite     : BEGIN IMMEDIATE — write-lock sejak awal transaksi,
      menutup celah TOCTOU antara SELECT COUNT dan INSERT.

    Bila body melempar exception, transaksi di-rollback agar kunci
    pasti lepas dan koneksi bersih untuk request berikutnya.

    Catatan: bila koneksi SUDAH berada di dalam transaksi (mis. penulisan
    audit sebelumnya belum di-commit), SQLite tidak bisa BEGIN lagi —
    guard lanjut tanpa lock eksplisit; kebenaran tetap dijaga UNIQUE
    constraint + retry di pemanggil. Pada PG advisory lock tetap diambil
    (re-entrant dalam transaksi yang sama).
    """
    if is_postgres(conn):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (int(key),))
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
    else:
        already_open = bool(getattr(conn, "in_transaction", False))
        if not already_open:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.rollback()
            raise


# ================================================================== common
def get_db(config):
    """Satu koneksi per thread per database (SQLite maupun PG)."""
    key = getattr(config, "db_url", "") or str(Path(config.db_file))
    conn = getattr(_local, "conn", None)
    conn_key = getattr(_local, "conn_key", None)
    if conn is None or conn_key != key:
        if conn is not None:
            conn.close()
        if getattr(config, "db_url", ""):
            conn = connect_postgres(config.db_url)
        else:
            conn = connect(config.db_file)
        _local.conn = conn
        _local.conn_key = key
    return conn


def close_db() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.conn_key = None


def _schema_migrations_ddl(conn) -> str:
    if is_postgres(conn):
        return ("CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY,"
                " applied_at TEXT NOT NULL DEFAULT"
                " to_char(now() AT TIME ZONE 'UTC',"
                " 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'))")
    return ("CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL"
            " DEFAULT (datetime('now')))")


def migrate(conn) -> list[int]:
    """Terapkan migrasi engine yang sesuai. Return daftar versi baru.

    - SQLite  : migrations/sqlite/*.sql   (001_init.sql = skema lama utuh)
    - Postgres: migrations/postgres/*.sql (001_multi_customer.sql)
    Aman dijalankan ulang; tiap file diterapkan maksimal sekali.
    """
    conn.execute(_schema_migrations_ddl(conn))
    conn.commit()
    applied = {r["version"] for r in conn.execute(
        "SELECT version FROM schema_migrations")}
    subdir = "postgres" if is_postgres(conn) else "sqlite"
    done: list[int] = []
    for sql_file in sorted((MIGRATIONS_DIR / subdir).glob("*.sql")):
        version = int(sql_file.name.split("_", 1)[0])
        if version in applied:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)",
                     (version,))
        conn.commit()
        done.append(version)
    return done
