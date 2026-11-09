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
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone

import db as dbmod
from services.provisioner import (OwnershipError, ProvisionError,
                                  TerminalSpec)
from services.terminals import (probe_ro_session, probe_rpyc_session,
                                terminal_token)
from services.verification import live_verify_account

log = logging.getLogger(__name__)

FINAL = ("connected", "failed")
CLEANUP_PREFIX = "cleanup:"


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


class ConnectCoordinator:
    def __init__(self, app, cfg, registry, provisioner):
        self._app = app
        self._cfg = cfg
        self._registry = registry
        self.provisioner = provisioner

    # ----------------------------------------------------------- helpers
    def spec_for(self, conn, terminal_id: int) -> TerminalSpec | None:
        row = conn.execute(
            "SELECT t.rpyc_port, t.provision_id, t.rpc_mode, c.code"
            " FROM mt5_terminals t JOIN customers c ON c.id=t.customer_id"
            " WHERE t.id=?", (terminal_id,)).fetchone()
        if row is None:
            return None
        return TerminalSpec(code=row["code"], port=int(row["rpyc_port"]),
                            provision_id=row["provision_id"],
                            rpc_mode=row["rpc_mode"] or "classic")

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
        cur = conn.execute(
            "UPDATE mt5_connect_jobs SET token_issued_at=? WHERE id=? AND"
            " state='connected' AND token_issued_at IS NULL",
            (_now_iso(), job_id))
        conn.commit()
        return cur.rowcount == 1

    # ------------------------------------------------------------ state
    def _set(self, conn, job_id: str, state: str, terminal_id=None,
             lifecycle: str | None = None) -> None:
        cur = conn.execute(
            "UPDATE mt5_connect_jobs SET state=?, updated_at=? WHERE id=?"
            " AND state NOT IN ('connected','failed')",
            (state, _now_iso(), job_id))
        if cur.rowcount != 1:
            conn.rollback()
            raise JobTimeout("job sudah diklaim pemulihan")
        if terminal_id is not None and lifecycle:
            conn.execute("UPDATE mt5_terminals SET lifecycle=?, updated_at=?"
                         " WHERE id=?", (lifecycle, _now_iso(), terminal_id))
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
        conn = dbmod.get_db(self._cfg)
        job = self.load(conn, job_id)
        if job is None or job["state"] in FINAL:
            return job["state"] if job else "missing"
        cid = int(job["customer_id"])
        try:
            dbmod.set_customer_context(conn, cid)
            spec = self.spec_for(conn, int(job["terminal_id"]))
            if spec is None:
                raise ProvisionError("baris terminal hilang")
            tid = int(job["terminal_id"])
            trow = conn.execute("SELECT lifecycle FROM mt5_terminals"
                                " WHERE id=?", (tid,)).fetchone()
            if job["created_terminal"]:
                self._set(conn, job_id, "provisioning", tid, "provisioning")
                self.provisioner.install(spec)
            needs_start = bool(job["created_terminal"]) or (
                trow["lifecycle"] in ("stopped", "failed")
                and spec.provision_id is not None)
            if needs_start:
                self._remaining(job)
                self._set(conn, job_id, "starting", tid, "starting")
                self.provisioner.start(spec)
                while not self._healthy(spec):
                    self._remaining(job)
                    time.sleep(0.2)
            self._set(conn, job_id, "verifying", tid, "verifying")
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
            cur = conn.execute(
                "UPDATE mt5_connect_jobs SET state='connected', updated_at=?"
                " WHERE id=? AND state NOT IN ('connected','failed')",
                (_now_iso(), job_id))
            if cur.rowcount != 1:
                conn.rollback()
                return "failed"
            conn.execute("UPDATE mt5_accounts SET state='active',"
                         " unbound_at=NULL WHERE id=?", (job["account_id"],))
            conn.execute("UPDATE mt5_terminals SET lifecycle='connected',"
                         " status='ready', last_error=NULL, updated_at=?"
                         " WHERE id=?", (_now_iso(), tid))
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
                    app.extensions["mt5_sync"].sync_account(
                        acc_id, mode="initial")
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
        """Hapus HANYA resource milik job (Tahap 3A/E)."""
        cid = job["customer_id"]
        acc_id = job["account_id"]
        if acc_id is not None:
            try:
                self._registry.evict(int(acc_id))
            except Exception:  # noqa: BLE001
                pass
        try:
            if cid is not None:
                dbmod.set_customer_context(conn, int(cid))
            if acc_id is not None:
                conn.execute("DELETE FROM mt5_credentials WHERE account_id=?",
                             (acc_id,))
                if job["created_account"]:
                    conn.execute("DELETE FROM mt5_accounts WHERE id=? AND"
                                 " customer_id=?", (acc_id, cid))
                else:
                    conn.execute(
                        "UPDATE mt5_accounts SET state=? WHERE id=? AND"
                        " state='pending'",
                        (job["prev_account_state"] or "unbound", acc_id))
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            log.exception("cleanup akun job %s gagal", job["id"])
        if job["created_terminal"] and job["terminal_id"] is not None:
            self.remove_terminal(conn, int(job["terminal_id"]),
                                 job["provision_id"])
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

    def remove_terminal(self, conn, terminal_id: int,
                        provision_id: str | None) -> bool:
        """Hentikan + hapus terminal HANYA dengan bukti kepemilikan.

        True bila terminal benar-benar dihapus (slot bebas). False bila
        kepemilikan tak terbukti / helper gagal -> baris DIPERTAHANKAN
        (lifecycle 'failed', slot tetap terpakai) untuk admin.
        """
        spec = self.spec_for(conn, terminal_id)
        if spec is None:
            return True
        if provision_id is None or spec.provision_id != provision_id:
            self._mark_failed(conn, terminal_id, "kepemilikan tidak terbukti")
            return False
        try:
            conn.execute("UPDATE mt5_terminals SET lifecycle='stopping'"
                         " WHERE id=?", (terminal_id,))
            conn.commit()
            self.provisioner.remove(spec)
        except (OwnershipError, ProvisionError) as e:
            self._mark_failed(conn, terminal_id, type(e).__name__)
            return False
        conn.execute("DELETE FROM mt5_terminals WHERE id=? AND provision_id=?",
                     (terminal_id, provision_id))
        conn.commit()
        return True

    @staticmethod
    def _mark_failed(conn, terminal_id: int, why: str) -> None:
        try:
            conn.execute("UPDATE mt5_terminals SET lifecycle='failed',"
                         " last_error=?, updated_at=? WHERE id=?",
                         (why[:200], _now_iso(), terminal_id))
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()

    # --------------------------------------------------------- recovery
    def recover_expired(self, conn) -> int:
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
                         " id=?", (str(r["error_class"])[len(CLEANUP_PREFIX):],
                                   r["id"]))
            conn.commit()
            n += 1
        return n
