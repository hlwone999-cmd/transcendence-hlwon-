# Keamanan & READ-ONLY — Transcendence Multi-Customer

## 1. Autentikasi & sesi

- Password customer: werkzeug KDF salted (scrypt default / pbkdf2-sha256) —
  plaintext tidak pernah disimpan.
- Token sesi: `secrets.token_urlsafe(32)`; server menyimpan **SHA-256**
  token + `expires_at` (default 7 hari) + `revoked_at`. Token asli hanya
  dikirim sekali ke klien.
- Logout = revoke di server + hapus dari secure storage perangkat.
- Transport: header `Authorization: Bearer` **atau** `X-API-Token`
  (nama header lama dipertahankan demi kompatibilitas Flutter).
- Rate limiting: login 10/5 menit per IP; register 5/jam per IP
  (sliding-window in-memory). Lockout per email setelah 5 gagal (15 menit).
- Kapasitas: maksimal 4 customer aktif — ditegakkan saat register.

## 2. Otorisasi (tidak percaya input)

- `require_customer` menurunkan `customer_id` dari token terverifikasi.
- `account_id`/`customer_id` di URL/query/body **diabaikan** untuk
  keperluan otorisasi — diuji test manipulasi ID.
- Query data selalu `WHERE account_id = (akun milik customer token)`.
- PostgreSQL RLS: policy per tabel data berbasis `app.customer_id`
  (defense-in-depth utk role non-owner; owner bypass — didokumentasikan).

## 3. Kredensial MT5

- Disimpan terenkripsi Fernet (AES-128-CBC + HMAC-SHA256 — kategori
  authenticated encryption setara AES-GCM) di `mt5_credentials`.
- Kunci HANYA dari env `MT5_CRED_KEY` (± rotasi via `MT5_CRED_KEY_OLD`);
  tidak pernah ada di repo, database, log, atau respons API.
- Password TIDAK PERNAH: dikembalikan ke klien, ditulis ke log,
  dimasukkan pesan error (diuji test kebocoran).
- Di Flutter: password MT5 hanya lewat form bind → server; token sesi
  di `flutter_secure_storage` (bukan SharedPreferences biasa).

## 4. READ-ONLY MT5 (larangan trading)

- `MT5ProviderBase` whitelist method baca: connect, disconnect,
  is_connected, account_info, terminal_info, symbol_info, positions_get,
  history_deals_get, history_orders_get.
- `__getattr__` memblokir SEMUA method lain → `ReadOnlyViolationError`
  (termasuk `order_send`, `order_check`, `positions_close`, ...).
- Audit statik (test otomatis): AST scan backend — tidak ada pemanggilan
  nama trading API; regex eval/exec/__import__; penyebutan `order_send`
  di luar file kebijakan ditolak.
- Tidak ada auto-trading, tidak ada EA; terminal hanya dijalankan untuk
  membaca riwayat/posisi.

## 5. Jaringan

- Semua port RPyC (18812-18815) bind 127.0.0.1 — tidak pernah publik.
- Backend bind 127.0.0.1 di balik reverse-proxy HTTPS.
- Tidak ada kredensial VPS/DB/MT5 di repo atau APK; deploy memakai
  GitHub Environment secrets + approval manual.

## 6. Audit

Tabel `audit_logs` mencatat: register, login, login_gagal, logout, bind,
unbind, sync_manual, admin_create_customer — dgn IP, user-agent, detail
JSON bebas kredensial. Retensi: ikut kebijakan backup DB.

## 7. Error handling

Semua endpoint gagal-aman; pesan error standar tanpa stack trace;
`logging.getLogger("rpyc")` ditekan ke WARNING agar argumen internal
tidak pernah muncul di log.

## 8. Row-Level Security PostgreSQL (defense-in-depth — audit #2)

Policy RLS pada tabel ber-customer (`mt5_deals`, `mt5_orders`,
`mt5_trades`, `mt5_symbols`, `mt5_sync_state`, `mt5_credentials`)
membaca `current_setting('app.customer_id', true)`.

- **Context per transaksi**: backend memasang context via
  `set_config('app.customer_id', <id>, true)` — semantik `SET LOCAL`:
  otomatis reset saat COMMIT/ROLLBACK, mustahil bocor ke transaksi
  berikutnya yang memakai koneksi yang sama. Dipasang di
  `require_customer` (dari token terverifikasi server) dan di worker
  sinkronisasi sebelum menyentuh baris akun (`db.set_customer_context`).
- **Role aplikasi non-owner**: koneksi produksi memakai
  `transcendence_app` (NOSUPERUSER, NOBYPASSRLS — migrasi 002). Owner
  database hanya untuk migrasi/administrasi dan BYPASS RLS (sifat bawaan
  PostgreSQL; didokumentasikan, bukan jalur aplikasi).
- **Bukti nyata** (test_rls_postgres.py, PostgreSQL sungguhan via
  pgserver): role aplikasi tanpa context melihat 0 baris; dengan context
  A hanya data A; query langsung `SELECT/UPDATE ... WHERE
  account_id=<B>` dari koneksi ber-context A menghasilkan 0 baris.
- **RLS BUKAN pengganti authorization backend**: filter `customer_id`
  pada query tetap wajib dan tetap diuji (test_multi_customer,
  test_api). RLS hanya lapisan terakhir bila ada lupa filter.
