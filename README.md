# Transcendence — Kalkulator Lot XAUUSD (IDR)

Aplikasi Flutter (Android) dari kalkulator lot XAUUSD berdenominasi Rupiah.
**Semua algoritma dan UI/UX dipertahankan 100% sama** dengan versi web
(arsip lengkapnya ada di folder [`legacy-web/`](legacy-web/)).

Cukup isi **maksimal SL (pips)**, balance, dan risk % — lot dihitung otomatis.
Dilengkapi tabel semua lot 0.01–10.00, matriks risk %, Rules Recovery 1–35,
kurs live, Monte Carlo, dan export CSV.

## Build & Install

### Cara cepat (GitHub Actions)
1. Push ke `main` → workflow **Build APK** berjalan otomatis.
2. Buka tab **Actions** → pilih run terbaru → unduh artifact **transcendence-apk**.
3. Install `app-release.apk` di HP (izinkan install dari sumber tidak dikenal).

### Build manual
```bash
flutter pub get
flutter build apk --release
# hasil: build/app/outputs/flutter-apk/app-release.apk
```

Dikembangkan dengan **Flutter 3.47.1 (stable, Dart 3.13.1)**.

## Struktur kode

| File | Fungsi |
|---|---|
| `lib/engine.dart` | Seluruh algoritma: parseAmt, kalkulasi lot, tabel, matriks, Rules Recovery, format angka id-ID |
| `lib/montecarlo.dart` | Simulasi Monte Carlo (port 1:1 `runMC`, dijalankan di Isolate) |
| `lib/charts.dart` | Grafik fan & histogram (replika canvas `drawMC`) |
| `lib/sticky_table.dart` | Tabel dengan header & kolom pertama menempel (replika `.tbl` web) |
| `lib/tables_build.dart` | Sel tabel rekomendasi & matriks (pick/tight/fit/me/dead) |
| `lib/ui_kit.dart` | Palet warna & komponen dasar (replika style.css) |
| `lib/main.dart` | Halaman utama, Rules Recovery, kurs live, export CSV, penyimpanan |
| `legacy-web/` | Versi web asli (index.html, script.js, style.css) sebagai arsip |
| `test/engine_test.dart` | Uji paritas angka antara port Dart dan perilaku web |

## Algoritma (identik dengan versi web)

```
Risk (Rp)    = Balance × Risk %
Risk (USD)   = Risk (Rp) ÷ Kurs USD/IDR
Lot          = Risk (USD) ÷ (Maksimal SL pips × 10)     → dibulatkan ke BAWAH per 0.01
SL pips      = Risk (USD) ÷ (Lot × 10)                  → untuk tabel semua lot
TP pips      = SL pips × rasio R:R
Target (Rp)  = Risk (Rp) × rasio R:R
```

Asumsi XAUUSD: 1 lot = 100 oz, 1 pip = $0,10 per oz, sehingga **0,01 lot × 10 pips = $1**.
Lot otomatis mentok **10.00 lot**; bila kebutuhan lebih besar, muncul peringatan
beserta SL minimal yang dibutuhkan.

## Fitur

- **Rules Recovery 1–35** — balance/risk/rasio terisi otomatis dari tabel PDF
  (pola: naik +20%, SL 10%, recovery 7,41%). Mode recovery 1:1 dengan X% —
  sekali menang balik persis ke balance rules sebelumnya.
- **Lot otomatis** dari maksimal SL + risiko aktual, TP pips, target profit,
  nilai per pip, batas 10.00 lot.
- **Tabel lot 0.01–10.00** (1000 baris, kelipatan 0.01) dengan baris terpilih
  ditandai emas dan otomatis di-scroll ke tengah.
- **Matriks SL pips × risk %** (1%–100% + risk terisi) dengan kotak bergaris
  emas untuk lot yang masih muat.
- **Kurs live** dari API publik (open.er-api.com, cadangan frankfurter.app),
  tanpa API key; tersedia kurs tetap Rp10.000.
- **Monte Carlo**: win rate, jumlah trade, spread/komisi, batas drawdown,
  compounding; grafik perjalanan balance & sebaran balance akhir.
- **Export CSV** tabel lot lewat share sheet Android.
- Input tersimpan otomatis (SharedPreferences, padanan localStorage web).
- Format angka fleksibel: `10.000.000`, `10,000,000`, `10jt`, `150rb`.
- Mode gelap/terang mengikuti sistem (palet sama dengan web).

## Catatan

- Spread, komisi, dan slippage belum masuk hitungan lot (hanya di Monte Carlo).
- Alat bantu hitung, bukan saran keuangan. CFD berisiko tinggi.
