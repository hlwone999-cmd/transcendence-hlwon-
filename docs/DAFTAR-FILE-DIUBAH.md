# Daftar File Berubah & Alasan — feat/multi-customer-mvp-postgresql

Basis: `main` @ `fbf844ff4c5ceaa666e0a64f3e4c3b034b3c1333`.

## File BARU (backend)

| File | Alasan |
|---|---|
| `backend/security/__init__.py`, `security/crypto.py` | Enkripsi kredensial MT5 Fernet — brief §5/§7 |
| `backend/auth.py` | Register/login/token/rate-limit/lockout — brief §7 |
| `backend/audit.py` | Audit log setiap aksi — brief §7 |
| `backend/services/terminals.py` | Registry terminal per customer (port, Wine prefix, systemd, runtime, backoff) — brief §4 |
| `backend/migrations/postgres/001_multi_customer.sql` | Skema PostgreSQL multi-customer + RLS — brief §5 |
| `backend/migrations/sqlite/002_multi_customer.sql` | Paritas fitur di SQLite utk dev/test lokal |
| `backend/tools/migrate_sqlite_to_postgres.py` | Jalur migrasi data lama → PG — brief §5 |
| `backend/tools/capacity_test.py` | Uji kapasitas 4 worker — brief §12 |
| `backend/tests/test_auth.py` | Uji #2 #3 + rate limit + kapasitas |
| `backend/tests/test_multi_customer.py` | Uji #4 #6 #7 #9 #10 #11 |
| `backend/tests/test_terminals.py` | Uji #5 #8 |
| `backend/tests/test_readonly_static.py` | Uji #12 (AST scan read-only) |
| `backend/tests/test_postgres_migration.py` | Uji #1 + E2E PG + migrasi data |

## File BARU (Flutter/docs/workflow)

| File | Alasan |
|---|---|
| `lib/mt5_auth.dart` | Klien auth + secure storage token — brief §7/§9 |
| `test/mt5_auth_test.dart` | Uji Flutter utk CI (di Sandbox tak bisa jalan) |
| `docs/MULTI-CUSTOMER-*.md` (6) | Deliverable arsitektur/provisioning/broker/backup/keamanan/test — brief §13 |
| `docs/CHECKSUM-PROTECTED-FILES.md` | Deliverable laporan checksum — brief §13 |
| `docs/DAFTAR-FILE-DIUBAH.md` | Deliverable ini |
| `docs/capacity-test-result.json` | Hasil uji kapasitas mentah |
| `.github/workflows/backend-deploy.yml` | Deploy dgn approval + backup + health check + rollback — brief §10 |
| `PR_DESCRIPTION.md` | Deskripsi PR utk review — brief §13 |

## File DIUBAH

| File | Alasan |
|---|---|
| `backend/db.py` | Dual-engine (PG+SQLite), adapter permukaan sqlite3, migrasi per-engine — brief §5 |
| `backend/config.py` | DATABASE_URL, MT5_CRED_KEY, AUTH_*, MAX_CUSTOMERS, RPYC_PORT_BASE, TERM_* — tanpa mengubah perilaku lama |
| `backend/app.py` | Wire TerminalRegistry + MultiAccountScheduler; tetap kompatibel injeksi test |
| `backend/api/routes.py` | Endpoint ter-scope customer, auth baru, bind/unbind; `_account_id(conn)` global DIHAPUS — brief §6; bentuk respons lama dipertahankan |
| `backend/services/sync.py` | `sync_account()` per-akun (lock/backoff/guard terminal); helper `sync()` legacy utuh; nama tabel via `trades_table/sync_state_table` |
| `backend/services/scheduler.py` | Tambahan MultiAccountScheduler; SyncScheduler legacy dipertahankan |
| `backend/services/analytics.py` | Hanya penamaan tabel (f-string `tt`) — logika statistik NOL perubahan |
| `backend/tests/conftest.py` | Fixture auth/baru + reset rate-limit; fixture lama utuh |
| `backend/tests/test_api.py` | Auth disesuaikan ke token sesi (token global tidak lagi memberi akses — brief §6); SEMUA asersi data dipertahankan |
| `backend/tests/test_date_filters.py` | Setup memakai register+bind+sync via endpoint nyata; SEMUA asersi angka lama dipertahankan |
| `lib/mt5_page.dart` | `_boot` baca token secure storage; dialog setting + form MASUK — tanpa menyentuh logika jurnal/kalkulator |
| `pubspec.yaml` | + `flutter_secure_storage` |
| `backend/.env.example` | Variabel env baru multi-customer |
| `.github/workflows/backend-tests.yml` | Diperluas: PG test, security check, checksum (tujuan sama: CI backend) |

## Audit round-4 (`fix/audit4-final-hardening`, basis `15e2f5a`)

Dua temuan audit independen + regression test. Sifat perubahan: kecil,
terlokalisasi, tanpa menyentuh algoritma kalkulator maupun MT5.

| File | Baru/Ubah | Alasan (temuan) |
|---|---|---|
| `backend/services/sync.py` | ubah | **Round-4 #1**: `sync_account()` — lock di-acquire SEBELUM `set_customer_context()`; context dipasang DI DALAM try. Bila lock gagal, `SyncLockBusy` dilempar sebelum scope terpasang — tidak ada lagi scope customer tertinggal di koneksi thread |
| `backend/services/terminals.py` | ubah | **Round-4 #1 lanjutan**: `_account_config()` (dipakai `runtime_for` cache-miss) memulihkan scope pemanggil di `finally` — versi lama meninggalkan scope customer setelah membaca `mt5_credentials` |
| `backend/db.py` | ubah | `PostgresConn.set_customer_context(None)` = soft-clear GUC transaksi berjalan (`set_config('',true)`) TANPA rollback — aman memulihkan scope lama di tengah transaksi pemanggil |
| `backend/deploy/backup_db.sh` | **baru** | **Round-4 #2**: logika backup dipindah dari workflow agar bisa dites sungguhan: `set -euo pipefail`, dump ke file `.part`, validasi `gzip -t` + non-kosong + marker dump PG, publikasi atomik `mv`, `LAST_PRE_DEPLOY` HANYA setelah tervalidasi, trap bersih-bersih |
| `backend/deploy/rollback.sh` | ubah | **Round-4 #2**: rollback hanya memakai backup MILIK run ini (`PRE_DEPLOY_STAMP` dari workflow); backup run gagal/korup → MENOLAK restore, backup lama TIDAK dipakai (kondisi masih pra-deploy); `LAST_PRE_DEPLOY` tidak lagi jadi dasar keputusan |
| `.github/workflows/backend-deploy.yml` | ubah | Step backup mengirim `backup_db.sh` via `bash -s` + mengekspor `PRE_DEPLOY_STAMP` ke GITHUB_ENV; step rollback meneruskan `PRE_DEPLOY_STAMP` |
| `backend/tests/test_sync_scope_lock.py` | **baru** | Regression round-4 #1: lock gagal → scope TIDAK pernah dipasang (rekam panggilan); urutan set→run→clear saat sukses; cleanup saat pekerjaan error; kontrol negatif bukti leak lama terdeteksi |
| `backend/tests/test_rls_no_leak_requests.py` | ubah | + `test_sync_lock_busy_tidak_meninggalkan_scope`: bukti GUC PostgreSQL sungguhan bersih ketika lock gagal + kontrol negatif |
| `backend/tests/test_backup_script.py` | **baru** | Regression round-4 #2 (12 test): backup sukses, pg_dump gagal, gzip gagal, dump kosong, bukan dump PG, gzip korup, stamp aneh, rantai "backup gagal → rollback menolak backup lama" |
| `backend/tests/test_rollback_script.py` | ubah | Kontrak `PRE_DEPLOY_STAMP` + 5 test baru: tanpa stamp ditolak, backup run gagal → backup lama TIDAK dipakai, dump korup/bukan-PG ditolak, jalur normal terbukti memakai dump run ini (bukan dump lama) |
| `backend/tests/test_deploy_backup_permission.py` | ubah | Test statik izin backup menunjuk `backup_db.sh` (logika dipindah); jaminan round-3 dipertahankan |
| `docs/MULTI-CUSTOMER-BACKUP-ROLLBACK.md` | ubah | Runbook diselaraskan kontrak round-4 (backup tervalidasi, stamp, rollback tanpa backup lama) |
| `docs/HASIL-TEST-AUDIT-4.md` | **baru** | Bukti test round-4 |

## TIDAK disentuh (protected + fitur lama)

- `lib/engine.dart`, `legacy-web/script.js`, `lib/montecarlo.dart`,
  `lib/main.dart` — **checksum identik baseline** (lihat
  docs/CHECKSUM-PROTECTED-FILES.md).
- `test/engine_test.dart`, `test/mc_pipeline_test.dart`, semua test
  kalkulator lain — diff kosong.
- `.github/workflows/main.yml` (build APK) — tidak diubah.
- Semua endpoint jurnal/equity/reconciliation: path & bentuk respons sama.
