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
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_local = threading.local()


def is_postgres(conn) -> bool:
    return isinstance(conn, PostgresConn)


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

    def rollback(self) -> None:
        self._conn.rollback()

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
