# Hasil Test — Perbaikan Audit Round-5

> Branch: `fix/audit5-deploy-serialization` (dari tip round-4
> `871f3655`). Dua temuan tambahan pada deployment: (1) workflow Backend
> Deploy tanpa concurrency group — dua run bisa paralel memodifikasi
> VPS/database yang sama, (2) stamp backup = timestamp detik — dua
> run/re-run pada detik sama menghasilkan stamp identik sehingga file
> backup saling menimpa dan backup lama bisa terkecoh dianggap milik
> run ini. Log lengkap di paket deliverable (SHA256SUMS.txt).
> **191 passed, 1 skipped** (skip = test live-sudo yang memang tidak
> tersedia di sandbox; jalankan di CI ubuntu-latest). Baseline sebelum
> round-5: **179 passed, 1 skipped** — tidak ada test lama yang dihapus;
> 12 test baru ditambahkan (`tests/test_deploy_serialization.py`).

## TEMUAN 1 — Serialisasi deployment production (concurrency group)

**Temuan.** Workflow `backend-deploy.yml` tidak punya block
`concurrency`. Dua `workflow_dispatch` bisa berjalan PARALEL dan
keduanya memodifikasi VPS/database produksi yang sama: backup dua run
saling menginjak, `LAST_PRE_DEPLOY`/`PREVIOUS_COMMIT` bisa tertulis oleh
run lain, migrasi bisa jalan di atas state run lain.

**Perbaikan** (`.github/workflows/backend-deploy.yml`, level workflow):

```yaml
concurrency:
  group: backend-deploy-production
  cancel-in-progress: false
```

- SATU antrian produksi: tepat satu deployment berjalan pada satu
  waktu; run berikutnya MENUNGGU (queued, tidak dibuang).
- `cancel-in-progress: false` — deployment yang sedang berjalan TIDAK
  PERNAH dibatalkan run baru (membatalkan di tengah jalan = backup/
  migrasi terpotong).
- Group STATIS tanpa `${{ }}` — group dinamis per-ref bisa membuat dua
  run dari ref berbeda tetap paralel di VPS yang sama.
- Group monopoli workflow ini: nama concurrency group GitHub Actions
  berlaku GLOBAL lintas workflow, jadi workflow lain (CI) tidak boleh
  memakai nama sama — dites statik (`test_group_production_monopoli_
  backend_deploy` memindai semua file `.github/workflows/*.yml`).
- Lapis kedua: job `deploy` tetap menarget environment `production`
  (proteksi reviewer) dan stamp backup unik per run (temuan 2).

**Test** (statik):
- `test_concurrency_group_khusus_production_di_level_workflow` — block
  ada TEPAT SATU di kolom 0 (level workflow, berlaku semua job), group
  = `backend-deploy-production` (memuat "production", tanpa `${{ }}`),
  `cancel-in-progress: false` eksplisit, tidak ada `true` di mana pun.
- `test_group_production_monopoli_backend_deploy` — tidak ada workflow
  lain yang memakai nama group sama.
- `test_wiring_run_id_run_attempt_ke_stamp_ke_rollback` — juga
  memverifikasi `environment: production` tetap pada job deploy.

## TEMUAN 2 — Stamp backup unik per run (run_id + run_attempt)

**Temuan.** `STAMP=$(date +%Y%m%d-%H%M%S)` = timestamp resolusi DETIK.
Dua run (atau re-run) pada detik yang sama menghasilkan stamp IDENTIK:
`backup_db.sh` menulis file final yang SAMA
(`pre-deploy-<stamp>.sql.gz`), isi saling menimpa, dan backup run lama
bisa terkecoh dianggap backup run ini — persis risiko yang round-4
tutup untuk kasus backup gagal.

**Perbaikan** (`backend/deploy/make_backup_stamp.sh` — baru; workflow
memanggilnya dengan `RUN_ID='${{ github.run_id }}'
RUN_ATTEMPT='${{ github.run_attempt }}'`):

- Stamp = identifier unik yang memuat run_id dan run_attempt:
  `run-<run_id>-attempt-<run_attempt>`. `run_id` unik per workflow run;
  `run_attempt` naik pada tiap re-run → dua run berbeda mustahil
  identik, re-run pun mendapat backup barunya sendiri.
- Deterministik: (run_id, attempt) sama → stamp sama.
- filename-safe: hanya `[0-9A-Za-z_-]` — sama dengan gate charset
  `backup_db.sh` dan `rollback.sh` (anti injeksi path; tanpa `/` tanpa
  `..` tidak bisa keluar dari `BACKUP_DIR`).
- Validasi numerik `RUN_ID`/`RUN_ATTEMPT` sebelum dipakai.

**Kontrak round-4 TETAP**: stamp diekspor ke `PRE_DEPLOY_STAMP`
SEBELUM backup dijalankan dan diteruskan ke `rollback.sh` — rollback
HANYA memakai backup milik run yang sama (lihat temuan 2 round-4).

**Test** (statik + perilaku):
- Statik: skema timestamp detik HILANG dari workflow
  (`test_skema_stamp_timestamp_detik_dihapus`); wiring
  `github.run_id`/`github.run_attempt` → `make_backup_stamp.sh` →
  `PRE_DEPLOY_STAMP` → `rollback.sh` dengan urutan ekspor SEBELUM
  backup (`test_wiring_...`); format + validasi numerik + pipefail pada
  skrip (`test_skrip_stamp_...`); charset gate konsisten di ketiga
  skrip deploy (`test_charset_gate_...`).
- Perilaku: generator dijalankan sungguhan — deterministik + unik antar
  run/attempt (`test_stamp_deterministik_dan_unik_...`); 40 pasangan +
  5 pasangan ulang dijalankan KONKUREN (12 worker) pada detik yang sama
  — kondisi yang membuat timestamp lama bisa bertabrakan — semua unik,
  deterministik, filename-safe (`test_stamp_unik_..._konkuren_60_...`);
  tolak input kosong/non-numerik/karakter aneh tanpa mengeluarkan stamp
  (`test_stamp_menolak_input_...`); stamp generator diterima penuh oleh
  `backup_db.sh` dan `LAST_PRE_DEPLOY` = stamp itu
  (`test_stamp_generator_diterima_penuh_...`).

## KONTRAK ROLLBACK — PRE_DEPLOY_STAMP milik run yang sama

**Test perilaku** (dua skenario, stub pg_dump/gzip/systemctl/git/psql):

- `test_dua_backup_konkuren_stamp_beda_tidak_saling_menimpa` — dua
  `backup_db.sh` KONKUREN, stamp `run-111-attempt-1` vs
  `run-222-attempt-1`, direktori backup sama: keduanya sukses, kedua
  dump valid dan isi masing-masing memuat marker run-nya sendiri,
  tidak ada `.part` tersisa, `LAST_PRE_DEPLOY` = salah satu stamp dan
  dump-nya ada. Defense-in-depth: walau serialisasi antrian dilanggar,
  stamp unik membuat backup antar-run mustahil bertabrakan.
- `test_scenario_attempt1_ok_attempt2_gagal_rollback_hanya_run_sama` —
  skenario penuh: attempt-1 backup SUKSES → attempt-2 (re-run run yang
  sama) backup GAGAL (pg_dump keluar kosong — ditolak validasi isi) →
  `LAST_PRE_DEPLOY` tetap attempt-1 → rollback dengan stamp attempt-2
  MENOLAK (exit 1): dump attempt-1 yang valid TIDAK di-restore, tidak
  ada systemctl/git/psql/createdb, `.env` utuh, "backup LAMA tidak
  boleh dipakai" → rollback dengan stamp attempt-1 me-restore PERSIS
  dump attempt-1 (terbukti via log stub gunzip) + `ROLLBACK OK`.

## Ringkasan angka

| Item | Nilai |
|---|---|
| Test baru file `tests/test_deploy_serialization.py` | 12 |
| Total suite | **191 passed, 1 skipped** (sebelum round-5: 179) |
| Protected files | blob identik baseline (4 file) — tidak disentuh |
| `.github/workflows/main.yml` | blob identik baseline — tidak disentuh |
| MT5 | READ-ONLY — tidak ada perubahan pada provider/terminals |
| Perintah | `python3 -m pytest backend` (rootdir `backend`, pytest.ini) |

Detail per-file: `docs/DAFTAR-FILE-DIUBAH.md` §Audit round-5.
