"""Akses SQLite (WAL) + runner migrasi sederhana.

Migrasi = file .sql bernomor di backend/migrations, dijalankan sekali
sesuai urutan, dicatat di tabel schema_migrations. Aman dijalankan ulang.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_local = threading.local()


def connect(db_file) -> sqlite3.Connection:
    db_file = Path(db_file)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_db(config) -> sqlite3.Connection:
    """Satu koneksi per thread per path database."""
    path = str(Path(config.db_file))
    conn = getattr(_local, "conn", None)
    conn_path = getattr(_local, "conn_path", None)
    if conn is None or conn_path != path:
        if conn is not None:
            conn.close()
        conn = connect(path)
        _local.conn = conn
        _local.conn_path = path
    return conn


def close_db() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.conn_path = None


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Terapkan migrasi yang belum jalan. Return daftar versi yang diterapkan."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    done: list[int] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(sql_file.name.split("_", 1)[0])
        if version in applied:
            continue
        with conn:
            conn.executescript(sql_file.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        done.append(version)
    return done
