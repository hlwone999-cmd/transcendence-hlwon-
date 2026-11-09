# Checksum Protected Files (Kalkulator Lot) — Bukti Tidak Berubah

Brief §3: file inti algoritma harus identik **byte-for-byte** dengan
baseline. Diverifikasi `sha256sum` pada baseline commit
`fbf844ff4c5ceaa666e0a64f3e4c3b034b3c1333` vs working tree branch
`feat/multi-customer-mvp-postgresql` — dan `git diff` terhadap baseline
untuk keempat file **kosong** (nol perbedaan).

## SHA-256

| File | SHA-256 (baseline) | SHA-256 (branch) | Status |
|---|---|---|---|
| `lib/engine.dart` | `d04cfd730f9709fc4a0b9fb78f3e6c363277cca5da4b8eaa5240b2c7bbc10bf2` | `d04cfd730f9709fc4a0b9fb78f3e6c363277cca5da4b8eaa5240b2c7bbc10bf2` | ✅ IDENTIK |
| `legacy-web/script.js` | `8d553d78fec7094fd63f71f548fe2e04a80cac23eb4ac65323e4fbdcd2f39e5f` | `8d553d78fec7094fd63f71f548fe2e04a80cac23eb4ac65323e4fbdcd2f39e5f` | ✅ IDENTIK |
| `lib/montecarlo.dart` | `abd8edadf1716d7f1701f5eccf47a9839192e9e33fd2f1465192ca13e0ea801d` | `abd8edadf1716d7f1701f5eccf47a9839192e9e33fd2f1465192ca13e0ea801d` | ✅ IDENTIK |
| `lib/main.dart` | `f25edffb25788902e604a04a7fa649bc37c15dfaa0fb792b9c55ad834e97be0b` | `f25edffb25788902e604a04a7fa649bc37c15dfaa0fb792b9c55ad834e97be0b` | ✅ IDENTIK |

## File pendukung kalkulator yang juga tidak disentuh

`git diff baseline..branch -- test/engine_test.dart test/mc_pipeline_test.dart`
→ kosong: seluruh test Dart kalkulator/monte carlo utuh.

## Cara verifikasi ulang

```bash
git checkout feat/multi-customer-mvp-postgresql
sha256sum lib/engine.dart legacy-web/script.js lib/montecarlo.dart lib/main.dart
git diff fbf844ff4c5ceaa666e0a64f3e4c3b034b3c1333 -- \
  lib/engine.dart legacy-web/script.js lib/montecarlo.dart lib/main.dart
# keluaran harus kosong
```

## Keterangan integrasi

Kalkulator lot tidak membutuhkan perubahan apa pun untuk integrasi
multi-customer (data jurnal MT5 berada di jalur terpisah
`mt5_page.dart`/`mt5_api.dart`/backend) — tidak ada blocker, tidak ada
solusi alternatif yang diperlukan. Satu-satunya file Flutter yang
disentuh: `mt5_page.dart` (login/config), `pubspec.yaml` (dependency),
plus file baru `lib/mt5_auth.dart`.
