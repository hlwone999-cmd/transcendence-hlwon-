# Hasil Test — Transcendence Multi-Customer MVP

Semua test di bawah **benar-benar dijalankan** di lingkungan implementasi
(Debian 13, Python 3.12.16, PostgreSQL 16.2 via pgserver) pada branch
`feat/multi-customer-mvp-postgresql`.

## 1. Ringkasan

| Suite | Hasil | Perintah |
|---|---|---|
| Backend penuh (regresi + baru) | **108 passed** | `cd backend && python3 -m pytest tests/` |
| — termasuk migrasi PG nyata | 6 test PG lulus | pgserver (PostgreSQL 16.2) |
| Uji kapasitas 4 worker | 20/20 sync OK, 0 duplikat | `python3 tools/capacity_test.py --cycles 5 --deals 300` |
| Flutter analyze/test | **TIDAK DIJALANKAN** — Flutter SDK tidak tersedia di sandbox | wajib `flutter analyze && flutter test` di GitHub Actions (workflow tersedia) |
| MT5 live | **TIDAK diuji** — tanpa terminal/kredensial broker | semua MT5 memakai mock (sejalan brief §11) |

Bukti verbose per-test: keluaran `-v` disimpan saat implementasi; 108
baris PASSED termasuk seluruh daftar di bawah.

## 2. Pemetaan 15 kategori wajib (brief §11)

| # | Kategori | Test | Hasil |
|---|---|---|---|
| 1 | Migrasi PG dari DB kosong | test_postgres_migration::test_migrasi_pg_dari_db_kosong | PASSED |
| 2 | Registrasi & login | test_auth::test_register_and_login_ok (+4) | PASSED |
| 3 | Token invalid/expired/revoked | test_auth::test_token_invalid/expired/logout | PASSED |
| 4 | 1 akun MT5 per customer | test_multi_customer::test_satu_akun_per_customer (+2) | PASSED |
| 5 | 4 customer = terminal terpisah | test_terminals::test_empat_customer_empat_terminal (port 18812-18815) | PASSED |
| 6 | A tidak bisa baca B | test_multi_customer::test_isolasi_data_antar_customer | PASSED |
| 7 | Manipulasi ID URL/body | test_multi_customer::test_manipulasi_account_id_tidak_berpengaruh, test_detail_trade_customer_lain_404 | PASSED |
| 8 | Restart A tidak ganggu B/C/D | test_terminals::test_evict_satu_runtime_tidak_mengganggu_lainnya, test_sync_ulang_setelah_evict | PASSED |
| 9 | Gagal sync A, B/C/D jalan | test_multi_customer::test_kegagalan_sinkron_satu_akun_terisolasi | PASSED |
| 10 | Sync berulang tanpa duplikat | test_multi_customer::test_sync_berulang_tidak_duplikat + test_sync_idempoten_di_postgresql | PASSED |
| 11 | Broker/server berbeda | test_multi_customer::test_broker_berbeda_dapat_dikonfigurasi | PASSED |
| 12 | Tidak ada pemanggilan trading | test_readonly_static (AST + regex + whitelist + mock) | PASSED |
| 13 | Flutter analyze/test | Tidak dapat dijalankan di sandbox (tanpa SDK) — file test ditulis; jalankan di CI | ⚠ TIDAK LULUS DIKLAIM |
| 14 | Regresi fitur lama | 70 test lama tetap lulus (analytics, date filters, reconstruct, API bentuk respons) | PASSED |
| 15 | Kalkulator lot identik | Checksum SHA-256 4 file = baseline (lihat docs/CHECKSUM-PROTECTED-FILES.md) | IDENTIK |

## 3. Detail penting yang dibuktikan test

- `test_migrasi_pg_dari_db_kosong`: DB PostgreSQL kosong → migrasi 001 →
  13 tabel + 6 RLS policy; idempoten (jalan ulang tidak menambah).
- `test_empat_customer_empat_terminal`: port tercatat
  `[18812, 18813, 18814, 18815]`, 4 wine-prefix & 4 service unik.
- `test_isolasi_data_antar_customer`: dua customer, dataset berbeda —
  masing-masing hanya melihat trades miliknya (net 96.0 vs 196.0).
- `test_kegagalan_sinkron_satu_akun_terisolasi`: akun 1 gagal connect →
  status error utk akun 1, akun 2-4 tetap `ok`.
- `test_sync_idempoten_di_postgresql`: 3× sync di PG → tetap 2 deal,
  1 trade.
- Regresi angka laporan pengguna (Oktober 2026) dari suite lama tetap
  lulus tanpa perubahan: net +Rp22.036,79, 37 closed trades, delta 0.

## 4. Keterbatasan yang diakui (jujur)

1. Flutter tidak dianalisis/di-test di sandbox — harus lewat CI.
2. Tidak ada terminal MT5/Wine live, kredensial broker, atau RPyC ke
   mt5server asli — seluruh interaksi MT5 memakai MockMT5Client.
3. Uji kapasitas memakai latensi tersimulasi 20-80 ms per panggilan MT5;
   angka CPU/RAM/disk adalah overhead BACKEND saja, bukan beban Wine.
4. Rate limiter in-memory berlaku per proses (single-replica MVP);
   multi-replica butuh penyimpanan bersama (Redis) — issue terbuka.
