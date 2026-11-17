# PR: Transcendence Multi-Customer MVP (PostgreSQL)

| | |
|---|---|
| **Branch** | `feat/multi-customer-mvp-postgresql` (dari `main` @ `fbf844f`) |
| **Status test** | ✅ 108/108 backend pytest (termasuk PostgreSQL 16 nyata via pgserver) · ⚠ flutter analyze/test belum dijalankan (tanpa SDK — wajib via CI) · ✅ checksum 4 file kalkulator identik baseline |
| **Larangan** | Semua dipatuhi: kalkulator lot tidak tersentuh (bukti checksum), MT5 read-only (whitelist + AST scan), tanpa auto-trading, tanpa rahasia di repo, RPyC localhost saja |
| **Status** | SIAP REVIEW — **tidak di-merge, tidak di-deploy** (brief §13: berhenti untuk independent review) |

## 1. Ringkasan

Backend kini melayani **multi-customer** (MVP: maks 4 aktif, 1 akun MT5
per customer) di atas **PostgreSQL**, dengan isolasi penuh per customer:
database, kredensial terenkripsi, terminal MT5 + Wine prefix + port RPyC
unik (18812-18815, localhost), unit systemd terpisah, scheduler per akun
(lock + backoff), dan audit log. Flutter tetap satu aplikasi; tambahan
hanya login customer + secure storage — **kalkulator lot byte-for-byte
identik baseline**.

## 2. Perubahan utama

1. **Database** — dual-engine (`db.py`): PostgreSQL (produksi) + SQLite
   (dev/test). Skema PG baru: `customers`, `users`, `auth_tokens`,
   `mt5_accounts` (UNIQUE customer_id), `mt5_credentials` (Fernet),
   `mt5_terminals`, `mt5_deals/orders/trades/symbols`,
   `mt5_sync_state`, `sync_logs`, `audit_logs` + Row-Level Security
   (defense-in-depth). Migrasi data SQLite→PG teruji (id dipertahankan).
2. **Auth** — register/login/logout/me; token sesi (server simpan
   SHA-256 + expiry + revocation); rate limit per IP; lockout per email;
   kredensial MT5 Fernet (kunci via env `MT5_CRED_KEY`); audit log.
3. **Isolasi terminal** — `TerminalRegistry`: 1 customer = 1 terminal,
   1 Wine prefix, 1 port RPyC unik localhost, 1 unit systemd; provider
   READ-ONLY per akun; evict saat unbind tanpa mengganggu yang lain.
4. **Sync & scheduler** — `sync_account()` per-akun (guard anti-salah-
   terminal), `MultiAccountScheduler` (pool 4 worker, lock per akun,
   backoff eksponensial, kegagalan terisolasi, idempoten).
5. **API** — endpoint lama tetap (path + bentuk respons); akun SELALU
   dari token terverifikasi; `_account_id(conn)` global DIHAPUS;
   manipulasi `account_id`/`customer_id` di URL/body diabaikan (diuji).
   Endpoint baru: `/auth/*`, `/accounts/bind|unbind`, `/admin/customers`.
6. **Flutter** — `lib/mt5_auth.dart` (baru): login + secure storage;
   `mt5_page.dart` baca token secure storage + form MASUK; field token
   manual dipertahankan. `engine.dart`, `montecarlo.dart`, `main.dart`,
   `legacy-web/script.js`: **tidak berubah**.
7. **CI/CD** — `backend-tests.yml` diperluas (PG test, security scan,
   checksum); `backend-deploy.yml` baru (approval → backup pg_dump →
   migrasi → health check → rollback otomatis). `main.yml` (APK) utuh.

## 3. Perubahan API yang perlu diketahui reviewer

- Token global statis `API_TOKEN` **tidak lagi memberi akses data** —
  hanya dipakai `/api/v1/admin/*` (bootstrap operasional). Data endpoint
  memakai token sesi customer (header `X-API-Token` tetap diterima
  sebagai transport; Flutter kini mengirim token sesi di header sama).
- `GET /mt5/status` menambah field additive `terminal` — kompatibel.
- Tabel internal: PG memakai `mt5_trades`/`mt5_sync_state` (brief §5);
  SQLite tetap `trades`/`sync_state` — dipetakan otomatis.

## 4. Risiko & mitigasi

| Risiko | Mitigasi |
|---|---|
| PG NOT NULL sebelum ON CONFLICT saat upsert akun | `customer_id` ikut di-INSERT; DO UPDATE tidak pernah mengubah pemilik (diuji) |
| Satu akun dipakai 2 customer | `UNIQUE(login, server)` + guard bind (diuji) |
| Terminal salah arah (port tertukar) | guard `_do_sync`: login/server provider wajib = akun ter- bind (diuji) |
| Kunci enkripsi hilang → kredensial tak terbaca | rotasi via `MT5_CRED_KEY_OLD`; dokumentasi backup |
| Rate limiter in-memory hilang saat restart | terima utk MVP 1 proses; Redis dicatat issue terbuka |
| Regresi statistik/filter tanggal | tipe waktu TEXT ISO dipertahankan; 70 test lama lulus termasuk baseline Oktober 2026 (+Rp22.036,79 / 37 trades / delta 0) |

## 5. Cara menguji (reviewer)

```bash
cd backend
pip install -r requirements.txt psycopg2-binary cryptography pgserver pytest
python3 -m pytest tests/ -o addopts="" -v          # 108 test
python3 tools/capacity_test.py --cycles 5          # uji kapasitas
sha256sum lib/engine.dart ../lib/engine.dart 2>/dev/null || true
git diff main -- lib/engine.dart legacy-web/script.js lib/montecarlo.dart lib/main.dart  # kosong
# Flutter (butuh SDK):
flutter pub get && flutter analyze && flutter test
```

Alur manual: `/auth/register` → `/auth/login` → `/accounts/bind` →
`POST /mt5/sync` → `/journal/stats` (semua dgn `Authorization: Bearer`).

## 6. Yang BELUM selesai (jujur)

- `flutter analyze`/`flutter test` belum pernah dijalankan (sandbox tanpa
  SDK) — test Dart sudah ditulis; wajib hijau di CI sebelum merge.
- Tidak ada uji live MT5/Wine/RPyC (tanpa terminal & kredensial broker);
  provisioning real-mode mengikuti dokumen, perlu verifikasi di VPS.
- RLS PG diuji level policy; koneksi aplikasi jalan sebagai owner
  (bypass) — enforcement nyata perlu role aplikasi terpisah (issue).
- Multi-replica rate limiting (Redis), kuantisasi CAP 4 customer utk
  upgrade path multi-akun — issue terbuka (docs/daftar issue).
