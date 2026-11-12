"""Scheduler sinkronisasi multi-customer (brief §6).

- MultiAccountScheduler : PRODUKSI. Satu dispatcher thread; tiap interval
  mengirim 1 worker per akun aktif (maks 4) ke thread pool. Lock per
  akun, backoff eksponensial per akun (runtime registry), kegagalan satu
  customer TIDAK menghentikan customer lain, tanpa duplikasi data
  (idempoten oleh UNIQUE constraints + dedup sync state).
- SyncScheduler         : LEGACY (deploy lama / test lama) — satu akun
  global, dipertahankan apa adanya.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


class MultiAccountScheduler:
    """Scheduler per-akun: isolasi kegagalan + backoff + lock per akun."""

    def __init__(self, config, sync_service, registry):
        self._cfg = config
        self._svc = sync_service
        self._registry = registry
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._state: dict = {
            "kind": "multi-account",
            "enabled": False, "running": False, "last_run": None,
            "next_run": None, "runs": 0,
            "per_account": {},   # account_id -> {last_status,last_error,runs}
        }
        self._pool: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------- kontrol
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, self._cfg.max_customers),
            thread_name_prefix="mt5-sync")
        self._thread = threading.Thread(target=self._loop,
                                        name="mt5-sync-dispatch", daemon=True)
        with self._state_lock:
            self._state["enabled"] = self._cfg.mt5_enabled
            self._state["next_run"] = self._iso_now()
        self._thread.start()
        log.info("scheduler multi-akun aktif (interval %ss, enabled=%s, "
                 "maks %d customer)", self._cfg.mt5_sync_interval_seconds,
                 self._cfg.mt5_enabled, self._cfg.max_customers)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def state(self) -> dict:
        with self._state_lock:
            st = dict(self._state)
            st["per_account"] = dict(self._state["per_account"])
            return st

    # --------------------------------------------------------------- loop
    def _loop(self) -> None:
        interval = max(30, self._cfg.mt5_sync_interval_seconds)
        while not self._stop.is_set():
            if not self._cfg.mt5_enabled:
                if self._stop.wait(timeout=5):
                    break
                continue
            self._dispatch_cycle()
            self._set(next_run=self._iso_now(interval))
            if self._stop.wait(timeout=interval):
                break

    def _dispatch_cycle(self) -> None:
        """Kirim satu worker per akun aktif — kegagalan terisolasi."""
        import db as dbmod
        now_mono = time.monotonic()
        try:
            # B12.1: daftar akun aktif & runtime = operasi SISTEM (lintas
            # customer) -> koneksi ROLE SISTEM thread scheduler (tanpa
            # customer context). Tiap sync men-scope customer akunnya.
            conn = dbmod.get_system_db(self._cfg)
            dbmod.set_customer_context(conn, None)
            account_ids = self._registry.active_account_ids(
                conn, limit=self._cfg.max_customers)
        except Exception:  # noqa: BLE001 — DB bermasalah: coba lagi nanti
            log.exception("scheduler gagal membaca daftar akun")
            return
        for account_id in account_ids:
            if self._stop.is_set():
                return
            try:
                rt = self._registry.runtime_for(conn, account_id)
            except Exception as e:  # noqa: BLE001 — provider/gagal dekripsi
                log.warning("runtime akun %d gagal dibuat: %s: %s",
                            account_id, type(e).__name__, e)
                self._note(account_id, "error", "runtime gagal dibuat")
                continue
            if rt.lock.locked():
                continue  # sync sebelumnya masih jalan — jangan dobel
            if now_mono < rt.next_allowed_ts:
                continue  # masih dalam masa backoff
            self._pool.submit(self._worker, account_id)

    def _worker(self, account_id: int) -> None:
        """Worker 1 akun. Error APAPUN tidak boleh menyentuh akun lain."""
        try:
            import db as dbmod
            sc = dbmod.get_system_db(self._cfg)     # B12.1: role sistem
            dbmod.set_customer_context(sc, None)
            summary = self._svc.sync_account(account_id, mode="incremental",
                                             conn=sc)
            self._note(account_id, summary.get("status", "error"),
                       summary.get("error"))
        except Exception as e:  # noqa: BLE001
            self._note(account_id, "error", f"{type(e).__name__}: {e}")

    def _note(self, account_id: int, status: str, error: str | None) -> None:
        with self._state_lock:
            self._state["running"] = False
            self._state["last_run"] = self._iso_now()
            self._state["runs"] += 1
            per = dict(self._state["per_account"])
            rec = dict(per.get(str(account_id), {"runs": 0}))
            rec.update({"last_status": status,
                        "last_error": error if status != "ok" else None,
                        "runs": rec.get("runs", 0) + 1})
            per[str(account_id)] = rec
            self._state["per_account"] = per

    def _set(self, **kw) -> None:
        with self._state_lock:
            self._state.update(kw)

    # -------------------------------------------------------------- util
    @staticmethod
    def _iso_now(offset: int = 0) -> str:
        dt = datetime.now(timezone.utc)
        if offset:
            dt = dt + timedelta(seconds=offset)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class SyncScheduler:
    """LEGACY: scheduler satu akun global (dipertahankan utk kompatibilitas)."""

    def __init__(self, config, sync_service):
        self._cfg = config
        self._svc = sync_service
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._state = {
            "kind": "legacy-single-account",
            "enabled": False, "running": False, "last_run": None,
            "next_run": None, "last_status": None, "last_error": None,
            "runs": 0,
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mt5-sync",
                                        daemon=True)
        self._set(enabled=self._cfg.mt5_enabled,
                  next_run=self._iso_now())
        self._thread.start()
        log.info("scheduler sync aktif (interval %ss, enabled=%s)",
                 self._cfg.mt5_sync_interval_seconds, self._cfg.mt5_enabled)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def state(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def _loop(self) -> None:
        interval = max(30, self._cfg.mt5_sync_interval_seconds)
        while not self._stop.is_set():
            if not self._cfg.mt5_enabled:
                if self._stop.wait(timeout=5):
                    break
                continue
            self._set(running=True, last_run=self._iso_now())
            try:
                summary = self._svc.sync(mode="incremental")
                self._set(running=False, last_status=summary.get("status"),
                          last_error=summary.get("error"),
                          runs=self._state["runs"] + 1)
            except Exception as e:  # noqa: BLE001 — jangan pernah matikan loop
                log.warning("sync scheduler gagal: %s", e)
                self._set(running=False, last_status="error",
                          last_error=str(e), runs=self._state["runs"] + 1)
            self._set(next_run=self._iso_now(interval))
            if self._stop.wait(timeout=interval):
                break

    @staticmethod
    def _iso_now(offset: int = 0) -> str:
        dt = datetime.now(timezone.utc)
        if offset:
            from datetime import timedelta
            dt = dt + timedelta(seconds=offset)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _set(self, **kw) -> None:
        with self._state_lock:
            self._state.update(kw)
