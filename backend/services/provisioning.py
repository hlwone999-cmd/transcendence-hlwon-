"""Koordinator auto-provisioning terminal MT5 (keputusan produk #3).

Alur job (mt5_connect_jobs) — dijalankan worker latar, status di DB
(konsisten lintas worker & restart):

    pending -> provisioning (helper install) -> starting (helper start +
    health check) -> verifying (login nyata broker, FAIL-CLOSED) ->
    connected | failed

Jaminan (Tahap 3A/E):
- Satu customer satu terminal (UNIQUE customer_id + alokasi idempoten di
  TerminalRegistry.provision_terminal, ber-capacity guard).
- Setiap terminal yang DIBUAT job memiliki provision_id (uuid4 hex).
  Cleanup hanya menghapus resource yang dibuat job itu sendiri dan hanya
  bila bukti kepemilikan cocok (DB: WHERE id=? AND provision_id=?;
  helper: penanda .provision_id). Tanpa bukti -> TIDAK dihapus, terminal
  ditandai 'failed' untuk ditangani admin.
- Batas waktu total (deadline_at) — lewat batas = gagal + cleanup aman.
- Token sesi TIDAK PERNAH diterbitkan sebelum job 'connected'; klaim
  token atomik satu kali (token_issued_at).
- Pemulihan: job non-final yang melewati deadline (mis. proses mati /
  restart) diklaim atomik oleh satu worker lalu dibersihkan.
- F2: timeout/sibuk/tidak-pasti dari helper TIDAK PERNAH membebaskan slot
  atau menghapus baris terminal. Baris ditahan lifecycle 'failed' dengan
  last_error 'retry:<n>:<kelas>:<state>'; retry_uncertain_removals()
  (startup) mencoba remove lagi — tetap hanya dengan bukti kepemilikan.
- F2/T1 — PEMEGANG BARIS TERMINAL (kolom op_owner, migrasi 006):
    NULL              bebas
    'job:<id>'        dipakai job aktivasi (diambil atomik di run_job)
    'remove:<uuid>'   penghapusan berjalan (klaim atomik + lease)
    'removal-pending' penghapusan tertunda — job baru TIDAK boleh memakai
  Semua transisi = UPDATE/DELETE BERSYARAT (op_owner + lifecycle +
  provision_id) di SQL — aman untuk beberapa worker PostgreSQL (READ
  COMMITTED mengevaluasi ulang WHERE setelah row lock). Job A tidak dapat
  menghapus terminal yang dipegang job B; job B tidak dapat memakai
  terminal yang sedang/akan dihapus; token hanya diklaim bila terminal
  job masih ada & tidak sedang dihapus.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import db as dbmod
from services.provisioner import (OwnershipError, ProvisionBusy,
                                  ProvisionError, ProvisionIncomplete,
                                  ProvisionTimeout, TerminalSpec)
from services.terminals import (probe_ro_session, probe_rpyc_session,
                                terminal_token)
from services.verification import live_verify_account

log = logging.getLogger(__name__)

FINAL = ("connected", "failed")
CLEANUP_PREFIX = "cleanup:"
# Fase cleanup ke-2 (akun sudah dibersihkan; tinggal terminal). Tetap diawali
# "cleanup:" agar is_final()/pemulihan menganggapnya belum final.
CLEANUP_TERM_PREFIX = "cleanup:terminal:"
OWNER_JOB = "job:"
OWNER_REMOVE = "remove:"
OWNER_PENDING = "removal-pending"
# B12: predikat klaim tahap 2 (scope SISTEM) — paritas
# _recovery_mapping_problem, dievaluasi atomik di UPDATE klaim.
_NO_CORRUPT_JOB = (
    "EXISTS (SELECT 1 FROM customers c WHERE"
    " c.id = mt5_terminals.customer_id) AND NOT EXISTS ("
    "SELECT 1 FROM mt5_connect_jobs j WHERE"
    " (j.terminal_id = mt5_terminals.id OR"
    " j.provision_id = mt5_terminals.provision_id) AND"
    " (j.customer_id IS NULL OR j.customer_id <> mt5_terminals.customer_id"
    " OR j.terminal_id IS NULL OR j.terminal_id <> mt5_terminals.id"
    " OR j.provision_id IS NULL OR"
    " j.provision_id <> mt5_terminals.provision_id))")
REMOVE_FAILED_PREFIX = "remove-failed:"   # gagal permanen -> admin
RETRY_PREFIX = "retry:"           # F2: remove belum terbukti, boleh diulang
EXHAUSTED_PREFIX = "retry-exhausted:"   # batas retry habis -> admin
RETRIABLE = (ProvisionBusy, ProvisionIncomplete, ProvisionTimeout)
MAX_REMOVE_RETRIES = 5


def _retry_count(last_error) -> int:
    """Jumlah percobaan remove yang tercatat di last_error 'retry:<n>:...'."""
    s = str(last_error or "")
    if not s.startswith(RETRY_PREFIX):
        return 0
    head = s[len(RETRY_PREFIX):].split(":", 1)[0]
    return int(head) if head.isdigit() else 1


class ScopeRestoreError(RuntimeError):
    """B10.1: scope RLS pemanggil TIDAK dapat dipulihkan. Koneksi sudah
    diamankan (scope dilepas, ditutup, dilepas dari slot thread-local) dan
    tidak boleh dipakai lagi. Operasi pemanggil TIDAK boleh dianggap
    sukses."""


_SECURED_ATTR = "_scope_secured"     # penanda: koneksi sudah diamankan


def _restore_scope(conn, prev):
    """Pasang kembali scope [prev]; bila gagal: rollback lalu ulangi.
    Return: None bila berhasil, pengecualian terakhir bila gagal."""
    try:
        dbmod.set_customer_context(conn, prev)
        return None
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.rollback()
        dbmod.set_customer_context(conn, prev)
        return None
    except Exception as e:  # noqa: BLE001
        return e


def _secure_close(conn) -> None:
    """Amankan koneksi yang scope-nya tidak pasti — TEPAT SEKALI:
    lepas scope (bila mungkin), tutup, dan lepas dari slot thread-local
    sehingga get_db() berikutnya membuka koneksi BARU tanpa scope."""
    if getattr(conn, _SECURED_ATTR, False):
        return
    try:
        setattr(conn, _SECURED_ATTR, True)
    except Exception:  # noqa: BLE001
        pass
    try:
        dbmod.set_customer_context(conn, None)
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    # B12.1: kosongkan HANYA slot thread-local milik koneksi ini (koneksi
    # aplikasi ATAU koneksi role sistem) — koneksi lain thread ini tidak
    # ditutup karena scope koneksi ini tidak pasti.
    loc = dbmod._local
    for slot in ("conn", "sysconn", "auditconn"):
        if getattr(loc, slot, None) is conn:
            setattr(loc, slot, None)
            setattr(loc, slot + "_key", None)


@contextmanager
def _caller_scope(conn):
    """B10/T3-1: simpan scope RLS customer PEMANGGIL dan pulihkan di finally
    — pada return normal, early return, pengecualian (termasuk timeout
    helper), dan pemanggilan bersarang. Paritas pola
    services/terminals.py (_account_config): set_customer_context(prev),
    soft-clear bila pemanggil tanpa scope, TANPA rollback.

    B10.1 — pemulihan gagal (setelah rollback + ulang):
    - koneksi diamankan sekali (_secure_close): scope dilepas, ditutup,
      dilepas dari slot thread-local; tidak pernah dipakai ulang;
    - body selesai normal  -> ScopeRestoreError (TIDAK ada sukses palsu);
    - body sudah melempar  -> pengecualian ASLI diteruskan apa adanya;
      kegagalan pemulihan hanya dicatat (tidak menutupi galat asli);
    - koneksi yang SUDAH diamankan (mis. oleh context manager bersarang)
      tidak dipulihkan/ditutup lagi."""
    if not dbmod.is_postgres(conn):
        yield
        return
    prev = conn.current_customer_context()
    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        if getattr(conn, _SECURED_ATTR, False):
            err = None                       # sudah diamankan di tingkat lain
            secured_before = True
        else:
            secured_before = False
            err = _restore_scope(conn, prev)
        if err is not None:
            log.error("pemulihan scope RLS gagal — koneksi diamankan & "
                      "ditutup (%s)", type(err).__name__)
            _secure_close(conn)
            if not body_failed:
                raise ScopeRestoreError(
                    "scope RLS pemanggil tidak dapat dipulihkan; koneksi "
                    "ditutup — operasi TIDAK sukses") from err
        elif secured_before and not body_failed:
            raise ScopeRestoreError(
                "koneksi sudah diamankan (scope tidak pasti) — operasi "
                "TIDAK sukses")


def _col(row, name, default=None):
    try:
        return row[name]
    except (KeyError, IndexError):
        return default


def _cleanup_klass(error_class) -> str:
    ec = str(error_class or "")
    for pre in (CLEANUP_TERM_PREFIX, CLEANUP_PREFIX):
        if ec.startswith(pre):
            return ec[len(pre):]
    return ec


def _iso_ago(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def is_final(job) -> bool:
    """Final = connected, atau failed DAN cleanup selesai."""
    if job is None:
        return True
    if job["state"] == "connected":
        return True
    return job["state"] == "failed" and not str(
        job["error_class"] or "").startswith(CLEANUP_PREFIX)
AUTH_CLASSES = ("auth_failed", "identity_mismatch", "master_password")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class JobTimeout(Exception):
    pass


class TerminalUnavailable(ProvisionError):
    """Baris terminal dipegang operasi lain (job lain / penghapusan) —
    job ini TIDAK boleh memakainya dan TIDAK boleh menyentuhnya."""


class ConnectCoordinator:
    def __init__(self, app, cfg, registry, provisioner):
        self._app = app
        self._cfg = cfg
        self._registry = registry
        self.provisioner = provisioner

    # ----------------------------------------------------------- helpers
    def spec_for(self, conn, terminal_id: int,
                 customer_id: int | None) -> TerminalSpec | None:
        """B11/B11.1: customer_id WAJIB; baris terminal harus milik customer
        itu. customer_id None / milik customer lain -> None (fail-closed,
        sama seperti tidak ada)."""
        if customer_id is None:
            return None
        row = conn.execute(
            "SELECT t.rpyc_port, t.provision_id, t.rpc_mode, c.code"
            " FROM mt5_terminals t JOIN customers c ON c.id=t.customer_id"
            " WHERE t.id=? AND t.customer_id=?",
            (terminal_id, int(customer_id))).fetchone()
        if row is None:
            return None
        return TerminalSpec(code=row["code"], port=int(row["rpyc_port"]),
                            provision_id=row["provision_id"],
                            rpc_mode=row["rpc_mode"] or "classic")

    @staticmethod
    def _terminal_mapping(conn, terminal_id: int, customer_id) -> str:
        """B11/T3-2: 'match' | 'absent' | 'mismatch' (baris ada tetapi
        milik customer lain, atau customer_id job tidak ada)."""
        row = conn.execute("SELECT customer_id FROM mt5_terminals WHERE id=?",
                           (terminal_id,)).fetchone()
        if row is None:
            return "absent"
        if customer_id is None or int(row["customer_id"]) != int(customer_id):
            return "mismatch"
        return "match"

    def listener_check(self, spec: TerminalSpec):
        """Mode real: listener RPyC wajib milik unit customer INI."""
        if self._cfg.terminal_provision_mode != "real":
            return None
        return lambda: bool(self.provisioner.status(spec).get("running"))

    # ---------------------------------------------------------- creation
    def create_job(self, conn, *, kind: str, customer_id: int, user_id: int,
                   account_id: int, terminal: dict, created_customer: bool,
                   created_account: bool, prev_account_state: str | None,
                   require_real: bool, lock_ip_hash: str | None,
                   lock_acct_hash: str | None) -> tuple[str, str]:
        job_id = uuid.uuid4().hex
        secret = secrets.token_urlsafe(32)
        now = _now_iso()
        conn.execute(
            """INSERT INTO mt5_connect_jobs (id, secret_hash, kind, state,
                 customer_id, user_id, account_id, terminal_id, provision_id,
                 created_customer, created_account, created_terminal,
                 prev_account_state, require_real, lock_ip_hash,
                 lock_acct_hash, deadline_at, created_at, updated_at)
               VALUES (?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, hash_secret(secret), kind, customer_id, user_id,
             account_id, terminal["terminal_id"], terminal.get("provision_id"),
             int(created_customer), int(created_account),
             int(bool(terminal.get("created"))), prev_account_state,
             int(require_real), lock_ip_hash, lock_acct_hash,
             time.time() + float(self._cfg.mt5_provision_timeout_seconds),
             now, now))
        conn.commit()
        return job_id, secret

    def start(self, job_id: str) -> threading.Thread:
        def _run():
            with self._app.app_context():
                try:
                    self.run_job(job_id)
                except Exception:  # noqa: BLE001
                    log.exception("job provisioning %s gagal", job_id)
                finally:
                    try:
                        dbmod.clear_customer_context(
                            dbmod.get_db(self._cfg))
                    except Exception:  # noqa: BLE001
                        pass
                    try:                  # B12.1: koneksi role sistem worker
                        dbmod.clear_system_context(self._cfg)
                    except Exception:  # noqa: BLE001
                        pass
        th = threading.Thread(target=_run, daemon=True,
                              name=f"provision-{job_id[:8]}")
        th.start()
        return th

    @staticmethod
    def load(conn, job_id: str):
        return conn.execute("SELECT * FROM mt5_connect_jobs WHERE id=?",
                            (job_id,)).fetchone()

    def wait(self, conn, job_id: str, timeout: float):
        end = time.monotonic() + max(0.0, timeout)
        while True:
            conn.rollback()           # snapshot baru (PG/SQLite WAL)
            job = self.load(conn, job_id)
            if is_final(job) or time.monotonic() >= end:
                return job
            time.sleep(0.05)

    @staticmethod
    def check_secret(job, secret: str) -> bool:
        return job is not None and hmac.compare_digest(
            job["secret_hash"], hash_secret(secret or ""))

    @staticmethod
    def claim_token(conn, job_id: str) -> bool:
        """Klaim token SEKALI — hanya bila job 'connected' DAN terminal job
        masih ada, tidak sedang/akan dihapus (T1). Satu UPDATE bersyarat."""
        cur = conn.execute(
            "UPDATE mt5_connect_jobs SET token_issued_at=? WHERE id=? AND"
            " state='connected' AND token_issued_at IS NULL AND EXISTS ("
            "SELECT 1 FROM mt5_terminals t WHERE"
            " t.id = mt5_connect_jobs.terminal_id AND"
            " t.customer_id = mt5_connect_jobs.customer_id AND"
            " t.lifecycle <> 'stopping' AND"
            " (t.op_owner IS NULL OR t.op_owner LIKE ?))",
            (_now_iso(), job_id, OWNER_JOB + "%"))
        conn.commit()
        return cur.rowcount == 1

    # ------------------------------------------------- pemegang terminal
    @staticmethod
    def _acquire_terminal(conn, job_id: str, terminal_id: int,
                          customer_id: int) -> None:
        """Ambil baris terminal untuk job ini (atomik). Gagal bila dipegang
        job lain, sedang dihapus, penghapusan tertunda, atau (B11) baris
        bukan milik customer job."""
        owner = OWNER_JOB + job_id
        cur = conn.execute(
            "UPDATE mt5_terminals SET op_owner=?, updated_at=? WHERE id=? AND"
            " customer_id=? AND lifecycle <> 'stopping' AND"
            " (op_owner IS NULL OR op_owner=?)",
            (owner, _now_iso(), terminal_id, int(customer_id), owner))
        conn.commit()
        if cur.rowcount != 1:
            raise TerminalUnavailable("terminal sedang dipakai operasi lain"
                                      " atau sedang dibersihkan")

    @staticmethod
    def _release_terminal(conn, job_id: str, terminal_id: int,
                          customer_id) -> None:
        """Job gagal yang TIDAK membuat terminal melepas pegangannya (B11:
        hanya baris milik customer job)."""
        if customer_id is None:
            log.warning("lepas terminal %s: customer job tidak ada — tidak"
                        " disentuh", terminal_id)
            return
        try:
            conn.execute(
                "UPDATE mt5_terminals SET op_owner=NULL, lifecycle=CASE WHEN"
                " lifecycle IN ('provisioning','starting','verifying') THEN"
                " 'failed' ELSE lifecycle END, updated_at=? WHERE id=? AND"
                " customer_id=? AND op_owner=?",
                (_now_iso(), terminal_id, int(customer_id),
                 OWNER_JOB + job_id))
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            log.exception("lepas terminal %s (job %s) gagal", terminal_id,
                          job_id)

    # ------------------------------------------------------------ state
    def _set(self, conn, job_id: str, state: str, terminal_id=None,
             lifecycle: str | None = None, customer_id=None) -> None:
        cur = conn.execute(
            "UPDATE mt5_connect_jobs SET state=?, updated_at=? WHERE id=?"
            " AND state NOT IN ('connected','failed')",
            (state, _now_iso(), job_id))
        if cur.rowcount != 1:
            conn.rollback()
            raise JobTimeout("job sudah diklaim pemulihan")
        if terminal_id is not None and lifecycle:
            if customer_id is None:
                conn.rollback()
                raise TerminalUnavailable("customer job tidak diketahui")
            cur = conn.execute(
                "UPDATE mt5_terminals SET lifecycle=?, updated_at=? WHERE"
                " id=? AND customer_id=? AND op_owner=?",
                (lifecycle, _now_iso(), terminal_id, int(customer_id),
                 OWNER_JOB + job_id))
            if cur.rowcount != 1:            # T1: terminal bukan milik job
                conn.rollback()
                raise TerminalUnavailable("terminal tidak lagi dipegang job")
        conn.commit()

    def _remaining(self, job) -> float:
        rem = float(job["deadline_at"]) - time.time()
        if rem <= 0:
            raise JobTimeout("melewati batas waktu provisioning")
        return rem

    def _healthy(self, spec: TerminalSpec) -> bool:
        st = self.provisioner.status(spec)
        if not st.get("running"):
            return False
        if self._cfg.terminal_provision_mode == "real":
            if spec.rpc_mode == "ro":
                return probe_ro_session(spec.port, terminal_token(
                    self._cfg, spec.code, spec.port, spec.provision_id))
            return probe_rpyc_session(spec.port) is True
        return True

    # ------------------------------------------------------------ worker
    def run_job(self, job_id: str) -> str:
        """B12.1: worker berjalan pada koneksi ROLE SISTEM (job di-load
        tanpa customer context), lalu seluruh operasi job berjalan dengan
        customer context job (policy *_system membatasi ke customer itu).
        Scope koneksi sistem dipulihkan di akhir."""
        conn = dbmod.get_system_db(self._cfg)
        with _caller_scope(conn):
            dbmod.set_customer_context(conn, None)
            return self._run_job(conn, job_id)

    def _run_job(self, conn, job_id: str) -> str:
        job = self.load(conn, job_id)
        if job is None or job["state"] in FINAL:
            return job["state"] if job else "missing"
        if job["customer_id"] is None:
            # job tanpa customer = korup: tidak ada operasi customer apa pun
            log.error("job %s tanpa customer_id — DITOLAK", job_id)
            dbmod.set_customer_context(conn, None)
            return "failed"
        cid = int(job["customer_id"])
        try:
            dbmod.set_customer_context(conn, cid)
            tid = int(job["terminal_id"])
            # B11/T3-2: terminal WAJIB milik customer job sebelum helper apa
            # pun dijalankan; tidak cocok / hilang -> gagal tertutup
            spec = self.spec_for(conn, tid, cid)
            if spec is None:
                raise TerminalUnavailable(
                    "terminal job tidak ada / bukan milik customer job ("
                    + self._terminal_mapping(conn, tid, cid) + ")")
            self._acquire_terminal(conn, job_id, tid, cid)   # T1 (atomik)
            trow = conn.execute("SELECT lifecycle FROM mt5_terminals"
                                " WHERE id=? AND customer_id=?",
                                (tid, cid)).fetchone()
            if trow is None:
                raise TerminalUnavailable("terminal bukan milik customer job")
            if job["created_terminal"]:
                self._set(conn, job_id, "provisioning", tid, "provisioning",
                          cid)
                self.provisioner.install(spec)
            needs_start = bool(job["created_terminal"]) or (
                trow["lifecycle"] in ("stopped", "failed")
                and spec.provision_id is not None)
            if needs_start:
                self._remaining(job)
                self._set(conn, job_id, "starting", tid, "starting", cid)
                self.provisioner.start(spec)
                while not self._healthy(spec):
                    self._remaining(job)
                    time.sleep(0.2)
            self._set(conn, job_id, "verifying", tid, "verifying", cid)
            acc = conn.execute("SELECT login, server FROM mt5_accounts"
                               " WHERE id=?", (job["account_id"],)).fetchone()
            ver = live_verify_account(
                conn, self._registry, int(job["account_id"]),
                timeout=min(float(self._cfg.mt5_verify_timeout_seconds),
                            self._remaining(job)),
                enforce_investor_readonly=self._cfg.
                mt5_enforce_investor_readonly,
                expected_login=int(acc["login"]),
                expected_server=acc["server"],
                require_real=bool(job["require_real"]),
                listener_check=self.listener_check(spec))
            if ver["class"] != "ok":
                return self.fail(conn, job_id, ver["class"])
            self._remaining(job)
            # T1: SATU transaksi — terminal masih dipegang job ini (UPDATE
            # bersyarat, row lock) LALU job 'connected'. Bila terminal sudah
            # diambil/dihapus pihak lain -> rollback; job TIDAK connected.
            cur = conn.execute(
                "UPDATE mt5_terminals SET lifecycle='connected',"
                " status='ready', last_error=NULL, op_owner=NULL,"
                " updated_at=? WHERE id=? AND customer_id=? AND op_owner=? AND"
                " (provision_id=? OR (provision_id IS NULL AND ? IS NULL))",
                (_now_iso(), tid, cid, OWNER_JOB + job_id, spec.provision_id,
                 spec.provision_id))
            if cur.rowcount != 1:
                conn.rollback()
                raise TerminalUnavailable("terminal tidak lagi dipegang job")
            cur = conn.execute(
                "UPDATE mt5_connect_jobs SET state='connected', updated_at=?"
                " WHERE id=? AND state NOT IN ('connected','failed')",
                (_now_iso(), job_id))
            if cur.rowcount != 1:
                conn.rollback()
                return "failed"
            conn.execute("UPDATE mt5_accounts SET state='active',"
                         " unbound_at=NULL WHERE id=?", (job["account_id"],))
            conn.commit()
            self._on_connected(conn, job, acc)
            return "connected"
        except JobTimeout:
            return self.fail(conn, job_id, "timeout")
        except OwnershipError:
            return self.fail(conn, job_id, "ownership")
        except ProvisionError:
            return self.fail(conn, job_id, "provision_failed")
        except Exception:  # noqa: BLE001
            log.exception("job %s: kegagalan internal", job_id)
            return self.fail(conn, job_id, "internal")

    def _on_connected(self, conn, job, acc) -> None:
        """Paritas PR #4: audit keberhasilan ('bind' + flag reactivate /
        'mt5_connect') dan sync awal dimulai DI SINI — tidak bergantung
        pada klien mengambil hasil job. Kegagalan tidak membatalkan job."""
        import audit as auditmod
        try:
            if job["kind"] == "bind":
                auditmod.audit(conn, "bind", customer_id=job["customer_id"],
                               user_id=job["user_id"],
                               entity=f"mt5_account:{job['account_id']}",
                               detail={"server": acc["server"],
                                       "reactivate":
                                           not bool(job["created_account"]),
                                       "live_verified": True})
            else:
                auditmod.audit(conn, "mt5_connect",
                               customer_id=job["customer_id"],
                               user_id=job["user_id"],
                               entity=f"mt5_account:{job['account_id']}",
                               detail={"mode": job["kind"],
                                       "server": acc["server"],
                                       "live_verified": True})
        except Exception:  # noqa: BLE001
            log.exception("audit job %s gagal", job["id"])
        if not self._app.extensions.get("auto_initial_sync", True):
            return
        app, acc_id = self._app, int(job["account_id"])

        def _sync():
            try:
                with app.app_context():
                    # B12.1: lookup akun/runtime lewat koneksi ROLE SISTEM;
                    # sync_account men-scope customer akun sendiri.
                    sc = dbmod.get_system_db(self._cfg)
                    dbmod.set_customer_context(sc, None)
                    app.extensions["mt5_sync"].sync_account(
                        acc_id, mode="initial", conn=sc)
            except Exception:  # noqa: BLE001
                log.exception("sync awal akun %d gagal", acc_id)
        threading.Thread(target=_sync, daemon=True,
                         name=f"initial-sync-{acc_id}").start()

    # ---------------------------------------------------------- failure
    def fail(self, conn, job_id: str, klass: str) -> str:
        """Klaim job (atomik) lalu bersihkan resource milik job."""
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        # Klaim atomik dgn penanda fase cleanup: job baru dianggap FINAL
        # (oleh wait()/polling) setelah cleanup selesai — respons gagal
        # tidak pernah dikirim sebelum resource milik job dibersihkan.
        cur = conn.execute(
            "UPDATE mt5_connect_jobs SET state='failed', error_class=?,"
            " updated_at=? WHERE id=? AND state NOT IN ('connected','failed')",
            (CLEANUP_PREFIX + klass, _now_iso(), job_id))
        conn.commit()
        if cur.rowcount != 1:
            return "failed"          # sudah diklaim pihak lain
        job = self.load(conn, job_id)
        self.cleanup(conn, job)
        conn.execute("UPDATE mt5_connect_jobs SET error_class=?, updated_at=?"
                     " WHERE id=?", (klass, _now_iso(), job_id))
        conn.commit()
        if klass in AUTH_CLASSES:
            import auth as authmod
            for kh, lock in ((job["lock_ip_hash"],
                              authmod._lockout_for(self._cfg)),
                             (job["lock_acct_hash"],
                              authmod._account_lockout_for(self._cfg))):
                if kh:
                    lock.record_failure_hashed(kh, conn=conn)
        return "failed"

    def cleanup(self, conn, job) -> None:
        """Lihat _cleanup. B10: scope RLS pemanggil dipulihkan."""
        with _caller_scope(conn):
            self._cleanup(conn, job)

    def _cleanup(self, conn, job) -> None:
        """Hapus HANYA resource milik job (Tahap 3A/E).

        T1: (1) akun/kredensial hanya dibersihkan selama akun MASIH 'pending'
        (belum diambil alih job lain) dan hanya SEKALI per job — fase
        dicatat 'cleanup:terminal:' sehingga pengulangan cleanup (pemulihan
        setelah crash) tidak menyentuh akun yang kini milik job baru;
        (2) terminal dihapus hanya bila masih dipegang job ini."""
        cid = job["customer_id"]
        acc_id = job["account_id"]
        ec = str(_col(job, "error_class") or "")
        # B12/T3-3: seluruh cleanup di scope customer JOB (DB menolak baris
        # customer lain). B12.1: job TANPA customer = korup -> berhenti di
        # sini (di koneksi role sistem "tanpa customer context" berarti
        # lintas customer, jadi TIDAK boleh lanjut tanpa customer).
        if cid is None:
            log.error("cleanup job %s tanpa customer_id — DITOLAK, tidak ada"
                      " resource yang disentuh", job["id"])
            return
        dbmod.set_customer_context(conn, int(cid))
        if acc_id is not None:
            try:
                self._registry.evict(int(acc_id))
            except Exception:  # noqa: BLE001
                pass
        if not ec.startswith(CLEANUP_TERM_PREFIX):
            try:
                if acc_id is not None:
                    conn.execute(
                        "DELETE FROM mt5_credentials WHERE account_id=? AND"
                        " EXISTS (SELECT 1 FROM mt5_accounts a WHERE a.id=?"
                        " AND a.state='pending')", (acc_id, acc_id))
                    if job["created_account"]:
                        conn.execute("DELETE FROM mt5_accounts WHERE id=? AND"
                                     " customer_id=? AND state='pending'",
                                     (acc_id, cid))
                    else:
                        conn.execute(
                            "UPDATE mt5_accounts SET state=? WHERE id=? AND"
                            " state='pending'",
                            (job["prev_account_state"] or "unbound", acc_id))
                if ec.startswith(CLEANUP_PREFIX):
                    conn.execute(
                        "UPDATE mt5_connect_jobs SET error_class=? WHERE id=?"
                        " AND error_class=?",
                        (CLEANUP_TERM_PREFIX + _cleanup_klass(ec), job["id"],
                         ec))
                conn.commit()
            except Exception:  # noqa: BLE001
                conn.rollback()
                log.exception("cleanup akun job %s gagal", job["id"])
        if job["terminal_id"] is not None:
            if job["created_terminal"]:
                self.remove_terminal(conn, int(job["terminal_id"]),
                                     job["provision_id"],
                                     expect_owner=OWNER_JOB + job["id"],
                                     customer_id=cid)
            else:
                self._release_terminal(conn, job["id"],
                                       int(job["terminal_id"]), cid)
        self._drop_customer_if_empty(conn, job)

    @staticmethod
    def _drop_customer_if_empty(conn, job) -> None:
        cid = job["customer_id"]
        if job["created_customer"] and cid is not None:
            try:
                left = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM mt5_accounts WHERE"
                    " customer_id=?) + (SELECT COUNT(*) FROM mt5_terminals"
                    " WHERE customer_id=?) AS n", (cid, cid)).fetchone()["n"]
                if int(left) == 0:
                    conn.execute("DELETE FROM customers WHERE id=?", (cid,))
                conn.commit()
            except Exception:  # noqa: BLE001
                conn.rollback()
                log.exception("cleanup customer job %s gagal", job["id"])

    def _remove_settled(self, spec: TerminalSpec) -> None:
        """remove di helper; bila respons hilang tetapi helper TERBUKTI
        berhenti (timeout non-uncertain: stopped_absent/incomplete/complete,
        atau helper berhenti sendiri exit 5), ulangi SEKALI. remove helper
        idempoten & berbasis penanda: already_absent bila sudah terhapus,
        OwnershipError bila resource kini milik provision_id lain.
        helper_running/unknown/sibuk TIDAK diulang segera (fail-closed)."""
        try:
            self.provisioner.remove(spec)
        except ProvisionTimeout as e:
            if e.uncertain:
                raise
            self.provisioner.remove(spec)
        except ProvisionIncomplete:
            self.provisioner.remove(spec)

    def remove_terminal(self, conn, terminal_id: int,
                        provision_id: str | None,
                        expect_owner: str | None = None,
                        claimed_op: str | None = None, *,
                        customer_id) -> bool:
        """B12/T3-3: kepemilikan baris dibaca di scope SISTEM (agar terminal
        customer lain terdeteksi 'mismatch', bukan 'absent' karena
        tersembunyi RLS -> tidak ada sukses palsu); penghapusan berjalan di
        scope customer [customer_id] (DB ikut menolak baris customer lain).
        Scope pemanggil dipulihkan. Lihat _remove_terminal."""
        with _caller_scope(conn):
            with dbmod.system_conn(self._cfg) as sc:
                mapping = self._terminal_mapping(sc, terminal_id,
                                                 customer_id)
                sc.commit()
            if mapping == "absent":
                return True
            if mapping == "mismatch":
                log.error("terminal %s: bukan milik customer %s — remove"
                          " DITOLAK, tidak disentuh", terminal_id,
                          customer_id)
                return False
            dbmod.set_customer_context(conn, int(customer_id))
            return self._remove_terminal(conn, terminal_id, provision_id,
                                         expect_owner, claimed_op,
                                         customer_id=customer_id)

    def _remove_terminal(self, conn, terminal_id: int,
                         provision_id: str | None,
                         expect_owner: str | None = None,
                         claimed_op: str | None = None, *,
                         customer_id) -> bool:
        """Hentikan + hapus terminal HANYA dengan bukti kepemilikan.

        True bila baris terminal tidak ada lagi (slot bebas). False bila
        baris DIPERTAHANKAN: dipegang operasi lain (TIDAK disentuh), atau
        helper gagal/tidak pasti (removal-pending, slot tetap terpakai).

        Klaim atomik (T1): lifecycle='stopping', op_owner='remove:<uuid>'
        hanya bila baris bebas (NULL), milik `expect_owner` (job yang
        membuatnya), atau removal-pending. Cleanup job (expect_owner=job)
        boleh mengambil baris NULL hanya selagi lifecycle 'provisioning'
        (belum pernah dipakai job lain). claimed_op: sudah diklaim
        pemulihan.

        B11/T3-2: customer_id WAJIB dan menjadi bagian setiap predikat.
        Baris milik customer lain / customer tidak diketahui -> TIDAK
        disentuh sama sekali (tidak dihapus, tidak ditandai)."""
        mapping = self._terminal_mapping(conn, terminal_id, customer_id)
        if mapping == "absent":
            return True
        if mapping == "mismatch":
            log.error("terminal %s: bukan milik customer %s — remove DITOLAK,"
                      " tidak disentuh", terminal_id, customer_id)
            return False
        cid = int(customer_id)
        spec = self.spec_for(conn, terminal_id, cid)
        if spec is None:
            log.error("terminal %s: spec customer %s tidak lengkap (customer"
                      " hilang?) — remove DITOLAK", terminal_id, cid)
            return False
        if provision_id is None or spec.provision_id != provision_id:
            # Tanpa bukti kepemilikan: TIDAK dihapus; ditandai 'failed' untuk
            # admin (paritas PR #4 S5) — HANYA bila tidak dipegang operasi
            # lain (job aktif / penghapusan), lewat UPDATE bersyarat.
            try:
                conn.execute(
                    "UPDATE mt5_terminals SET lifecycle='failed',"
                    " last_error=?, updated_at=? WHERE id=? AND"
                    " customer_id=? AND op_owner IS NULL",
                    ("kepemilikan tidak terbukti", _now_iso(), terminal_id,
                     cid))
                conn.commit()
            except Exception:  # noqa: BLE001
                conn.rollback()
            log.warning("terminal %s: kepemilikan tidak terbukti — TIDAK"
                        " dihapus", terminal_id)
            return False
        op = claimed_op
        if op is None:
            op = OWNER_REMOVE + uuid.uuid4().hex
            if expect_owner is None:
                cond = "(op_owner IS NULL OR op_owner=?)"
                params = (OWNER_PENDING,)
            else:
                cond = ("(op_owner=? OR op_owner=? OR (op_owner IS NULL AND"
                        " lifecycle='provisioning'))")
                params = (expect_owner, OWNER_PENDING)
            cur = conn.execute(
                "UPDATE mt5_terminals SET lifecycle='stopping', op_owner=?,"
                " updated_at=? WHERE id=? AND customer_id=? AND"
                " provision_id=? AND lifecycle <> 'stopping' AND " + cond,
                (op, _now_iso(), terminal_id, cid, provision_id) + params)
            conn.commit()
            if cur.rowcount != 1:
                gone = self._row_gone(conn, terminal_id)
                if not gone:
                    log.info("terminal %s dipegang operasi lain — remove"
                             " dilewati (tidak disentuh)", terminal_id)
                return gone
        prev = conn.execute("SELECT last_error FROM mt5_terminals WHERE id=?"
                            " AND customer_id=?", (terminal_id, cid)).fetchone()
        tries = _retry_count(prev["last_error"] if prev else None) + 1
        try:
            self._remove_settled(spec)
        except RETRIABLE as e:
            # F2: helper mungkin masih bekerja / keadaan tidak pasti ->
            # baris & slot DITAHAN (removal-pending), remove diulang oleh
            # pemulihan — dengan batas: setelah MAX_REMOVE_RETRIES -> admin.
            detail = f"{type(e).__name__}:{getattr(e, 'state', '')}"
            if tries > MAX_REMOVE_RETRIES:
                self._release_removal(conn, terminal_id, op,
                                      f"{EXHAUSTED_PREFIX}{tries - 1}:"
                                      f"{detail}", admin=True, customer_id=cid)
            else:
                self._release_removal(conn, terminal_id, op,
                                      f"{RETRY_PREFIX}{tries}:{detail}",
                                      customer_id=cid)
            return False
        except (OwnershipError, ProvisionError) as e:
            self._release_removal(conn, terminal_id, op,
                                  f"{REMOVE_FAILED_PREFIX}{type(e).__name__}",
                                  admin=True, customer_id=cid)
            return False
        cur = conn.execute(
            "DELETE FROM mt5_terminals WHERE id=? AND customer_id=? AND"
            " provision_id=? AND op_owner=? AND lifecycle='stopping'",
            (terminal_id, cid, provision_id, op))
        conn.commit()
        if cur.rowcount != 1:
            log.warning("terminal %s: klaim remove %s tidak berlaku lagi"
                        " (diambil pemulihan lain) — baris tidak dihapus di"
                        " sini", terminal_id, op)
            return self._row_gone(conn, terminal_id)
        return True

    def _row_gone(self, conn, terminal_id: int) -> bool:
        """B12/T3-3: 'baris sudah tidak ada' WAJIB dibaca tanpa customer
        context di koneksi ROLE SISTEM (B12.1) — di scope customer, baris
        yang (kini) milik customer lain tersembunyi RLS dan akan terbaca
        'tidak ada' (sukses palsu)."""
        with dbmod.system_conn(self._cfg) as sc:
            gone = sc.execute("SELECT 1 FROM mt5_terminals WHERE id=?",
                              (terminal_id,)).fetchone() is None
            sc.commit()
        return gone

    def _release_removal(self, conn, terminal_id: int, op: str, why: str,
                         admin: bool = False, *, customer_id: int) -> None:
        """Kembalikan klaim remove menjadi removal-pending (job baru tetap
        tidak dapat memakai baris). admin=True: tidak diulang otomatis,
        dilaporkan (log ERROR + audit_logs)."""
        try:
            conn.execute(
                "UPDATE mt5_terminals SET lifecycle='failed', op_owner=?,"
                " last_error=?, updated_at=? WHERE id=? AND customer_id=? AND"
                " op_owner=?",
                (OWNER_PENDING, why[:200], _now_iso(), terminal_id,
                 int(customer_id), op))
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            log.exception("tandai removal-pending terminal %s gagal",
                          terminal_id)
        if admin:
            log.error("terminal %s: penghapusan butuh admin (%s)",
                      terminal_id, why)
            try:
                import audit as auditmod
                auditmod.audit(conn, "terminal_remove_needs_admin",
                               customer_id=int(customer_id),
                               entity=f"mt5_terminal:{terminal_id}",
                               detail={"reason": why[:200]})
            except Exception:  # noqa: BLE001
                log.exception("audit terminal %s gagal", terminal_id)

    # --------------------------------------------------------- recovery
    def recover_expired(self, conn) -> int:
        """Lihat _recover_expired. B10/T3-1: dipanggil di jalur REQUEST
        (routes.py) — scope RLS customer pemanggil dipulihkan apa pun
        hasilnya."""
        # B12.1: pemindaian job kedaluwarsa = operasi SISTEM (lintas
        # customer) -> koneksi ROLE SISTEM sendiri; koneksi pemanggil tidak
        # disentuh. Cleanup tiap job berjalan dengan customer context job.
        sc = dbmod.get_system_db(self._cfg)
        with _caller_scope(sc):
            dbmod.set_customer_context(sc, None)
            return self._recover_expired(sc)

    def _recover_expired(self, conn) -> int:
        """Gagalkan + bersihkan job non-final yang melewati deadline
        (proses mati / restart). Klaim atomik: tiap job hanya
        dibersihkan SATU worker."""
        rows = conn.execute(
            "SELECT id FROM mt5_connect_jobs WHERE state NOT IN"
            " ('connected','failed') AND deadline_at < ?",
            (time.time(),)).fetchall()
        n = 0
        for r in rows:
            self.fail(conn, r["id"], "interrupted")
            n += 1
        # cleanup yang terputus di tengah jalan (proses mati) -> ulangi
        # (idempoten: penghapusan berbasis id + bukti kepemilikan)
        stuck = conn.execute(
            "SELECT id, error_class FROM mt5_connect_jobs WHERE"
            " state='failed' AND error_class LIKE ? AND deadline_at < ?",
            (CLEANUP_PREFIX + "%", time.time())).fetchall()
        for r in stuck:
            job = self.load(conn, r["id"])
            self.cleanup(conn, job)
            conn.execute("UPDATE mt5_connect_jobs SET error_class=? WHERE"
                         " id=? AND error_class LIKE ?",
                         (_cleanup_klass(r["error_class"]), r["id"],
                          CLEANUP_PREFIX + "%"))
            conn.commit()
            n += 1
        return n

    def _claim_lease(self) -> float:
        """Lama klaim pemulihan dianggap hidup: > dua putaran remove
        terlama (timeout + grace + inspect, dua kali karena remove ulang)."""
        c = self._cfg
        one = (float(getattr(c, "provisioner_timeout_seconds", 60)) +
               float(getattr(c, "provisioner_term_grace_seconds", 10)) +
               float(getattr(c, "provisioner_inspect_timeout_seconds", 15)))
        return max(120.0, 2 * one + 30.0)

    @staticmethod
    def _stale_owner_problem(r) -> str | None:
        """B11.2: None bila op_owner='job:X' pada terminal ini sah dimiliki
        job X (customer, terminal, provision sama); selain itu alasan."""
        if r["t_customer_id"] is None or r["j_customer_id"] is None:
            return "customer_id kosong"
        if int(r["t_customer_id"]) != int(r["j_customer_id"]):
            return "customer job berbeda"
        if r["j_terminal_id"] is None or int(r["j_terminal_id"]) != int(r["id"]):
            return "terminal_id job berbeda"
        if r["j_provision_id"] != r["t_provision_id"]:
            return "provision_id job berbeda"
        return None

    @staticmethod
    def _recovery_mapping_problem(conn, r) -> str | None:
        """B11.1: None bila kandidat pemulihan boleh diproses; selain itu
        alasan fail-closed. customer_id diambil dari baris terminal dan
        WAJIB konsisten dengan customer setiap job yang merujuk terminal
        itu (job customer lain / tanpa customer = mapping korup)."""
        if r["customer_id"] is None:
            return "customer_id terminal kosong"
        if not r["provision_id"]:
            return "provision_id kosong"
        cid = int(r["customer_id"])
        if conn.execute("SELECT 1 FROM customers WHERE id=?",
                        (cid,)).fetchone() is None:
            return "customer terminal tidak ada"
        # B12: SETIAP job yang merujuk terminal ini ATAU membawa
        # provision_id-nya wajib cocok seluruh identitasnya (customer,
        # terminal_id, provision_id). Kandidat tahap 2 selalu punya
        # provision_id -> NULL di sisi job TIDAK sah di sini.
        bad = conn.execute(
            "SELECT j.id, j.customer_id, j.terminal_id, j.provision_id FROM"
            " mt5_connect_jobs j WHERE (j.terminal_id=? OR j.provision_id=?)"
            " AND (j.customer_id IS NULL OR j.customer_id<>? OR"
            " j.terminal_id IS NULL OR j.terminal_id<>? OR"
            " j.provision_id IS NULL OR j.provision_id<>?) LIMIT 1",
            (int(r["id"]), r["provision_id"], cid, int(r["id"]),
             r["provision_id"])).fetchone()
        if bad is not None:
            if bad["customer_id"] is None or int(bad["customer_id"]) != cid:
                what = "customer berbeda"
            elif bad["terminal_id"] is None or \
                    int(bad["terminal_id"]) != int(r["id"]):
                what = "terminal_id berbeda"
            else:
                what = "provision_id berbeda"
            return "job %s merujuk terminal dengan %s" % (bad["id"], what)
        return None

    def retry_uncertain_removals(self, conn) -> int:
        """Lihat _retry_uncertain_removals. B10: scope pemanggil
        dipulihkan (sebelumnya dikosongkan paksa di akhir)."""
        # B12.1: pindai/validasi/klaim = koneksi ROLE SISTEM tanpa customer
        # context (job korup customer mana pun terlihat); penghapusan =
        # customer context terminal. Koneksi pemanggil tidak disentuh.
        sc = dbmod.get_system_db(self._cfg)
        with _caller_scope(sc):
            dbmod.set_customer_context(sc, None)
            return self._retry_uncertain_removals(sc)

    def _retry_uncertain_removals(self, conn) -> int:
        """F2/S1: pemulihan penghapusan terminal — berbasis BARIS TERMINAL
        (bukan hanya terminal milik job gagal): mencakup penghapusan dari
        cleanup job, unbind, dan klaim remove yang kedaluwarsa (worker mati).

        1. Pegangan job basi (job sudah final) dilepas / dijadikan
           removal-pending (job pembuat terminal).
        2. Kandidat: removal-pending + 'retry:<n>' ATAU 'remove:*' dengan
           lease kedaluwarsa. Klaim ATOMIK per baris (satu worker saja).
        3. remove_terminal(claimed_op) — bukti kepemilikan tetap wajib
           (provision_id baris = penanda helper). Batas retry -> admin.
        Terminal yang dipegang job AKTIF tidak pernah menjadi kandidat."""
        lease_cut = _iso_ago(self._claim_lease())
        n = 0
        # 1. Pegangan job basi. B11.1/B11.2: op_owner='job:X' hanya SAH bila
        #    job X merujuk terminal INI dengan identitas lengkap yang sama:
        #    customer_id, terminal_id, dan provision_id (NULL = NULL untuk
        #    terminal lama). Kandidat dipilih hanya dari op_owner + state
        #    final; kecocokan identitas diperiksa eksplisit (ditolak ->
        #    log.error, tidak disentuh) DAN diulang atomik di predikat
        #    UPDATE. Kegagalan satu baris tidak menghentikan baris lain
        #    maupun tahap 2.
        try:
            stale = conn.execute(
                "SELECT t.id, t.op_owner, t.customer_id AS t_customer_id,"
                " t.provision_id AS t_provision_id,"
                " j.id AS j_id, j.customer_id AS j_customer_id,"
                " j.terminal_id AS j_terminal_id,"
                " j.provision_id AS j_provision_id, j.created_terminal FROM"
                " mt5_terminals t JOIN mt5_connect_jobs j ON t.op_owner ="
                " ? || j.id"
                " WHERE j.state IN ('connected','failed') AND"
                " (j.error_class IS NULL OR j.error_class NOT LIKE ?)",
                (OWNER_JOB, CLEANUP_PREFIX + "%")).fetchall()
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            log.exception("pemindaian pemilik basi gagal")
            stale = []
        for r in stale:
            why = self._stale_owner_problem(r)
            if why is not None:
                log.error("terminal %s: pemilik basi %s DITOLAK (%s) — tidak"
                          " disentuh", r["id"], r["op_owner"], why)
                continue
            tcid = int(r["t_customer_id"])
            same_job = (" AND EXISTS (SELECT 1 FROM mt5_connect_jobs j WHERE"
                        " j.id=? AND j.customer_id=mt5_terminals.customer_id"
                        " AND j.terminal_id=mt5_terminals.id AND"
                        " (j.provision_id=mt5_terminals.provision_id OR"
                        " (j.provision_id IS NULL AND"
                        " mt5_terminals.provision_id IS NULL)) AND"
                        " j.state IN ('connected','failed'))")
            try:
                if r["created_terminal"]:
                    cur = conn.execute(
                        "UPDATE mt5_terminals SET op_owner=?,"
                        " lifecycle='failed', last_error=?, updated_at=?"
                        " WHERE id=? AND customer_id=? AND op_owner=?"
                        + same_job,
                        (OWNER_PENDING, RETRY_PREFIX + "0:stale-owner",
                         _now_iso(), r["id"], tcid, r["op_owner"], r["j_id"]))
                else:
                    cur = conn.execute(
                        "UPDATE mt5_terminals SET op_owner=NULL, updated_at=?"
                        " WHERE id=? AND customer_id=? AND op_owner=?"
                        + same_job,
                        (_now_iso(), r["id"], tcid, r["op_owner"], r["j_id"]))
                if cur.rowcount != 1:
                    log.warning("terminal %s: pemilik basi berubah sebelum"
                                " dilepas — tidak disentuh", r["id"])
                conn.commit()
            except Exception:  # noqa: BLE001
                conn.rollback()
                log.exception("pelepasan pemilik basi terminal %s gagal",
                              r["id"])
        # 2. Kandidat penghapusan — kolom eksplisit dari baris TERMINAL.
        pred = ("((op_owner=? AND lifecycle='failed' AND last_error LIKE ?)"
                " OR (op_owner LIKE ? AND lifecycle='stopping' AND"
                " (updated_at IS NULL OR updated_at < ?)))")
        pp = (OWNER_PENDING, RETRY_PREFIX + "%", OWNER_REMOVE + "%",
              lease_cut)
        try:
            rows = conn.execute(
                "SELECT id, customer_id, provision_id, op_owner, lifecycle,"
                " last_error FROM mt5_terminals"
                " WHERE provision_id IS NOT NULL AND " + pred, pp).fetchall()
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            log.exception("pemindaian pemulihan terminal gagal")
            return n
        for r in rows:
            try:
                why = self._recovery_mapping_problem(conn, r)
                if why is not None:
                    log.error("terminal %s: pemulihan DITOLAK (%s) — helper"
                              " tidak dipanggil, baris tidak disentuh",
                              r["id"], why)
                    conn.commit()
                    continue
                tcid = int(r["customer_id"])
                op = OWNER_REMOVE + uuid.uuid4().hex
                # B12: klaim ATOMIK dengan identitas yang sama seperti
                # validasi (scope SISTEM — job korup customer lain ikut
                # terlihat): customer masih ada dan TIDAK ADA job yang
                # merujuk terminal / provision ini dengan identitas lain.
                cur = conn.execute(
                    "UPDATE mt5_terminals SET lifecycle='stopping', op_owner=?,"
                    " updated_at=? WHERE id=? AND customer_id=? AND"
                    " provision_id=? AND " + pred + " AND " + _NO_CORRUPT_JOB,
                    (op, _now_iso(), r["id"], tcid,
                     r["provision_id"]) + pp)
                claimed = cur.rowcount == 1
                conn.commit()
                if not claimed:
                    log.warning("terminal %s: klaim pemulihan ditolak (keadaan"
                                " / mapping berubah setelah validasi) — tidak"
                                " disentuh", r["id"])
                    continue
                removed = self.remove_terminal(
                    conn, int(r["id"]), r["provision_id"], claimed_op=op,
                    customer_id=tcid)
                dbmod.set_customer_context(conn, None)     # kembali: sistem
                if not removed:
                    self._unclaim_if_untouched(conn, r, op)
                    continue
                n += 1
                dbmod.set_customer_context(conn, tcid)
                job = conn.execute(
                    "SELECT * FROM mt5_connect_jobs WHERE terminal_id=?"
                    " AND customer_id=? AND provision_id=? AND"
                    " created_customer=1 AND state='failed'"
                    " ORDER BY created_at DESC LIMIT 1",
                    (r["id"], tcid, r["provision_id"])).fetchone()
                if job is not None:
                    self._drop_customer_if_empty(conn, job)
                dbmod.set_customer_context(conn, None)     # kembali: sistem
            except Exception:  # noqa: BLE001
                conn.rollback()
                log.exception("retry remove terminal %s gagal", r["id"])
                try:
                    dbmod.set_customer_context(conn, None)
                except Exception:  # noqa: BLE001
                    log.exception("scope sistem pemulihan tidak dapat"
                                  " dipasang ulang — pemulihan dihentikan")
                    return n
        return n

    @staticmethod
    def _unclaim_if_untouched(conn, r, op: str) -> None:
        """B12: remove_terminal menolak SETELAH klaim (mis. mapping berubah
        di antara klaim dan penghapusan) dan tidak mengubah baris -> klaim
        dikembalikan PERSIS ke keadaan sebelum klaim (lifecycle, op_owner,
        last_error). Hanya bila baris masih dipegang klaim [op] ini."""
        try:
            cur = conn.execute(
                "UPDATE mt5_terminals SET lifecycle=?, op_owner=?,"
                " last_error=? WHERE id=? AND op_owner=? AND"
                " lifecycle='stopping'",
                (r["lifecycle"], r["op_owner"], r["last_error"], r["id"], op))
            conn.commit()
            if cur.rowcount == 1:
                log.error("terminal %s: pemulihan dibatalkan setelah klaim —"
                          " keadaan dikembalikan", r["id"])
        except Exception:  # noqa: BLE001
            conn.rollback()
            log.exception("pengembalian klaim terminal %s gagal", r["id"])
