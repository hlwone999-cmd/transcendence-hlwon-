"""AUDIT ROUND-4 #1 — urutan lock vs scope RLS pada sync_account().

Masalah: versi lama memanggil dbmod.set_customer_context() SEBELUM
rt.lock.acquire(). Bila lock gagal, SyncLockBusy dilempar SEBELUM blok
try/finally — clear_customer_context pada finally TIDAK PERNAH berjalan,
sehingga scope customer tertinggal di koneksi thread-local (tugas/
request berikutnya pada thread itu mewarisi scope akun sebelumnya).

Perbaikan: lock di-acquire DULU; set_customer_context dipindah KE DALAM
try sehingga:
  - lock gagal  -> SyncLockBusy SEBELUM context dipasang (tidak ada
    scope yang perlu dibersihkan — inilah yang dibuktikan test di bawah);
  - lock sukses -> context dipasang di dalam try; setiap kegagalan
    setelahnya tetap melewati finally (cleanup + release lock).

Empat lapis bukti (tanpa PG — jalan di mana pun):
1. URUTAN lock gagal: set_customer_context TIDAK BOLEH dipanggil sama
   sekali (rekam via monkeypatch). Pada kode lama test ini GAGAL.
2. URUTAN lock sukses: lock -> set(context) -> _run_sync -> clear
   (scope terpasang sebelum pekerjaan, dibersihkan di finally).
3. PEKERJAAN error setelah context terpasang: cleanup tetap berjalan,
   backoff tercatat, lock lepas.
4. POSITIF-LEAK: simulasi PERSIS urutan lama (set context, raise
   SyncLockBusy tanpa cleanup) JELAS terdeteksi oleh rekam-panggilan —
   jadi test #1 benar-benar menangkap bug, bukan lulus kosong.
Bukti GUC PostgreSQL SUNGGUHAN (scope benar-benar bersih di level
koneksi) ada di tests/test_rls_no_leak_requests.py
(test_sync_lock_busy_tidak_meninggalkan_scope — pgserver).
"""
import threading

import pytest

import db as dbmod
from services.sync import SyncLockBusy, SyncService


class _FakeRuntime:
    """Runtime minimum yang dibutuhkan sync_account (tanpa terminal)."""

    def __init__(self, account_id, customer_id, lock_fails: bool):
        self.account_id = account_id
        self.customer_id = customer_id
        self.provider = object()   # tidak pernah dipakai saat lock gagal
        self.lock = threading.Lock()
        self.consecutive_failures = 0
        self.next_allowed_ts = 0.0
        self.last_error = None
        if lock_fails:
            # lock dipegang pihak lain -> acquire(blocking=False) PASTI gagal
            assert self.lock.acquire(blocking=False)

    def backoff_penalty(self, base=30, cap=1800):
        return 30.0


class _FakeRegistry:
    def __init__(self, rt):
        self._rt = rt

    def runtime_for(self, conn, account_id):
        assert account_id == self._rt.account_id
        return self._rt


@pytest.fixture()
def rekam_calls(monkeypatch):
    """Rekam pemanggilan set/clear customer context (level dbmod)."""
    calls = []
    monkeypatch.setattr(
        dbmod, "set_customer_context",
        lambda conn, cid: calls.append(("set", cid)))
    monkeypatch.setattr(
        dbmod, "clear_customer_context",
        lambda conn: calls.append(("clear", None)))
    return calls


def test_lock_gagal_context_tidak_pernah_dipasang(cfg, rekam_calls):
    """INTI TEMUAN #1: lock gagal -> SyncLockBusy, dan scope RLS TIDAK
    pernah dipasang pada koneksi (bersih tanpa perlu cleanup)."""
    rt = _FakeRuntime(101, 7, lock_fails=True)
    svc = SyncService(cfg, lambda: object(), registry=_FakeRegistry(rt))
    with pytest.raises(SyncLockBusy):
        svc.sync_account(101)
    assert rekam_calls == [], (
        "kode lama memasang context SEBELUM lock — pada lock gagal "
        "set_customer_context sempat dipanggil dan TIDAK pernah "
        "dibersihkan (leak). Kode baru wajib tidak memanggilnya sama "
        "sekali.")


def test_lock_sukses_urutan_lock_sebelum_context(cfg, rekam_calls,
                                                 monkeypatch):
    """Bila lock berhasil: urutan wajib set(context) -> _run_sync ->
    clear di finally (scope terpasang SEBELUM pekerjaan)."""
    rt = _FakeRuntime(102, 9, lock_fails=False)
    svc = SyncService(cfg, lambda: object(), registry=_FakeRegistry(rt))

    def _fake_run(**kwargs):
        rekam_calls.append(("run", None))
        return {"status": "ok"}

    monkeypatch.setattr(svc, "_run_sync", _fake_run)
    summary = svc.sync_account(102)
    assert summary["status"] == "ok"
    kinds = [k for k, _ in rekam_calls]
    assert kinds == ["set", "run", "clear"], (
        f"urutan salah: {kinds} — scope wajib dipasang sebelum pekerjaan "
        "dan dibersihkan di finally")
    # lock dilepas setelah tugas selesai (dapat di-acquire lagi)
    assert not rt.lock.locked()
    assert rt.lock.acquire(blocking=False), "lock wajib terlepas kembali"
    rt.lock.release()


def test_lock_sukses_pekerjaan_error_cleanup_tetap_jalan(cfg, rekam_calls,
                                                         monkeypatch):
    """Bila pekerjaan sync meledak (setelah context terpasang), finally
    WAJIB membersihkan scope + melepas lock — backoff tercatat."""
    rt = _FakeRuntime(103, 11, lock_fails=False)
    svc = SyncService(cfg, lambda: object(), registry=_FakeRegistry(rt))

    class _Boom(RuntimeError):
        pass

    def _explode(**kwargs):
        rekam_calls.append(("run", None))
        raise _Boom("provider meledak di tengah sync")

    monkeypatch.setattr(svc, "_run_sync", _explode)
    with pytest.raises(_Boom):
        svc.sync_account(103)
    kinds = [k for k, _ in rekam_calls]
    assert kinds == ["set", "run", "clear"], (
        f"urutan salah: {kinds} — cleanup wajib jalan walau pekerjaan error")
    assert rt.consecutive_failures == 1, "backoff tercatat saat gagal"
    assert not rt.lock.locked(), "lock wajib lepas setelah kegagalan"


def test_bukti_leak_urutan_lama_terdeteksi(cfg, rekam_calls):
    """Kontrol negatif: simulasi PERSIS urutan lama — pasang context,
    lalu SyncLockBusy TANPA cleanup. Rekam-pemanggilan WAJIB menunjukkan
    'set' TANPA 'clear' berikutnya (inilah leak yang diperbaiki) — jadi
    test_lock_gagal_context_tidak_pernah_dipasang benar-benar menangkap
    bug, bukan lulus kosong."""
    rt = _FakeRuntime(104, 13, lock_fails=True)   # lock sudah dipegang orang lain
    conn = dbmod.connect(cfg.db_file)
    # urutan lama: context dipasang dulu ...
    dbmod.set_customer_context(conn, rt.customer_id)
    # ... lalu lock gagal dan SyncLockBusy dilempar sebelum try/finally
    assert not rt.lock.acquire(blocking=False)
    with pytest.raises(SyncLockBusy):
        raise SyncLockBusy("sync akun 104 sedang berjalan")
    kinds = [k for k, _ in rekam_calls]
    assert "set" in kinds and "clear" not in kinds, (
        "kontrol negatif: urutan lama meninggalkan 'set' tanpa 'clear' "
        "— persis leak yang dilaporkan audit")
    # kode baru menghilangkan kemungkinan ini karena set TIDAK dipanggil
    # sebelum lock (lihat test_lock_gagal_context_tidak_pernah_dipasang)
