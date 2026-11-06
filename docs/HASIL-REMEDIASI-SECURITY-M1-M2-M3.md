# Hasil Remediasi Security — M1/M2/M3 `lib/mt5_auth.dart` (PR #4)

> Permintaan owner: tiga temuan security review `lib/mt5_auth.dart`
> (M1 token plaintext, M2 HTTP 401, M3 URL HTTP produksi) diperbaiki,
> semua test dijalankan ulang, lima protected files wajib identik.
> Status: **SEMUA TEMUAN DITUTUP — JANGAN MERGE, JANGAN DEPLOY**.
> Commit remediasi: `8b03d72` (kode + test). Dokumen ini: `docs/`
> commit terpisah.

## Catatan recovery sandbox

Sandbox implementasi di-reset oleh sistem dan repositori kembali ke
snapshot `2e3ebde` (sebelum commit lokal). Empat commit yang sudah
diverifikasi pada sesi sebelumnya (**e9a6ab3, cd7cac0, f10c2aa,
d8bc1c1**) hilang dari disk bersama snapshot tersebut — tidak pernah
di-push, sehingga tidak ada di GitHub. Isi keempatnya diterapkan ulang
dan diverifikasi ulang dari nol sebagai:

| Commit lama (hilang) | Commit pengganti | Isi |
|---|---|---|
| e9a6ab3 | `a6ae663` | pyyaml ke pip Backend CI |
| cd7cac0 + f10c2aa | `076171c` | share_plus ^12.0.2 + file_picker ^11.0.3 + adaptasi journal.dart + pubspec.lock |
| d8bc1c1 | `ef9fcfc` | bug cast bindAccount/me + flutter_test_config.dart |

`docs/HASIL-TEST-CHECKPOINT-R22.md` dari sesi lama ikut hilang dan
tidak direproduksi; bukti lengkap penggantinya ada di dokumen ini.

## Resolusi dependensi (rantai win32)

Bentrok inti: `flutter_secure_storage 9.2.x` (terkunci di v9 API,
wajib owner) → `flutter_secure_storage_windows 3.1.2` → **win32 ^5.0.0**.
Sementara file_picker 12/13.x memakai paket federasi
`windows_file_picker` (semua versinya → **win32 ^6.3.0**, Dart ≥3.10)
dan share_plus 13.3.1 → win32 ^6.0.1. Keduanya mustahil dipertemukan
dengan win32 ^5.

Kombinasi final (dipilih agar secure_storage v9 tidak tersentuh dan
perilaku aplikasi tidak berubah):

| Paket | Versi terkunci | Constraint win32 |
|---|---|---|
| share_plus | 12.0.2 | ^5.5.3 |
| file_picker | 11.0.3 | ^5.9.0 |
| flutter_secure_storage | 9.2.4 (rentang ^9.2.2) | → fss_windows 3.1.2 → ^5.0.0 |
| **win32 (resolved)** | **5.15.0** | memenuhi semua ^5 |

`file_picker` 11.0.3 tetap menyediakan `FilePicker.pickFiles` statis,
hanya saja mengembalikan `FilePickerResult?` (bukan `List<PlatformFile>`
seperti 13.x) dan `PlatformFile` tanpa `readAsBytes()`. Satu-satunya
call-site (`lib/journal.dart`, tombol PILIH FILE EXCEL) diadaptasi:
byte dibaca via `File(path).readAsBytes()` — semantik identik dengan
implementasi `readAsBytes()` 13.x. Perilaku UX tidak berubah.

`flutter pub get` nyata (Flutter 3.47.1 • Dart 3.13.1 — sama dengan
`main.yml`) sukses tanpa konflik; `pubspec.lock` ikut di-commit
(`076171c`) dan pub get ulang menghasilkan nol diff.

## Diff per temuan (ringkas, `lib/mt5_auth.dart`)

**M1 — `SessionStore.readToken()`** (sebelum: baris 39–54 lama;
sesudah: baris 47–90):
- Baca secure storage; bila isinya token valid → bersihkan sisa
  `mt5_api_token` plaintext di SharedPreferences (best-effort), lalu
  return token secure.
- Bila fallback legacy dipakai: tulis ke secure storage, **verifikasi
  baca balik**, hapus plaintext HANYA jika verifikasi cocok; bila
  write/read/verifikasi gagal → token lama **dipertahankan** dan
  migrasi dicoba ulang di panggilan berikutnya.
- Sebelumnya: `saveToken(legacy)` tanpa pembersihan — plaintext
  tertinggal selamanya.

**M2 — `_send(..., {clearSessionOn401})`** (sebelum: baris 101–110
lama; sesudah: baris 175–206):
- Parameter baru `clearSessionOn401` = true untuk panggilan
  ber-token: `me()`, `bindAccount()`, `unbindAccount()`, `logout()`.
- 401 pada panggilan tersebut → `await store.clear()` sebelum melempar
  `Mt5ApiErrorKind.auth`.
- Timeout & kegagalan jaringan (catch `fn().timeout(25s)`) dan status
  5xx/JSON rusak → **tidak** menyentuh store.
- `login`/`register` sengaja tanpa pembersihan: 401 di sana berarti
  kredensial salah, bukan sesi basi — sesi valid tidak dihapus karena
  salah ketik password. (`logout` tetap membersihkan via `finally`.)

**M3 — konstruktor `Mt5AuthApi` + `_validatedBase` + `_isLoopback`**
(sebelum: `_uri` baris 89–94 menerima `http://` apa pun; sesudah:
baris 122–163):
- Validasi sekali di konstruktor: scheme `http` ke host non-loopback
  tanpa flag → `Mt5ApiError(badResponse, 'URL HTTP ditolak — produksi
  wajib HTTPS…')` (tertangkap `on Mt5ApiError` di `mt5_page.dart`,
  tampil sebagai toast ramah — tidak perlu ubah UI).
- HTTP diperbolehkan hanya untuk `localhost`, `127.0.0.0/8`, `::1`,
  atau `allowInsecureHttp: true` (development eksplisit).
- Host tanpa scheme tetap default `https://` — perilaku lama utuh;
  HTTPS tidak tersentuh.

Tidak ada perubahan pada: rumus/algoritma/konstanta/pembulatan/output
kalkulator lot, integrasi MT5 (read-only), backend authorization &
isolasi antar-customer, dan perilaku lain di luar ketiga temuan.

## Hasil test — SEBELUM vs SESUDAH

SEBELUM = HEAD `ef9fcfc` (baseline recovery, pra-remediasi).
SESUDAH = HEAD `8b03d72` (remediasi). Flutter 3.47.1 • Dart 3.13.1.

| Pemeriksaan | SEBELUM | SESUDAH |
|---|---|---|
| `flutter analyze` | 0 issue | 0 issue |
| `flutter test` | **114 passed** | **127 passed** (+13 baru: 4 M1, 5 M2, 4 M3) |
| Backend compile (`compileall`) | OK | OK |
| Security read-only static | 5 passed | 5 passed |
| Backend test penuh (SQLite) | 179 passed, 0 gagal, 2 skipped | 179 passed, 0 gagal, 2 skipped (junitxml) |
| PostgreSQL migration + E2E | — | 6 passed, 0 gagal (junitxml) |
| Ruff (E9,F63,F7,F82) | All checks passed | All checks passed |
| Validasi YAML 4 workflow | OK | OK |
| Guard kebijakan deploy | exit 0 | exit 0 |
| Tripwire kalkulator (sha256) | IDENTIK | IDENTIK |

Skips Backend CI (dokumentasi jujur, bukan disembunyikan):
1. `test_mt5server_cli.py:46` — mt5linux tidak terpasang di sandbox
   (uji live CLI); di CI ubuntu-latest juga dilewati kecuali
   dependency dipasang.
2. `test_deploy_backup_permission.py:160` — butuh passwordless sudo;
   semantik izin sudah dibuktikan lewat simulasi non-root; jalan di
   CI ubuntu-latest.

## Bukti penutupan tiap temuan (test → assertion)

- **M1** (`test/mt5_auth_test.dart`, group *M1*):
  - *migrasi BERHASIL*: `readToken()` mengembalikan token; secure
    storage berisi token; `sp.getString('mt5_api_token') == null`.
  - *migrasi GAGAL (write error)*: token tetap terbaca; secure storage
    kosong; plaintext **DIPERTAHANKAN**.
  - *verifikasi baca balik tak cocok*: plaintext DIPERTAHANKAN.
  - *secure sudah berisi token*: sisa plaintext ikut dibersihkan.
- **M2** (group *M2*):
  - `me()` 401 → error `auth` + store kosong (sesi dibersihkan).
  - login 401 (salah password) → sesi lama **tetap ada**.
  - `TimeoutException` → error `network`, token **tetap**.
  - `SocketException` → error `network`, token **tetap**.
  - HTTP 500 → error, token **tetap**.
- **M3** (group *M3*):
  - `http://jurnal.domain.com` → ditolak, pesan memuat "HTTPS".
  - `http://localhost:8000` jalan (URL request `http://localhost:8000/api/v1/auth/login`); `http://127.0.0.1:5000` dan
    `http://[::1]:9000` diterima.
  - `https://…` jalan; host tanpa scheme tetap `https://`.
  - `http://192.168.1.10:8000` ditolak tanpa flag, jalan dengan
    `allowInsecureHttp: true`.

## Bukti protected files identik (blob SHA vs baseline `fbf844f`)

| File | HEAD (`8b03d72`) | fbf844f | Status |
|---|---|---|---|
| `lib/engine.dart` | 1137cf2e…59f7 | sama | IDENTIK |
| `lib/montecarlo.dart` | 5e1d0cb4…1873 | sama | IDENTIK |
| `lib/main.dart` | 34cb6a1a…addd | sama | IDENTIK |
| `legacy-web/script.js` | 34f2cb17…2c76 | sama | IDENTIK |
| `.github/workflows/main.yml` | 26cb8f03…72d6 | sama | IDENTIK |

Tripwire sha256 (langkah Backend Lint) juga IDENTIK terhadap baseline
`fbf844f`. Bukti mentah: `workspace/evidence-remediasi/` (log 00–10 +
junitxml + pip freeze) dan salinan `bukti_backend_ci_remediasi.txt`.

## Commit (antara `2e3ebde` dan HEAD) & status working tree

1. `a6ae663` fix(ci): tambah pyyaml ke pip Backend CI
2. `076171c` fix(deps): share_plus ^12.0.2 + file_picker ^11.0.3
3. `ef9fcfc` fix(flutter): bug cast bindAccount/me + mock channel test
4. `8b03d72` fix(security): remediasi M1/M2/M3 mt5_auth
5. (docs) commit dokumen ini

Working tree bersih; pubspec.lock konsisten (pub get ulang = nol diff).
