# REDESIGN JURNAL MT5 — KEPUTUSAN D1–D5 & IMPLEMENTASI

> Status: **TERIMPLEMENTASI** di branch lokal `feat/jurnal-mt5-redesign`
> (basis `b96f27c`). Dokumen ini adalah dokumentasi O2 yang disetujui
> pemilik proyek. Migrasi O1 (`password_kind`) **DITUNDA** — tidak ada
> migrasi database pada iterasi ini.

## 1. Ringkasan keputusan (disetujui)

| Keputusan | Isi | Status implementasi |
|---|---|---|
| **D1** | Identitas customer = nomor akun MT5 + server broker + password investor. Tidak ada input URL / email customer / password customer / token manual di UI. | ✅ UI hanya 3 input; URL backend dari konfigurasi aplikasi. |
| **D2** | Self-link otomatis dengan batas maksimum 4 customer aktif; capacity guard aman pendaftaran bersamaan, unique constraint akun, rate limit, lockout, audit; konfigurasi on/off oleh admin. | ✅ `POST /api/v1/mt5/connect` + `MT5_SELF_LINK_ENABLED` + `MAX_CUSTOMERS` (default 4). |
| **D3** | PUTUSKAN KONEKSI hanya logout + mencabut token sesi. Riwayat jurnal tidak dihapus. Unbind hanya via API/admin. | ✅ `POST /api/v1/mt5/disconnect`; unbind tetap `POST /api/v1/accounts/unbind` (tidak diekspos di UI). |
| **D4** | Dropdown server Exness + opsi "Lainnya" input manual. Daftar TIDAK dianggap lengkap / sama untuk semua akun. | ✅ `lib/mt5_servers.dart` + catatan kejujuran di UI. |
| **O2** | Dokumentasi (dokumen ini). | ✅ |
| **O1** | Migrasi DB `password_kind`. | ⏸ DITUNDA (disetujui pemilik). |

## 2. Arsitektur & alur baru

```
┌──────────── Flutter (APK) ────────────┐      ┌──────── Flask (VPS) ────────┐
│ Mt5JournalPage                        │      │ api/routes.py               │
│  ├─ AppConfig.apiBaseUrl              │HTTPS │  ├─ POST /api/v1/mt5/connect│
│  │   (--dart-define, bukan input UI)  │─────►│  │   (self-link / login)    │
│  ├─ form 3 input (D1)                 │      │  ├─ POST /api/v1/mt5/       │
│  │   nomor akun · server · password   │      │  │   disconnect (D3)        │
│  ├─ HUBUNGKAN AKUN MT5                │      │  ├─ GET /mt5/status,        │
│  ├─ status koneksi (PulseDot)         │◄─────│  │   /journal/* (read-only) │
│  └─ PUTUSKAN KONEKSI                  │      │  └─ mt5linux READ-ONLY      │
└───────────────────────────────────────┘      └─────────────────────────────┘
```

### 2.1 `POST /api/v1/mt5/connect` — self-link otomatis (D1/D2)

Request (tanpa sesi; endpoint publik terbatas):

```json
{"login": 12345678, "server": "Exness-MT5Real", "password": "<investor>"}
```

Perilaku:

1. **Akun sudah terdaftar** (`UNIQUE(login, server)` di `mt5_accounts`)
   → verifikasi password terhadap kredensial terenkripsi
   (perbandingan konstanta-waktu, plaintext hanya di memori) → terbitkan
   token sesi customer pemiliknya (HTTP 200, `mode: "login"`).
2. **Akun baru** → buat customer + user internal (email sintetis
   `{login}.{server-slug}@mt5.link` dengan password acak yang tidak
   pernah dikirim ke siapa pun — login self-link SELALU via endpoint
   ini), kredensial terenkripsi Fernet, provisioning terminal per
   customer, sync awal background → HTTP 201, `mode: "self_link"`.
3. **Identitas pernah terdaftar tanpa akun aktif** (mis. setelah unbind
   via API/admin) → pakai ulang customer yang sama (tidak memakan slot
   kapasitas baru).

Guard keamanan (semua diuji):

| Guard | Implementasi |
|---|---|
| Kapasitas maks 4 customer aktif | `MAX_CUSTOMERS` (default 4) + `capacity_guard` (PG advisory lock / SQLite BEGIN IMMEDIATE — TOCTOU tertutup) |
| Unique constraint akun | `UNIQUE(login, server)` `mt5_accounts` — balapan → 409 rapi + customer yatim dibersihkan |
| Rate limit | 10 permintaan / 5 menit per IP (sliding window) |
| Lockout | Per (login, server) setelah 5 kegagalan verifikasi → 429 |
| Audit | `mt5_connect` / `mt5_connect_gagal` di `audit_logs` (TANPA password) |
| Nonaktif oleh admin | Env `MT5_SELF_LINK_ENABLED=false` → 403 |

Response sukses:

```json
{"ok": true, "mode": "self_link", "token": "...", "token_type": "bearer",
 "customer_code": "C001",
 "account": {"login": 12345678, "server": "Exness-MT5Real",
             "currency": "USD"},
 "terminal": {"rpyc_port": 18812, "status": "ready"}}
```

### 2.2 `POST /api/v1/mt5/disconnect` — PUTUSKAN KONEKSI (D3)

Ber-sesi. HANYA `revoke_token(g.token_id)` + audit `mt5_disconnect`.
Tidak menyentuh `mt5_accounts`, `mt5_credentials`, `mt5_deals`, `trades`,
terminal, atau scheduler. Uji regresi membuktikan: setelah disconnect →
401 di `/journal/trades`; setelah connect ulang → riwayat utuh.

### 2.3 Sisa endpoint tidak berubah

`GET /mt5/status`, `GET /mt5/account`, `POST /mt5/sync`,
`GET /journal/trades|stats|equity-curve|reconciliation`, `GET /symbols`
— semuanya tetap ber-sesi customer dan terisolasi per customer
(filter `account_id` dari DB, bukan dari request; RLS PostgreSQL
defense-in-depth). Semua endpoint baru ikut prefix `/api/v1` —
Flutter (`mt5_api.dart`, `mt5_auth.dart`) dan Flask (`url_prefix`) sudah
diperiksa konsisten (syarat implementasi #9, diuji di
`test_mt5_auth_test.dart`/`test_mt5_connect.py`).

## 3. Model keamanan password investor (syarat #2–#5)

| Fase | Nasib password |
|---|---|
| Di APK | Hanya di memori (TextEditingController) selama proses koneksi; controller dibersihkan di SEMUA jalur keluar (sukses/gagal) dan saat dispose. Tidak pernah masuk storage. |
| Di transport | HTTPS wajib (kebijakan M3 satu sumber `Mt5UrlPolicy`; HTTP hanya loopback/dev eksplisit). |
| Di backend (memori) | Hanya selama request verifikasi/penyimpanan. |
| Di backend (at-rest) | Fernet (AES-128-CBC + HMAC-SHA256) di `mt5_credentials.enc_password`, kunci env `MT5_CRED_KEY`; `key_version` mendukung rotasi `MT5_CRED_KEY_OLD`. |
| Log & audit | TIDAK PERNAH. Diuji: `test_password_investor_tidak_pernah_plaintext_di_db_dan_audit`, `test_password_tidak_bocor_ke_log_backend`. |

Catatan jujur (risiko yang diterima MVP, terdokumentasi): kredensial
akun baru tidak diverifikasi live ke broker pada saat connect (terminal
per customer butuh provisioning + aktivasi admin dulu — sama seperti
semantik `/accounts/bind` sebelumnya). Password salah baru ketahuan saat
sync awal gagal; pemulihan via unbind admin (§5). Mitigasi penyalahgunaan:
rate limit + lockout + kapasitas 4 + audit.

## 4. MT5 read-only tetap utuh (syarat #6–#7)

- Provider `MT5ProviderBase` whitelist metode baca saja
  (`READ_METHODS`); `order_send/eval/...` → `ReadOnlyViolationError`
  (`__getattr__` blokir). Uji `test_provider_mock_tetap_menolak_method_tulis`
  + static guard: kode `/mt5/connect` bebas nama metode tulis.
- Investor password (bukan master) yang dipakai — sesi MT5 investor
  memang trade-disabled di sisi broker; lapisan aplikasi menambahkan
  whitelist sebagai defense-in-depth.
- **Kalkulator lot tidak disentuh**: `lib/engine.dart`,
  `lib/montecarlo.dart`, `lib/main.dart`, `legacy-web/script.js`,
  `.github/workflows/main.yml` TIDAK diubah (diverifikasi hash, §6).

## 5. Prosedur admin

| Kebutuhan | Langkah |
|---|---|
| Nonaktifkan self-link | Set `MT5_SELF_LINK_ENABLED=false` di `.env` VPS → restart backend. UI customer tetap tampil, tapi connect ditolak 403. |
| Provisioning manual | Jalur lama tetap ada: `POST /api/v1/auth/register` (admin) + `POST /api/v1/accounts/bind`. |
| Reset customer yang connect dengan password salah | Unbind via API (`POST /api/v1/accounts/unbind` dengan sesi customer) ATAU hapus baris `mt5_accounts`+`mt5_credentials`+`mt5_terminals` customer terkait di DB — customer lama otomatis dipakai ulang saat connect ulang. |
| Lihat aktivitas | `SELECT * FROM audit_logs ORDER BY id DESC;` |

## 6. File yang diubah / ditambah

**Backend**

| File | Perubahan |
|---|---|
| `backend/config.py` | + `mt5_self_link_enabled` (env `MT5_SELF_LINK_ENABLED`, default on) |
| `backend/auth.py` | + parameter `skip_password_validation` pada `register_customer` (default perilaku lama utuh) |
| `backend/api/routes.py` | + `POST /mt5/connect`, `POST /mt5/disconnect`, helper `_server_slug`, `registry_provision`, `_discard_orphan_customer` |
| `backend/tests/test_mt5_connect.py` | **BARU** — 14 test regresi (login, logout/disconnect, isolasi, read-only, kapasitas, balapan, lockout, kebocoran) |

**Flutter**

| File | Perubahan |
|---|---|
| `lib/mt5_page.dart` | Redesain besar: form koneksi 3 input (D1), dropdown server + Lainnya (D4), tombol lihat/sembunyi, HUBUNGKAN AKUN MT5, status koneksi, PUTUSKAN KONEKSI + dialog konfirmasi (D3); hapus dialog ATUR SERVER (URL/email/password/token manual); URL dari `AppConfig`; 401 → kembali ke form koneksi. |
| `lib/mt5_auth.dart` | + `connectAccount()` (POST /mt5/connect, token disimpan aman, password tidak pernah lokal), + `disconnect()` (POST /mt5/disconnect, clear di `finally`); 401 membawa pesan server. |
| `lib/mt5_api.dart` | `friendly` error auth menampilkan pesan server bila ada (fallback teks lama). |
| `lib/app_config.dart` | **BARU** — URL backend via `--dart-define TRANSCENDENCE_API_URL`. |
| `lib/mt5_servers.dart` | **BARU** — daftar server Exness (non-eksaustif) + `kServerLainnya` + validasi manual. |
| `test/mt5_test.dart` | Widget test disesuaikan desain baru (form koneksi, 401 → form, regresi D1/D4). |
| `test/mt5_connect_flow_test.dart` | **BARU** — 15 test: alur connect/disconnect widget+unit, menggantikan `l2_token_overwrite_test.dart` (vektor L2 dihapus by design: tidak ada lagi field token manual). |
| `test/mt5_auth_test.dart` | + 5 test unit connectAccount/disconnect. |
| `test/l2_token_overwrite_test.dart` | **DIHAPUS** (semantiknya hilang bersama field token manual; rotasi token kini diuji lewat connect). |

**Protected files**: `lib/engine.dart`, `lib/montecarlo.dart`,
`lib/main.dart`, `legacy-web/script.js`, `.github/workflows/main.yml`
— TIDAK tersentuh.

## 7. Konfigurasi build APK (untuk rilis)

```bash
flutter build apk --release \
  --dart-define=TRANSCENDENCE_API_URL=https://jurnal.domain.com
```

Bila `TRANSCENDENCE_API_URL` tidak di-set, halaman jurnal menampilkan
peringatan "URL backend belum dikonfigurasi pada build ini" — tidak crash.

## 8. Cakupan test (syarat #10)

Backend (`backend/tests/test_mt5_connect.py`, 14 test):

- login: connect baru 201 → token valid; connect ulang → 200 mode login
  tanpa duplikasi; setelah unbind → pakai-ulang customer yang sama.
- logout: disconnect → token dicabut; riwayat jurnal utuh setelah
  connect ulang; unbind tetap jalur terpisah.
- isolasi: customer A (akun X) tidak melihat jurnal customer B (akun Y);
  detail trade customer lain 404.
- read-only: provider menolak metode tulis; source `/mt5/connect`
  bebas nama metode tulis MT5.
- D2: kapasitas 403 + slot bebas setelah unbind; balapan bersamaan slot
  terakhir tepat satu sukses; lockout 429; self-link off 403; input
  tidak valid 400; password tidak pernah plaintext di DB/audit/log.

Flutter (`flutter test`, dijalankan CI — sandbox tanpa Flutter SDK):

- `test/mt5_connect_flow_test.dart` (15), `test/mt5_test.dart` (disesuaikan),
  `test/mt5_auth_test.dart` (+5), plus seluruh test lama yang tidak
  tersentuh desain.

---

## 9. HARDENING VERIFIKASI LIVE (pasca-audit commit d4f714f)

### 9.1 Masalah yang diperbaiki

1. Akun baru sebelumnya dianggap "terhubung" begitu POST /mt5/connect
   sukses — kredensial BELUM pernah diuji ke terminal/broker. Password
   salah, server tidak cocok, atau terminal mati tetap menghabiskan slot
   customer.
2. Koneksi ulang akun lama hanya membandingkan password dengan
   ciphertext tersimpan — jika password diubah di broker atau server
   tidak cocok, sesi tetap diterbitkan dan UI bisa menampilkan status
   seolah aktif.
3. API MetaTrader5 (`initialize`) menerima password MASTER dan password
   INVESTOR tanpa parameter pembeda — kredensial master-capable bisa
   tersimpan di database.

### 9.2 Desain verifikasi live (satu POST, sinkron, berbatas waktu)

`POST /api/v1/mt5/connect` kini SELALU menjalankan login nyata ke broker
via provider read-only (`_live_verify_account`) dengan batas waktu
`MT5_VERIFY_TIMEOUT_SECONDS` (default 12s, wajib < timeout HTTP klien
25s). Klasifikasi hasil:

| Klasifikasi     | Makna                                    | Akun baru                          | Akun lama (reconnect)                          |
|-----------------|------------------------------------------|------------------------------------|------------------------------------------------|
| `ok`            | Login nyata sukses, sesi investor        | Diaktifkan + token, `connected`    | Token, `connected`                             |
| `auth_failed`   | Broker menolak kredensial                | 401 + SEMUA jejak dibersihkan      | 401 tanpa token (pesan: password broker mungkin diubah) |
| `master_password` | Sesi berhak trade (`trade_allowed=true`) | 401 + cleanup (kredensial tidak disimpan) | 401 tanpa token (admin perlu reset kredensial) |
| `unreachable` / `timeout` / `busy` | Terminal/broker tidak terjangkau dalam batas waktu | 503 + cleanup — slot TIDAK terpakai | Token tetap terbit (riwayat tetap terbaca, D3) + `terminal_pending` |

Cleanup (`_cleanup_failed_activation`) membuang: runtime provider,
kredensial, baris akun, baris terminal, customer BARU hasil request,
dan direktori terminal — slot kapasitas bebas kembali, tanpa baris
yatim. Idempoten dan best-effort; pesan error asli tidak pernah
tertutup pembersihan.

### 9.3 Deteksi password master (audit risiko #8)

MetaTrader5 tidak membedakan master vs investor di API — satu-satunya
sinyal adalah `account_info().trade_allowed`: sesi investor (read-only)
selalu `false`, sesi password master `true`. Backend menolak kredensial
yang menghasilkan sesi trade-capable dan TIDAK PERNAH menyimpannya.
Kebijakan dapat dilonggarkan admin via
`MT5_ENFORCE_INVESTOR_READONLY=false` (tidak disarankan). Risiko sisa:
broker yang menandai `trade_allowed=true` untuk sesi investor
(non-standar) akan selalu ditolak — pesan mengarahkan ke admin; dan
password investor yang kebetulan identik dengan master tetap terdeteksi
sebagai master (sifat, bukan bug: kredensial itu memang trade-capable).

### 9.4 Status UI (empat keadaan terpisah)

- `MEMVERIFIKASI…` — POST /mt5/connect berjalan; server memverifikasi
  kredensial + login read-only ke broker.
- `MENYIAPKAN TERMINAL` — sesi diterima tapi probe live belum
  membuktikan; UI mem-poll `GET /mt5/status` (sumber kebenaran = probe
  nyata backend, maks 4 siklus).
- `TERHUBUNG` — HANYA setelah `/mt5/status` melaporkan `connected=true`
  (hasil probe live), bukan karena POST sukses.
- `GAGAL — VERIFIKASI BROKER BELUM BERHASIL` — banner + alasan tanpa
  kredensial + tombol COBA VERIFIKASI ULANG; sesi tetap ada dan riwayat
  jurnal tetap terbuka (D3).

Perbaikan boot: tanpa sesi tersimpan, halaman langsung menampilkan form
koneksi (tanpa round-trip 401) — semua endpoint jurnal memang butuh
token sesi.

### 9.5 Batasan pengujian tanpa terminal MT5 aktif

- Semua test memakai `MockMT5Client`; klasifikasi auth/unreachable/
  timeout/master disimulasikan lewat knob `fail_auth`, `fail_connect`,
  `connect_delay`, `trade_allowed`. Kejujuran jalur sukses terhadap
  broker NYATA hanya bisa dipastikan saat deployment VPS (terminal
  menyala).
- Pesan otorisasi broker dikenali dari teks `last_error` terminal
  (marker: "invalid login", "password", "authorization", dst.). Broker
  dengan pesan non-standar dapat terklasifikasi `unreachable` — akibat
  hanya pesan lebih lunak + tanpa lockout, bukan lubang keamanan.
- Waktu start terminal Wine (mode "real") bervariasi; admin dapat
  menaikkan `MT5_VERIFY_TIMEOUT_SECONDS`. Self-link butuh terminal
  unit sudah `enable --now` sebelum verifikasi bisa berhasil.

### 9.6 Konfigurasi tambahan

| Env | Default | Arti |
|-----|---------|------|
| `MT5_VERIFY_TIMEOUT_SECONDS` | `12` | Batas waktu verifikasi live per connect (detik). |
| `MT5_ENFORCE_INVESTOR_READONLY` | `1` | Tolak kredensial yang menghasilkan sesi trade-capable (deteksi password master). |

Test verifikasi live: `test_mt5_connect.py` kini 24 test (10 baru:
cleanup kredensial ditolak broker, master password + lockout, broker
unreachable slot bebas, timeout broker + cleanup, reconnect broker
menolak 401 tanpa token, reconnect terminal mati terminal_pending,
provisioning gagal tanpa yatim, unit klasifikasi, static guard).
Flutter: `mt5_connect_flow_test.dart` kini 18 test (W9 MENYIAPKAN
TERMINAL → TERHUBUNG hanya setelah probe; W10 banner GAGAL + sesi
tetap); perbaikan finder ambigu (judul kartu vs tombol) yang membuat
7 test widget + 4 test lain gagal sejak d4f714f.
