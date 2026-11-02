# Filter Tanggal, Zona Waktu, dan Rekonsiliasi Profit (Phase 1B)

Dokumen ini mendefinisikan SEMANTIK EKSAK filter tanggal Transcendence,
kebijakan zona waktu, kebijakan atribusi profit, dan metode rekonsiliasi
terhadap terminal MetaTrader 5. Berlaku sejak branch
`feat/theme-datefilter-reconciliation`.

---

## 1. Semantik interval waktu

- **Zona kanonik**: semua timestamp disimpan & dibandingkan dalam **UTC**
  (format string ISO `YYYY-MM-DDTHH:MM:SSZ` di SQLite).
- Interval efektif setiap filter: **`[from, to)`** —
  - `from` **inklusif** (`close_time_utc >= from`);
  - `to` **eksklusif** (`close_time_utc < to`) saat parameter
    `to_exclusive=1` dikirim klien (mode default APK sekarang);
  - Tanpa `to_exclusive`, `to` inklusif (`<= to`) — dipertahankan untuk
    **kompatibilitas API lama**; `YYYY-MM-DD` tanpa jam diartikan
    akhir hari itu (23:59:59 UTC).
- Konsekuensi batas: trade yang menutup **tepat 00:00:00** tanggal batas
  masuk ke periode BARU, tidak dobel antara dua periode berurutan
  (interval `[A, B)` dan `[B, C)` saling lepas dan berurutan).
- Endpoint yang menghormati filter: `/journal/stats`,
  `/journal/trades`, `/journal/equity-curve`, `/journal/reconciliation`.

## 2. Preset tanggal

| Preset | Definisi eksak |
| --- | --- |
| 7 / 30 / 90 HARI | **Rolling**: `[now_utc − N hari, now_utc)`. Durasi bergerak, bukan hari kalender. |
| BLN INI | Kalender: hari ke-1 bulan berjalan 00:00 **zona tampilan** s/d hari ke-1 bulan berikutnya 00:00 zona tampilan, dikonversi ke UTC, `to` eksklusif. BUKAN rolling 30 hari. |
| BLN LALU | Kalender: bulan sebelumnya penuh, mekanisme sama. |
| SEMUA | Tanpa `from`/`to` (seluruh riwayat tersinkron). |
| PILIH TGL | Custom: mulai 00:00 zona tampilan s/d akhir hari terpilih (+1 hari 00:00, eksklusif). Divalidasi: mulai ≤ akhir. |

Interval aktif **selalu terlihat di UI** (label preset + tanggal mulai –
akhir + keterangan "batas akhir eksklusif").

## 3. Zona waktu

- **UTC** = zona penyimpanan & API. APK tidak pernah mengirim zona lain.
- **Zona tampilan** yang didukung fase ini: `Asia/Jakarta` (WIB, UTC+7)
  dan `UTC` — keduanya tanpa DST, offset dikodekan eksplisit di
  `lib/mt5_filters.dart` (`AppTimeZone`). Konversi **dilakukan sekali**
  di klien (dinding-waktu → UTC) sebelum kirim; backend tidak pernah
  mengkonversi ulang (tidak ada konversi ganda).
- Menambah zona IANA lain: struktur sudah siap, tapi zona **dengan DST**
  membutuhkan package `timezone` (belum ditambahkan — sengaja, sesuai
  batasan "tanpa dependensi tidak perlu").
- **Zona waktu server broker MT5: TIDAK DIASUMSI.** Terminal MT5
  menampilkan waktu server broker (bisa bukan UTC/WIB). Statusnya
  saat ini: **tidak dikonfirmasi**. Cara identifikasi manual:
  1. Buka terminal MT5 di VPS → tab *Market Watch* → catat jam yang
     ditampilkan terminal;
  2. Bandingkan dengan UTC saat itu (mis. `date -u` di VPS);
  3. Selisihnya = offset server broker (umumnya tetap per tahun,
     beberapa broker bergeser saat DST musim panas — periksa ulang).
  Selama belum dikonfirmasi, perbandingan angka Transcendence vs
  terminal harus memakai **interval eksplisit yang sama** (lihat §6),
  bukan "bulan ini" masing-masing aplikasi.

## 4. Definisi perhitungan (sumber: `services/analytics.py`)

- Setiap trade = **rekonstruksi deals** dengan `position_id` sama
  (bukan 1 deal = 1 trade). Partial close, scale-in/out, dan reversal
  netting ditangani `services/reconstruct.py`.
- `net_profit` trade = Σ deal (`profit + commission + swap + fee`).
- `gross_profit` = Σ net_profit trade closed yang > 0;
  `gross_loss` = |Σ net_profit < 0|; `net_profit` = Σ semua closed.
- `win_rate` = wins / (wins+losses+breakeven) × 100 — hanya trade
  **closed**. `profit_factor` = gross_profit / gross_loss (null bila
  tidak ada loss).
- **Deposit / withdrawal / bonus** (deal_type BALANCE, 2) TIDAK PERNAH
  masuk profit trading — hanya tampil sebagai langkah terpisah
  (`balance_ops_cum` di kurva).
- **Kurva** di tab MT5 berlabel "KURVA P/L TRADING (NET)": kumulatif
  net P/L trading, **bukan** kurva equity akun historis — snapshot
  equity historis tidak tersedia dari API riwayat MT5. Keterbatasan
  ini dinyatakan eksplisit di UI.

## 5. Kebijakan atribusi periode

- Trade diatribusikan **penuh** ke periode tanggal **penutupannya**
  (`close_time_utc`), meskipun membuka di periode sebelumnya.
- Trade yang masih **open** tidak masuk statistik closed; di daftar
  trade (endpoint `/journal/trades`) ia muncul memakai
  `COALESCE(close_time_utc, open_time_utc)` sebagai kunci filter.
- **Partial close lintas periode**: trade yang sebagian dealnya di
  periode A dan menutup di periode B dihitung penuh di B (basis
  close-date). Ini sumber selisih yang **wajar** antara:
  - basis *deal-execution-date* (History → Deals di MT5), dan
  - basis *position-close-date* (History → Positions di MT5).
  Endpoint rekonsiliasi menampilkan ketiga basis sekaligus
  (`from_deals`, `by_position`, `by_close`) supaya selisih bisa
  dijelaskan, bukan disembunyikan.

## 6. Metode rekonsiliasi vs terminal MT5

Endpoint: `GET /api/v1/journal/reconciliation?from=...&to=...&to_exclusive=1[&symbol=...]`

Respons memuat, untuk interval yang **identik**:
- `from_deals` — jumlah profit/komisi/swap/fee dari raw deals dalam
  interval (basis deal-execution-date);
- `by_position` — total net dari semua trade yang **terlibat** deals
  tersebut (trade penuh, termasuk deals di luar interval);
- `by_close` — total trade CLOSED yang menutup di interval (basis yang
  dipakai semua statistik Transcendence);
- `balance_ops` — deposit/withdrawal dalam interval (terpisah);
- `delta_close_vs_deals` — 0 berarti data konsisten; selain 0 harus
  terjelaskan oleh partial-close/trade lintas interval.

Langkah praktis membandingkan dengan MT5:
1. Tentukan interval eksplisit yang sama di dua sisi (tanggal + jam +
   zona!), mis. `2026-10-01T00:00:00Z` s/d `2026-11-01T00:00:00Z`
   (eksklusif).
2. Di terminal: History → klik kanan → **Custom period** sesuai
   interval itu (dalam waktu server broker — konversi dulu, lihat §3);
   catat Profit, Commission, Swap tab *Deals* dan tab *Positions*.
3. Bandingkan dengan `from_deals` (tab Deals) dan `by_close`/`by_position`
   (tab Positions) dari endpoint rekonsiliasi.
4. Selisih yang tersisa wajib dijelaskan (biasanya: window tidak
   identik, timezone salah konversi, atau trade lintas interval).

## 7. Akar masalah diskrepansi Oktober 2026 (investigasi)

**Angka laporan pengguna** (per 1 Nov 2026):
- Terminal MT5 (screenshot): **+228,257.81**
- APK, filter "30H": **≈ +843,000**
- DB Transcendence, Oktober 2026 UTC: 37 closed, **+22,036.79**

**Identitas matematis terverifikasi** (test sintetis
`backend/tests/test_date_filters.py::TestOctoberRegression`):
- `22,036.79 + 206,221.02 = 228,257.81` → angka MT5 konsisten dengan
  window yang **memulai setelah 1 Oktober**.
- `22,036.79 + 206,221.02 + 614,660.35 = 842,918.16 ≈ 843rb` → angka
  "30H" konsisten dengan window yang **memulai setelah 2 Oktober**.

**Akar masalah di kode (terkonfirmasi, bukan spekulasi):** filter lama
di `mt5_page.dart` hanya mengirim `from` berupa **tanggal saja**
(`YYYY-MM-DD`) yang dihitung dari **jam perangkat** (WIB) tanpa `to`,
dan backend menafsirkannya `>= tanggal 00:00 UTC` tanpa batas akhir.
Artinya "30 HARI" lama sebenarnya berarti **"sejak tanggal-0:00-UTC
itu sampai selamanya"** — bukan 30 hari terakhir. Bila tombol dipakai
pada tanggal N, tanggal mulainya bisa mundur/maju melewati hari-hari
tertentu dan memotong trade awal Oktober, sehingga totalnya jauh dari
bulan penuh.

**Yang TIDAK dapat dipastikan:** window persis yang dipakai saat
screenshot MT5 diambil, dan zona waktu server broker (lihat §3).
Karena itu diskrepansi **tidak dapat direproduksi secara konklusif
terhadap screenshot**; yang direproduksi dan didokumentasikan adalah
perilaku filter APK lama (fixture regresi di atas). Tidak ada nilai
profit yang diubah/dipaksa cocok — perbaikannya adalah **filter yang
eksplisit** (§1–§2), zona waktu eksplisit (§3), dan alat rekonsiliasi
(§6).

## 8. Keterbatasan

- Histori equity/balance per saat (snapshot) tidak tersedia → tidak
  ada kurva equity akun historis; drawdown dihitung dari kurva P/L
  trading closed. Persennya (`max_drawdown_pct`) berpenyebut puncak
  P/L kumulatif — BUKAN saldo akun; bila puncak <= 0 (akun belum
  pernah untung pada rentang tsb) API mengembalikan `null` dan UI
  menampilkan **N/A**, bukan 0% yang menyesatkan (Phase 1.1). UI
  memberi label eksplisit "CLOSED P/L" pada sel MAX DD dan pill DD.
- Zona broker belum dikonfirmasi; hingga dikonfirmasi, "BLN INI" di
  Transcendence (UTC/WIB) **tidak harus sama** dengan "Oktober" di
  terminal.
- `to_exclusive` adalah parameter API baru; klien lama (versi APK
  sebelumnya) tetap bekerja dengan semantik inklusif lama. Sejak
  Phase 1.1, cabang balance-ops pada `equity_curve` juga menghormati
  `to_exclusive` (sebelumnya selalu `<=`) — kini konsisten dengan
  cabang trades dan rekonsiliasi pada batas interval yang sama.
- Money disimpan REAL (float SQLite) dengan pembulatan 8 desimal;
  migrasi ke representasi desimal eksak masuk rencana Phase 4
  (PostgreSQL).
