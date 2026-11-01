"""Scheduler sinkronisasi berkala (thread daemon, tanpa dependensi eksternal).

- MT5_ENABLED=false -> thread idle, tidak menyentuh MT5.
- Setiap siklus: jalankan sync incremental; error dicatat di state,
  TIDAK pernah mematikan backend.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class SyncScheduler:
    def __init__(self, config, sync_service):
        self._cfg = config
        self._svc = sync_service
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._state = {
            "enabled": False, "running": False, "last_run": None,
            "next_run": None, "last_status": None, "last_error": None,
            "runs": 0,
        }

    # ------------------------------------------------------------- kontrol
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mt5-sync", daemon=True)
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

    # --------------------------------------------------------------- loop
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
                          last_error=summary.get("error"), runs=self._state["runs"] + 1)
            except Exception as e:  # noqa: BLE001 — jangan pernah matikan loop
                log.warning("sync scheduler gagal: %s", e)
                self._set(running=False, last_status="error", last_error=str(e),
                          runs=self._state["runs"] + 1)
            self._set(next_run=self._iso_now(interval))
            if self._stop.wait(timeout=interval):
                break

    # -------------------------------------------------------------- util
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
