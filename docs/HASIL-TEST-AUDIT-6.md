# Hasil Test — Perbaikan Audit Round-6

> Branch: `fix/audit6-github-deploy-queue` (dari tip round-5
> `bc4bf81`). Satu temuan: workflow `backend-deploy.yml` sudah punya
> concurrency group + `cancel-in-progress: false` (round-5), tetapi
> belum `queue: max` — default GitHub Actions hanya mempertahankan
> SATU running dan SATU pending run per group; pending lama dapat
> dibatalkan dan digantikan pending terbaru, sehingga deploy produksi
> ber-approval bisa hilang dari antrian. Log lengkap di paket
> deliverable (SHA256SUMS.txt). **193 passed, 1 skipped** (skip =
> test live-sudo yang memang tidak tersedia di sandbox; jalankan di
> CI ubuntu-latest). Baseline sebelum round-6: **191 passed, 1
> skipped** (round-5) — tidak ada test lama yang dihapus; 2 test baru
> ditambahkan ke `tests/test_deploy_serialization.py`.

## TEMUAN — Concurrency production belum `queue: max`

**Temuan.** GitHub Actions secara default hanya mempertahankan satu
running dan satu pending run per concurrency group: saat run ketiga
datang, run pending lama DIBATALKAN dan digantikan oleh pending
terbaru. Untuk antrian deployment production, run yang sudah
di-approve reviewer bisa menghilang dari antrean tanpa jejak.

**Perbaikan** (`.github/workflows/backend-deploy.yml`, level
workflow — ketiga nilai wajib):

```yaml
concurrency:
  group: backend-deploy-production
  cancel-in-progress: false
  queue: max
```

- `queue: max` (temuan #1): hingga **100 run pending** dapat mengantre
  dan diproses SEQUENTIAL sesuai urutan masuk antrean; run tambahan
  yang datang saat antrean penuh (ke-101) **ditolak/dibatalkan** —
  tidak ada run di dalam antrean yang digusur.
- Group `backend-deploy-production` TETAP (temuan #2) — statis, tanpa
  `${{ }}`, monopoli workflow ini (dites statik round-5 tetap lulus).
- `cancel-in-progress: false` TETAP (temuan #3) — deployment berjalan
  tidak pernah dibatalkan; `queue: max` memang hanya valid bersama
  `cancel-in-progress: false` (GitHub Actions melarang kombinasi
  dengan `cancel-in-progress: true` — dites statik).
- Kontrak stamp/rollback round-4/5 TIDAK berubah: rollback tetap
  HANYA memakai `PRE_DEPLOY_STAMP` milik run yang sama — antrean
  berapa pun panjang, pasangan kode+database yang di-restorasi selalu
  pasangan run itu sendiri.
- Dokumentasi diperbarui (`docs/MULTI-CUSTOMER-BACKUP-ROLLBACK.md`
  §5.1): antrean maksimal 100 pending run + kemungkinan run tambahan
  ditolak ketika antrean penuh + konsekuensi operasional.

**Test** (statik + struktural, 2 baru):

- `test_concurrency_queue_max_group_dan_cancel_false_tiga_nilai` —
  regresi KETIGA nilai pada block workflow-level (kolom 0): block
  diurai baris per baris dan wajib PERSIS
  `{group: backend-deploy-production, cancel-in-progress: false,
  queue: max}` tanpa kunci lain; pesan galat spesifik per nilai untuk
  auditor.
- `test_yaml_concurrency_struktural_tiga_kunci_tanpa_job_level` —
  seluruh file di-parse `yaml.safe_load` (validasi YAML sungguhan):
  dict concurrency top-level = tepat tiga kunci bernilai benar
  (`False` boolean utk cancel-in-progress), dan TIDAK ada concurrency
  di level job (`guard`/`deploy`) — antrian produksi dikelola tepat
  satu di level workflow.
- Test round-5 tetap lulus tanpa perubahan (group monopoli antar
  workflow, cancel-in-progress tidak pernah true, wiring stamp/
  rollback, perilaku backup/rollback).

## Validasi YAML & shell

- `yaml.safe_load` pada `.github/workflows/backend-deploy.yml` →
  `{'group': 'backend-deploy-production', 'cancel-in-progress': False,
  'queue': 'max'}`; tiga workflow lain parse OK dan TIDAK memakai
  group produksi (monopoli tetap).
- `bash -n` OK untuk `make_backup_stamp.sh`, `backup_db.sh`,
  `rollback.sh` (tidak berubah di round-6).

## Ringkasan angka

| Item | Nilai |
|---|---|
| Test baru round-6 (`tests/test_deploy_serialization.py`) | 2 |
| Total suite | **193 passed, 1 skipped** (sebelum round-6: 191 passed, 1 skipped) |
| Protected files | blob identik baseline (4 file) — tidak disentuh |
| `.github/workflows/main.yml` | blob identik baseline — tidak disentuh |
| MT5 | READ-ONLY — tidak ada perubahan pada provider/terminals |
| Perintah | `python3 -m pytest backend` (rootdir `backend`, pytest.ini) |

Detail per-file: `docs/DAFTAR-FILE-DIUBAH.md` §Audit round-6.
