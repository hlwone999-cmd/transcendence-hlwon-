# Hasil Test — Perbaikan Audit Round-4

> Branch: `fix/audit4-final-hardening` (dari tip round-3 `15e2f5a`).
> Dua temuan audit independen: (1) urutan lock vs scope RLS pada
> `sync_account()`, (2) backup `pg_dump | gzip` tanpa pipefail +
> rollback tidak boleh memakai backup lama saat run gagal. Log lengkap
> di paket deliverable (SHA256SUMS.txt). **179 passed, 1 skipped**
> (skip = test live-sudo yang memang tidak tersedia di sandbox; jalankan
> di CI ubuntu-latest). Baseline sebelum round-4: **157 passed,
> 1 skipped** — tidak ada test lama yang dihapus; 22 test baru/tersesuaikan
> ditambahkan.

## TEMUAN 1 — `set_customer_context()` sebelum `rt.lock.acquire()`

**Temuan.** `sync_account()` lama memasang scope RLS dulu, baru
meng-acquire lock. Bila lock gagal, `SyncLockBusy` dilempar SEBELUM
blok try/finally — `clear_customer_context()` di finally TIDAK PERNAH
berjalan, scope customer tertinggal di koneksi thread-local, dan
tugas/request berikutnya pada thread itu mewarisi scope akun sebelumnya.

**Perbaikan** (`backend/services/sync.py` + temuan lanjutan):
- Urutan dibalik: lock DULU; `set_customer_context()` dipindah KE DALAM
  try. Lock gagal → `SyncLockBusy` sebelum scope terpasang (tidak ada
  yang perlu dibersihkan). Lock sukses → setiap kegagalan setelahnya
  tetap melewati finally (cleanup + release lock).
- **Regression test menyingkap leak kedua** pada jalur yang sama:
  `TerminalRegistry._account_config()` (dipanggil `runtime_for` saat
  cache-miss) memasang scope untuk membaca `mt5_credentials` dan TIDAK
  memulihkannya — jadi walau urutan di sync.py sudah benar, panggilan
  pertama runtime_for tetap meninggalkan scope. Diperbaiki: scope
  pemanggil DIPULIHKAN di `finally` (bila pemanggil sudah di-scope,
  scope itu dipasang kembali apa adanya).
- Pendukung (`backend/db.py`): `PostgresConn.set_customer_context(None)`
  kini soft-clear GUC transaksi berjalan (`set_config('', is_local=true)`)
  TANPA rollback — memulihkan scope tanpa mengganggu transaksi pemanggil
  (beda dengan `clear_customer_context()` yang me-rollback untuk
  teardown).

**Test** (22 lulus — 5 baru + penyesuaian kontrol negatif):
- `tests/test_sync_scope_lock.py` (4, jalan di mana pun):
  - `test_lock_gagal_context_tidak_pernah_dipasang` — INTI: lock gagal →
    `SyncLockBusy` dan `set_customer_context` TIDAK pernah dipanggil
    (rekam via monkeypatch). **Mutation check: dengan kode LAMA test ini
    GAGAL** (context sempat dipasang) — test benar-benar menangkap bug.
  - `test_lock_sukses_urutan_lock_sebelum_context` — urutan
    set(context) → _run_sync → clear di finally; lock terlepas kembali.
  - `test_lock_sukses_pekerjaan_error_cleanup_tetap_jalan` — pekerjaan
    meledak setelah context terpasang → cleanup jalan, backoff tercatat,
    lock lepas.
  - `test_bukti_leak_urutan_lama_terdeteksi` — kontrol negatif: simulasi
    PERSIS urutan lama (set context → SyncLockBusy tanpa cleanup) JELAS
    terdeteksi — test #1 bukan lulus kosong.
- `tests/test_rls_no_leak_requests.py::test_sync_lock_busy_tidak_
  meninggalkan_scope` (PostgreSQL SUNGGUHAN via pgserver): lock gagal →
  memori Python `None` + `current_setting('app.customer_id')` kosong
  walau transaksi baru di-commit pada koneksi yang sama; kontrol negatif
  memasang scope sengaja dan detektor WAJIB melihatnya (memori + GUC).
  Test ini juga membuktikan perbaikan `_account_config` (runtime_for
  cache-miss tidak lagi meninggalkan scope).

## TEMUAN 2 — backup `pg_dump | gzip` tanpa pipefail + rollback pakai backup lama

**Temuan.** Backup lama di workflow hanya `set -e`: kegagalan `pg_dump`
TERSEMBUNYI (exit pipeline = exit gzip), dump kosong/terpotong terbit
dengan nama final, dan `LAST_PRE_DEPLOY` ikut diperbarui. Rollback
berikutnya membaca `LAST_PRE_DEPLOY` → me-restore dump rusak/lama.

**Perbaikan** (`backend/deploy/backup_db.sh` baru + workflow +
`rollback.sh`):
1. Logika backup dipindah ke `backend/deploy/backup_db.sh` (dikirim
   workflow via `bash -s`, pola sama dengan rollback.sh) agar bisa
   DIJALANKAN sungguhan dalam test — bukan hanya dicek statik.
2. `set -euo pipefail`: kegagalan di posisi mana pun dalam pipeline
   menghentikan deployment.
3. Dump ke file sementara `.part` (awali titik, umask 077 → 600):
   nama final tidak pernah berisi file belum tervalidasi.
4. Validasi berlapis SEBELUM publikasi: `gzip -t` (CRC) + ukuran
   dekompresi > 0 (gzip kosong adalah gzip VALID — `gzip -t` saja tidak
   cukup!) + marker header `PostgreSQL database dump`. Pembaca validasi
   menghabiskan seluruh stdin → aman SIGPIPE di bawah pipefail.
5. Publikasi atomik `mv .part → final`; `LAST_PRE_DEPLOY` ditulis HANYA
   setelah backup tervalidasi.
6. `trap rm -f` membersihkan `.part` pada kegagalan apa pun.
7. **Rollback tidak memakai backup lama**: runner mengekspor
   `PRE_DEPLOY_STAMP` (stamp run ini) dan rollback.sh hanya me-restore
   dump `pre-deploy-$PRE_DEPLOY_STAMP.sql.gz` yang lolos validasi ulang.
   Bila run ini gagal backup → rollback MENOLAK semuanya (sistem masih
   pra-deploy — migrasi belum jalan; intervensi manual). `LAST_PRE_DEPLOY`
   tidak lagi menjadi dasar keputusan restore.

**Test** (`tests/test_backup_script.py` 12 + `tests/test_rollback_script.py`
10 + `tests/test_deploy_backup_permission.py` 7 — semua skrip dijalankan
sungguhan dengan stub pg_dump/gzip/sudo pada PATH):
- Sukses: `.part` → tervalidasi → final 600, `LAST_PRE_DEPLOY` = stamp
  run ini, `.part` tidak tersisa.
- **pg_dump gagal di tengah aliran (exit 1)** → pipeline gagal (pipefail),
  TANPA file final, `LAST_PRE_DEPLOY` lama TIDAK berubah, `.part`
  dibersihkan trap.
- **gzip gagal** → pipeline gagal, tanpa publikasi, stamp tidak berubah.
- pg_dump exit 0 tapi output KOSONG → `gzip -t` lolos (gzip kosong
  valid!) → validasi isi MENOLAK (bukti cek non-kosong wajib).
- Output bukan dump PG (teks error) → marker MENOLAK.
- gzip menghasilkan file korup → `gzip -t` MENOLAK.
- STAMP dengan karakter aneh (`;`, `../`, `$()`) → ditolak.
- Rollback: TANPA `PRE_DEPLOY_STAMP` → ditolak tanpa perubahan; stamp
  run ini tapi dump TIDAK ADA (simulasi backup gagal, dump lama +
  `LAST_PRE_DEPLOY` sengaja disediakan) → rollback MENOLAK dump lama
  (tidak satu pun perintah restore/service/git dipanggil, `.env` utuh);
  dump korup / bukan dump PG → ditolak; jalur normal terbukti memakai
  dump MILIK RUN INI via log stub `gunzip` (path dump lama tidak pernah
  dibaca).
- Rantai end-to-end nyata: backup gagal → stamp lama tetap ada →
  rollback.sh dijalankan sungguhan dengan stamp run gagal → menolak,
  tidak ada perubahan (`.env`, service, DB).
- Izin round-3 tetap terjaga: `install -d -o $BACKUP_OWNER ... -m 750`
  dipasang SEBELUM penulisan dump; tanpa `sudo mkdir`/`sudo tee`;
  dump 600.

## Verifikasi batasan

- Protected files identik baseline `fbf844f` (blob + sha256, lihat
  CHECKSUM-PROTECTED-FILES.md): `lib/engine.dart`, `lib/montecarlo.dart`,
  `lib/main.dart`, `legacy-web/script.js` — `git log fbf844f..HEAD`
  pada keempat file KOSONG.
- `.github/workflows/main.yml` — blob identik baseline, diff kosong.
- MT5 READ-ONLY — tidak ada perubahan di `backend/mt5client/*` maupun
  `backend/services/terminals.py` blok read-only.
- Kalkulator (lot/formula/konstanta/pembulatan/urutan) — tidak disentuh.
- Tidak ada push / merge / deploy — semua perubahan lokal pada branch.

## Ringkasan angka

| Paket | Jumlah | Hasil |
|---|---|---|
| Seluruh suite pytest | 180 item | **179 passed, 1 skipped** (sudo) |
| Test baru round-4 | 22 | semua lulus |
| Test lama | 157+1skip | semua lulus (tidak ada regresi) |
