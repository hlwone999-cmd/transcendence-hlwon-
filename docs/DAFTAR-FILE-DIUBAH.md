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

## TIDAK disentuh (protected + fitur lama)

- `lib/engine.dart`, `legacy-web/script.js`, `lib/montecarlo.dart`,
  `lib/main.dart` — **checksum identik baseline** (lihat
  docs/CHECKSUM-PROTECTED-FILES.md).
- `test/engine_test.dart`, `test/mc_pipeline_test.dart`, semua test
  kalkulator lain — diff kosong.
- `.github/workflows/main.yml` (build APK) — tidak diubah.
- Semua endpoint jurnal/equity/reconciliation: path & bentuk respons sama.
