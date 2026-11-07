# Arsitektur Multi-Customer — Transcendence MVP

## A. Arsitektur SEBELUM (baseline `fbf844f`)

```
Flutter APK ── X-API-Token (1 token global) ──> Flask (1 akun MT5 global)
                                                  ├─ SQLite journal.db (7 tabel)
                                                  ├─ 1 provider mt5linux @127.0.0.1:18812
                                                  └─ 1 scheduler thread (lock global)
Pemilihan akun: _account_id(conn) = "ORDER BY last_sync_at DESC LIMIT 1"
Kredensial MT5: env global (MT5_LOGIN/PASSWORD/SERVER) — 1 akun saja
```

Masalah: satu token untuk semua, satu akun, satu terminal, satu lock —
tidak bisa melayani lebih dari 1 customer, tidak ada isolasi data.

## B. Arsitektur SESUDAH (branch `feat/multi-customer-mvp-postgresql`)

```
Flutter Android (login customer, token sesi di secure storage)
   │  Authorization: Bearer / X-API-Token (nama header lama dipertahankan)
   ▼
Flask API /api/v1
   ├─ auth.py           : register/login/logout/me + rate-limit + lockout
   ├─ require_customer  : customer context SELALU dari token terverifikasi
   │                      (customer_id/account_id dari request diabaikan)
   ├─ audit.py          : audit_logs utk setiap aksi penting
   ├─ security/crypto   : kredensial MT5 Fernet (kunci env MT5_CRED_KEY)
   ▼
PostgreSQL 16 (RLS defense-in-depth)
   ├─ customers, users, auth_tokens
   ├─ mt5_accounts (UNIQUE customer_id → 1 akun/customer)
   ├─ mt5_credentials (enc_password), mt5_terminals (port unik)
   ├─ mt5_deals, mt5_orders, mt5_trades, mt5_symbols
   └─ mt5_sync_state, sync_logs, audit_logs
   ▼
TerminalRegistry (runtime per akun)
   ├─ provider READ-ONLY per akun  → 127.0.0.1:1881x (port per customer)
   ├─ lock per akun + backoff eksponensial
   ▼
MultiAccountScheduler (dispatcher + 4 worker)
   └─ kegagalan 1 customer TIDAK menghentikan customer lain
```

Isolasi per customer (brief §4) — dipetakan ke implementasi:

| Kebutuhan | Implementasi |
|---|---|
| 1 akun MT5 | `mt5_accounts.customer_id UNIQUE` + guard bind |
| 1 Wine prefix | `mt5_terminals.wine_prefix` per customer |
| 1 terminal | `terminal_dir` per customer + unit systemd sendiri |
| 1 direktori data | `data_dir` per customer |
| 1 koneksi RPyC | provider per akun (lazy, evict saat unbind) |
| 1 port unik | alokasi 18812..18815, UNIQUE di tabel |
| 1 service/process | `transcendence-mt5-<code>.service` per customer |
| 1 status koneksi/sync | `mt5_sync_state` per akun |
| Log terpisah | `sync_logs.account_id`, audit terpisah |

## C. Keputusan desain penting

1. **Dual-engine**: `db.py` adapter membuat kode layanan engine-agnostic.
   Produksi = PostgreSQL; SQLite dipertahankan untuk dev/test & regresi.
2. **Nama tabel**: PG memakai `mt5_trades` & `mt5_sync_state` (brief §5);
   SQLite lama tetap `trades` & `sync_state` — dipetakan oleh
   `db.trades_table(conn)` / `db.sync_state_table(conn)`.
3. **Tipe waktu TEXT ISO-UTC** dipertahankan (bukan timestamptz) agar
   SEMANTIK filter tanggal yang sudah disetujui pengguna tidak berubah
   (perbandingan leksikografis identik) — regresi nol.
4. **Kompatibilitas API**: path & bentuk respons endpoint lama tetap;
   perubahan auth didokumentasikan di PR_DESCRIPTION §perubahan API.
5. **RLS PostgreSQL**: policy per tabel data berbasis
   `current_setting('app.customer_id')` — defense-in-depth, bukan
   pengganti otorisasi backend (owner bypass, didokumentasikan).
6. **Legacy path**: `SyncService.sync()` + `SyncScheduler` dipertahankan
   apa adanya untuk regresi & deploy lama; produksi multi-customer
   memakai `sync_account()` + `MultiAccountScheduler`.
