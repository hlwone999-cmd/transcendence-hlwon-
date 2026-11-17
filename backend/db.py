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
# B12.1: role PostgreSQL untuk operasi SISTEM (migrasi 008). Scope sistem
# BUKAN lagi GUC (app.scope dapat dipalsukan lewat set_config oleh role
# aplikasi); scope sistem = koneksi TERPISAH yang login sebagai role ini.
APP_ROLE = "transcendence_app"
SYSTEM_ROLE = "transcendence_system"


class SystemDbConfigError(RuntimeError):
    """Koneksi sistem tidak dikonfigurasi / login sebagai role aplikasi."""

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

    def __init__(self, dsn: str, role_kind: str = "app"):
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
        # B12.1: "app" (role request/customer) atau "system" (role sistem).
        self.role_kind = role_kind

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

        AUDIT ROUND-4 #1 — customer_id None = lepas scope TANPA rollback
        (soft-clear): GUC transaksi berjalan dikosongkan (set_config '',
        is_local=true — hilang saat commit/rollback) dan memori di-reset
        agar reapply tidak menghidupkannya. Aman dipakai di tengah
        transaksi pemanggil (beda dgn clear_customer_context yang
        me-rollback) — dipakai untuk MEMULIHKAN scope lama pemanggil
        setelah blok scoped (lihat TerminalRegistry._account_config).
        """
        if customer_id is None:
            try:
                self._apply("")
            except Exception:  # noqa: BLE001 - koneksi mungkin sudah mati
                pass
            self._customer_id = None
            return
        if isinstance(customer_id, bool) or not isinstance(customer_id, int):
            customer_id = int(customer_id)     # ValueError/TypeError: tolak
        self._apply(str(int(customer_id)))
        self._customer_id = int(customer_id)

    def _apply(self, customer: str) -> None:
        cur = self._conn.cursor()
        cur.execute("SELECT set_config(%s, %s, true)",
                    (CUSTOMER_GUC, customer))
        cur.close()

    def _reapply_context(self) -> None:
        """Pasang ulang GUC customer pada transaksi yang baru terbuka."""
        if self._customer_id is None:
            return
        self._apply(str(self._customer_id))

    def current_customer_context(self) -> int | None:
        return self._customer_id

    def clear_customer_context(self) -> None:
        """Bersihkan scope customer di sisi Python DAN PostgreSQL sekaligus.

        Audit round-3 (tambahan): setelah request selesai, koneksi
        thread-local TIDAK BOLEH menyimpan customer scope aktif — kalau
        tidak, request berikutnya yang tidak berautentikasi (health,
        register/login, 401) mewarisi scope customer sebelumnya begitu
        transaksi berikutnya di-commit (reapply GUC lama, test
        test_rls_no_leak_requests membuktikan celah ini).

        Langkah (urutan penting):
        1. Hentikan transaksi yang masih menggantung: rollback().
           - Request yang SUDAH sukses pasti sudah commit() sebelum
             sini — rollback di titik itu no-op dan TIDAK membatalkan
             data yang sudah tersimpan (audit: cleanup tidak boleh
             membatalkan transaksi yang seharusnya sudah berhasil).
           - Bila request gagal dan transaksi masih terbuka, rollback
             MENUTUP transaksi itu — justru itu pembersihannya (tidak
             ada transaksi terbuka yang bocor ke request berikutnya).
        2. Reset GUC di level SESSION (is_local=false): menjamin GUC
           kosong walau ada jalur yang memasangnya non-local.
        3. Hapus memori `_customer_id` agar reapply transaksi berikutnya
           tidak memasang ulang context lama.
        """
        try:
            if self.in_transaction:
                self._conn.rollback()
        except Exception:  # noqa: BLE001 - koneksi mungkin sudah mati
            pass
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT set_config(%s, '', false)",
                        (CUSTOMER_GUC,))
            cur.close()
            self._conn.commit()
        except Exception:  # noqa: BLE001 - pembersihan tak boleh meledak
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        self._customer_id = None

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


def current_scope(conn):
    """int customer / None (SQLite: selalu None)."""
    return conn.current_customer_context() if is_postgres(conn) else None


@contextmanager
def system_conn(config):
    """B12.1: koneksi SISTEM (role transcendence_system) tanpa customer
    context untuk operasi yang MEMANG lintas customer. Scope koneksi sistem
    sebelum blok dipulihkan di finally (juga saat blok melempar). Pemulihan
    gagal -> koneksi sistem ditutup & dilepas dari slot thread-local.
    SQLite: koneksi biasa (tanpa RLS)."""
    sc = get_system_db(config)
    if not is_postgres(sc):
        yield sc
        return
    prev = sc.current_customer_context()
    sc.set_customer_context(None)
    try:
        yield sc
    finally:
        try:
            sc.set_customer_context(prev)
        except Exception:
            try:
                sc.rollback()
                sc.set_customer_context(prev)
            except Exception:
                _drop_system_slot(sc)
                raise


def clear_customer_context(conn) -> None:
    """Lepas scope customer dari koneksi (no-op di SQLite).

    Audit round-3: dipanggil teardown Flask tiap selesai request dan
    finally worker sync — koneksi thread-local kembali ke kondisi:
    tanpa transaksi terbuka + GUC app.customer_id kosong + memori
    scope Python kosong. Request berikutnya pada thread yang sama
    PASTI mulai tanpa scope (tidak mewarisi customer sebelumnya).

    Tidak membatalkan transaksi yang sudah berhasil: request sukses
    sudah commit sebelum cleanup; rollback hanya menutup transaksi
    yang tersisa (no-op pada koneksi idle).
    """
    if is_postgres(conn):
        conn.clear_customer_context()


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
            conn.config = config          # B12.1: penulis audit (audit.py)
        else:
            conn = connect(config.db_file)
        _local.conn = conn
        _local.conn_key = key
    return conn


def _no_password(parts) -> bool:
    return not parts.password            # None ('user@') atau '' ('user:@')


def system_dsn(config) -> str:
    """DSN role SISTEM (B12.2, fail-closed).

    - DATABASE_SYSTEM_URL eksplisit -> dipakai (identitasnya tetap
      diverifikasi ketat oleh _open_system); DSN yang SAMA dengan DSN
      aplikasi ditolak.
    - Kosong + DSN aplikasi BERPASSWORD -> DITOLAK (wajib eksplisit).
    - Kosong + DSN lokal TANPA password dengan user transcendence_app
      (trust/peer lokal) -> diturunkan: user diganti transcendence_system.
    - Selain itu (mis. DSN owner/superuser) -> DITOLAK. TIDAK ADA fallback
      ke DSN aplikasi atau owner."""
    from urllib.parse import urlsplit, urlunsplit
    url = getattr(config, "db_url", "") or ""
    explicit = (getattr(config, "db_system_url", "") or "").strip()
    if explicit:
        if explicit == url:
            raise SystemDbConfigError(
                "DATABASE_SYSTEM_URL sama dengan DATABASE_URL — role sistem "
                "wajib memakai login terpisah")
        return explicit
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise SystemDbConfigError("DATABASE_URL tidak valid") from e
    if not _no_password(parts):
        raise SystemDbConfigError(
            "DATABASE_SYSTEM_URL wajib diisi bila DATABASE_URL memakai "
            "password (tidak ada fallback ke DSN aplikasi/owner)")
    if parts.username != APP_ROLE:
        raise SystemDbConfigError(
            "DATABASE_SYSTEM_URL wajib diisi: turunan otomatis hanya untuk "
            f"DSN lokal tanpa password dengan user {APP_ROLE}")
    host = parts.netloc.split("@", 1)[1] if "@" in parts.netloc else ""
    sep = ":@" if parts.password == "" else "@"
    return urlunsplit((parts.scheme, f"{SYSTEM_ROLE}{sep}{host}",
                       parts.path, parts.query, parts.fragment))


# B12.4: TANPA format()/'%%' — satu-satunya '%' adalah placeholder
# parameter role aplikasi, sehingga hasil diagnostik tidak bergantung pada
# interpolasi psycopg2 (tes memastikan).
_IDENTITY_SQL = """
SELECT current_user::text AS cu, session_user::text AS su,
       r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb,
       r.rolreplication, r.rolinherit, r.rolcanlogin,
       COALESCE((SELECT array_agg(b.rolname::text ORDER BY b.rolname)
                   FROM pg_auth_members m JOIN pg_roles b ON b.oid=m.roleid
                  WHERE m.member = r.oid), '{}') AS member_of,
       COALESCE((SELECT array_agg(u.rolname::text ORDER BY u.rolname)
                   FROM pg_auth_members m JOIN pg_roles u ON u.oid=m.member
                  WHERE m.roleid = r.oid AND NOT u.rolsuper
                    AND u.oid <> (SELECT datdba FROM pg_database
                                   WHERE datname = current_database())),
                '{}') AS members,
       (SELECT count(*) FROM pg_class c WHERE c.relowner = r.oid)
         AS owned_rels,
       COALESCE((SELECT array_agg(n.nspname::text ORDER BY n.nspname)
                   FROM pg_namespace n WHERE n.nspowner = r.oid), '{}')
         AS owned_schemas,
       COALESCE((SELECT array_agg((CASE p.prokind
                                         WHEN 'p' THEN 'procedure'
                                         WHEN 'a' THEN 'aggregate'
                                         WHEN 'w' THEN 'window'
                                         ELSE 'function' END)
                                   || ' ' || p.oid::regprocedure::text
                                   ORDER BY p.oid)
                   FROM pg_proc p WHERE p.proowner = r.oid), '{}')
         AS owned_procs,
       COALESCE((SELECT array_agg((CASE t.typtype
                                         WHEN 'd' THEN 'domain'
                                         WHEN 'e' THEN 'enum'
                                         WHEN 'c' THEN 'composite'
                                         WHEN 'r' THEN 'range'
                                         WHEN 'm' THEN 'multirange'
                                         ELSE 'type' END)
                                   || ' ' || t.oid::regtype::text
                                   ORDER BY t.oid)
                   FROM pg_type t WHERE t.typowner = r.oid
                    AND NOT (t.typtype = 'c' AND EXISTS (
                        SELECT 1 FROM pg_class c WHERE c.oid = t.typrelid
                          AND c.relkind <> 'c'))), '{}')
         AS owned_types,
       COALESCE((SELECT array_agg(pg_describe_object(d.classid, d.objid,
                                                     d.objsubid)
                                  ORDER BY d.classid, d.objid)
                   FROM pg_shdepend d
                  WHERE d.refclassid = 'pg_authid'::regclass
                    AND d.refobjid = r.oid AND d.deptype = 'o'
                    AND d.dbid = (SELECT oid FROM pg_database
                                   WHERE datname = current_database())),
                '{}') AS owned_here,
       (SELECT count(*) FROM pg_shdepend d
         WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = r.oid
           AND d.deptype = 'o' AND d.dbid = 0) AS owned_shared,
       (SELECT count(*) FROM pg_shdepend d
         WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = r.oid
           AND d.deptype = 'o' AND d.dbid NOT IN (0, (SELECT oid FROM
               pg_database WHERE datname = current_database())))
         AS owned_other_db,
       (SELECT datdba = r.oid FROM pg_database
         WHERE datname = current_database()) AS is_dbowner,
       pg_has_role(%s, r.oid, 'MEMBER') AS app_is_member
  FROM pg_roles r WHERE r.rolname = current_user
"""


def verify_system_identity(conn) -> list[str]:
    """B12.2: daftar pelanggaran identitas koneksi sistem (kosong = sah).
    Syarat: current_user = session_user = transcendence_system (tanpa SET
    ROLE / opsi role di DSN); NOSUPERUSER, NOBYPASSRLS, NOCREATEROLE,
    NOCREATEDB, NOREPLICATION, NOINHERIT, LOGIN; bukan anggota role apa
    pun; tidak ada anggota selain superuser/owner DB (keanggotaan ADMIN
    pembuat role) dan role aplikasi TIDAK anggota (langsung/tidak
    langsung); tidak memiliki objek dan bukan owner database."""
    cur = conn.cursor()
    try:
        cur.execute(_IDENTITY_SQL, (APP_ROLE,))
        r = cur.fetchone()
    finally:
        cur.close()
    if r is None:
        return ["role current_user tidak ditemukan"]
    bad = []
    if r["cu"] != SYSTEM_ROLE:
        bad.append(f"current_user={r['cu']} (wajib {SYSTEM_ROLE})")
    if r["su"] != r["cu"]:
        bad.append(f"session_user={r['su']} != current_user={r['cu']}"
                   " (SET ROLE / opsi role pada DSN)")
    for col, label in (("rolsuper", "SUPERUSER"), ("rolbypassrls",
                       "BYPASSRLS"), ("rolcreaterole", "CREATEROLE"),
                       ("rolcreatedb", "CREATEDB"),
                       ("rolreplication", "REPLICATION"),
                       ("rolinherit", "INHERIT")):
        if r[col]:
            bad.append(f"role memiliki {label}")
    if not r["rolcanlogin"]:
        bad.append("role tanpa LOGIN")
    if r["member_of"]:
        bad.append(f"role anggota dari {list(r['member_of'])}")
    if r["members"]:
        bad.append(f"role memiliki anggota {list(r['members'])}")
    if r["app_is_member"]:
        bad.append(f"{APP_ROLE} dapat memakai role sistem (keanggotaan)")
    # B12.3: kepemilikan objek APA PUN (owner melewati RLS, dapat ALTER/
    # DROP/CREATE OR REPLACE). Objek internal PostgreSQL dimiliki bootstrap
    # superuser (pinned, tidak tercatat di pg_shdepend) -> setiap objek
    # milik role ini adalah objek NON-internal dan ditolak.
    if r["owned_rels"]:
        bad.append(f"role memiliki {r['owned_rels']} objek relasi (owner"
                   " bypass RLS)")
    if r["owned_schemas"]:
        bad.append(f"role memiliki schema {list(r['owned_schemas'])}")
    if r["owned_procs"]:
        bad.append(f"role memiliki {list(r['owned_procs'])[:10]}")
    if r["owned_types"]:
        bad.append(f"role memiliki {list(r['owned_types'])[:10]}")
    explicit = {str(x) for x in (r["owned_schemas"] or [])}
    other = [str(x) for x in (r["owned_here"] or [])
             if not str(x).startswith(("schema ", "function ", "procedure ",
                                       "aggregate ", "type ", "table ",
                                       "sequence ", "view ", "index "))]
    if other:
        bad.append(f"role memiliki objek lain {other[:10]}")
    elif r["owned_here"] and not (r["owned_rels"] or explicit or
                                  r["owned_procs"] or r["owned_types"]):
        bad.append(f"role memiliki objek {list(r['owned_here'])[:10]}")
    if r["owned_shared"] and not r["is_dbowner"]:
        bad.append(f"role memiliki {r['owned_shared']} objek shared"
                   " (database/tablespace)")
    if r["owned_other_db"]:
        bad.append(f"role memiliki {r['owned_other_db']} objek di database"
                   " lain")
    if r["is_dbowner"]:
        bad.append("role adalah owner database")
    return bad


def _open_system(config, slot: str):
    """Buka + VERIFIKASI koneksi sistem untuk slot thread-local [slot].
    Gagal (DSN, koneksi, identitas) -> koneksi ditutup, slot DIKOSONGKAN,
    SystemDbConfigError. Tidak ada koneksi tak terverifikasi tersimpan."""
    old = getattr(_local, slot, None)
    try:
        key = system_dsn(config)
    except SystemDbConfigError:
        _clear_slot(slot, close=True)
        raise
    if old is not None and getattr(_local, slot + "_key", None) == key:
        return old
    _clear_slot(slot, close=True)
    conn = None
    try:
        conn = PostgresConn(key, role_kind="system")
        bad = verify_system_identity(conn)
        conn.rollback()
    except SystemDbConfigError:
        raise
    except Exception as e:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        raise SystemDbConfigError(
            f"koneksi sistem gagal dibuka/diverifikasi: {type(e).__name__}"
        ) from e
    if bad:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise SystemDbConfigError(
            "koneksi sistem DITOLAK: " + "; ".join(bad))
    conn.config = config
    setattr(_local, slot, conn)
    setattr(_local, slot + "_key", key)
    return conn


def _clear_slot(slot: str, close: bool) -> None:
    conn = getattr(_local, slot, None)
    if conn is not None and close:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    setattr(_local, slot, None)
    setattr(_local, slot + "_key", None)


def get_system_db(config):
    """B12.1/B12.2: satu koneksi SISTEM per thread (terpisah dari get_db),
    identitas diverifikasi ketat saat dibuka. SQLite: sama dengan get_db
    (tanpa RLS)."""
    if not getattr(config, "db_url", ""):
        return get_db(config)
    return _open_system(config, "sysconn")


def get_audit_db(config):
    """B12.1/B12.2: koneksi ROLE SISTEM khusus penulisan audit_logs (per
    thread), TIDAK PERNAH diberi customer context; validasi identitas SAMA
    dengan get_system_db. Dipakai audit() bila scope koneksi pemanggil
    bukan milik customer_id audit (pra-auth, customer sudah dihapus)."""
    return _open_system(config, "auditconn")


def _drop_system_slot(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    if getattr(_local, "sysconn", None) is conn:
        _local.sysconn = None
        _local.sysconn_key = None


def clear_system_context(config) -> None:
    """Lepas customer context pada koneksi SISTEM thread ini (bila ada)."""
    conn = getattr(_local, "sysconn", None)
    if conn is not None:
        conn.clear_customer_context()


def close_db() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.conn_key = None
    for slot in ("sysconn", "auditconn"):
        sc = getattr(_local, slot, None)
        if sc is not None:
            try:
                sc.close()
            except Exception:  # noqa: BLE001
                pass
            setattr(_local, slot, None)
            setattr(_local, slot + "_key", None)


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


class MigrationPrivilegeError(RuntimeError):
    """Migrasi tertunda tetapi role koneksi tidak berhak DDL.

    Role aplikasi produksi (transcendence_app, migrasi 002) sengaja
    NON-OWNER tanpa hak CREATE pada skema — migrasi WAJIB dijalankan
    dengan DSN owner (docs/MULTI-CUSTOMER-INSTALASI-PROVISIONING.md §4).
    """


def _applied_versions(conn) -> set[int] | None:
    """Versi yang sudah diterapkan, atau None bila tabel belum ada.

    Hanya SELECT — tidak butuh hak DDL (aman untuk role non-owner).
    """
    if is_postgres(conn):
        row = conn.execute(
            "SELECT to_regclass('public.schema_migrations') AS t").fetchone()
        exists = row is not None and row["t"] is not None
    else:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='schema_migrations'").fetchone() is not None
    if not exists:
        return None
    return {int(r["version"]) for r in conn.execute(
        "SELECT version FROM schema_migrations")}


def _migration_files(conn) -> list[tuple[int, Path]]:
    subdir = "postgres" if is_postgres(conn) else "sqlite"
    return [(int(f.name.split("_", 1)[0]), f)
            for f in sorted((MIGRATIONS_DIR / subdir).glob("*.sql"))]


def _is_privilege_error(exc: BaseException) -> bool:
    code = getattr(exc, "pgcode", None)
    # B12.4: RAISE EXCEPTION eksplisit dari migrasi (P0001) BUKAN galat hak
    # DDL generik walaupun teksnya memuat "permission denied" — pesannya
    # (mis. creator default privileges yang tidak dapat dicabut) harus
    # sampai ke operator apa adanya, tidak diganti pesan generik.
    if code == "P0001":
        return False
    # 42501 insufficient_privilege; 42P01 tidak relevan di sini
    return code == "42501" or "permission denied" in str(exc).lower()


def migrate(conn) -> list[int]:
    """Terapkan migrasi engine yang sesuai. Return daftar versi baru.

    - SQLite  : migrations/sqlite/*.sql   (001_init.sql = skema lama utuh)
    - Postgres: migrations/postgres/*.sql (001_multi_customer.sql)
    Aman dijalankan ulang; tiap file diterapkan maksimal sekali.

    Remediasi startup non-owner: bila SEMUA migrasi sudah diterapkan,
    fungsi ini TIDAK menjalankan DDL apa pun (hanya SELECT) — backend
    dapat start dengan role aplikasi non-owner (transcendence_app) yang
    tidak punya hak CREATE pada skema public (PostgreSQL >= 15). Bila
    ada migrasi tertunda dan role tidak berhak DDL, dilempar
    MigrationPrivilegeError yang jelas (bukan traceback psycopg2).
    """
    files = _migration_files(conn)
    applied = _applied_versions(conn)
    conn.commit()
    if applied is not None and all(v in applied for v, _ in files):
        return []
    try:
        if applied is None:
            conn.execute(_schema_migrations_ddl(conn))
            conn.commit()
            applied = set()
        done: list[int] = []
        for version, sql_file in files:
            if version in applied:
                continue
            conn.executescript(sql_file.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)",
                         (version,))
            conn.commit()
            done.append(version)
        return done
    except Exception as e:  # noqa: BLE001
        if is_postgres(conn) and _is_privilege_error(e):
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            pending = sorted(v for v, _ in files
                             if applied is None or v not in applied)
            raise MigrationPrivilegeError(
                f"Migrasi tertunda {pending} tidak bisa diterapkan dengan "
                "role koneksi ini (tanpa hak DDL). Jalankan migrasi dengan "
                "DSN OWNER database terlebih dahulu (lihat docs/MULTI-"
                "CUSTOMER-INSTALASI-PROVISIONING.md §4), lalu start "
                "backend dengan role aplikasi non-owner.") from e
        raise
