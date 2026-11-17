# Hasil Test — Perbaikan Audit Round-3

> Branch: `fix/audit3-deploy-hardening` (dari `fix/audit2-cli-deploy-rls`
> @ `a539aa8`). Empat temuan deployment + satu tambahan (cleanup scope
> RLS antar request) — semua ditangani dan diuji. Log lengkap di paket
> deliverable (SHA256SUMS.txt). **157 passed, 1 skipped** (skip = test
> live-sudo yang memang tidak tersedia di sandbox; jalankan di CI).

## TEMUAN 1 — Backup permission (root-owned dir vs tulis deploy)

**Temuan.** `sudo mkdir -p /var/backups/transcendence` membuat direktori
MILIK ROOT, sementara `gzip > $BACKUP_DIR/...` dijalankan user deploy →
penulisan dump GAGAL (Permission denied) atau bergantung kebetulan.

**Perbaikan** (`.github/workflows/backend-deploy.yml`):
- `sudo install -d -o $BACKUP_OWNER -g $BACKUP_OWNER -m 750 $BACKUP_DIR`
  (idempoten; owner/mode dikoreksi SEBELUM setiap penulisan).
- Dump + `PREVIOUS_COMMIT` + `APPROVED_COMMIT` + `LAST_PRE_DEPLOY`
  ditulis user deploy TANPA `sudo tee` (umask 077, `chmod 600`).

**Test** (`backend/tests/test_deploy_backup_permission.py`, 7 — 6 lulus,
1 skip):
- Statik: `install -d -o` + mode 750 ada (2×, sebelum penulisan);
  `sudo mkdir -p $BACKUP_DIR` dan `sudo tee $BACKUP_DIR` TIDAK ada;
  dump `gzip >` + `umask 077` + `chmod 600`; urutan izin < dump.
- Simulasi perilaku: direktori tanpa izin tulis → penulisan GAGAL
  (PermissionError dibuktikan); setelah langkah perbaikan → dump
  600 + dir 750 tertulis benar.
- Live root (sudo-gated, skip di sandbox): dir root-owned sungguhan →
  tulis GAGAL → `sudo install -d -o <user>` → tulis BERHASIL.

## TEMUAN 2 — Deploy immutable commit (bukan origin/<branch>)

**Temuan.** Deploy `git reset --hard origin/$(branch)` — origin bisa
bergerak SETELAH approval; commit ter-deploy ≠ commit ter-approve.

**Perbaikan**:
- Input `deploy_sha` (full 40-hex; default = SHA run) + **guard job**:
  (1) ref pemicu wajib `main` (`PROD_BRANCH`, aturan ref produksi
  eksplisit); (2) SHA wajib 40-hex; (3) `git merge-base --is-ancestor
  $SHA origin/main` — produksi hanya commit di sejarah main.
- VPS: `git checkout --detach '$DEPLOY_SHA'` (bukan reset origin);
  SHA tercatat `$BACKUP_DIR/APPROVED_COMMIT` (jejak audit).
- Lint guard baru menolak `git reset --hard origin/` dalam command line
  workflow apa pun.

**Test**: guard kebijakan (lint workflow) lulus; `test_rollback_script
.py::test_rollback_script_terpasang_di_workflow` memastikan skema
terpasang. Simulasi checkout SHA eksplisit diberkaskan oleh stub `git`
pada test rollback (`git checkout --detach abc1234` tercatat; tanpa
`reset --hard`).

## TEMUAN 3 — First-deploy rollback (bukan kode baru + skema lama)

**Temuan.** Saat `PREVIOUS_COMMIT=NONE`, rollback lama tetap me-restore
DB lama lalu membiarkan KODE BARU berjalan di atas skema lama.

**Perbaikan** (logika dipindah `backend/deploy/rollback.sh`):
- Jalur normal: kode → `$PREVIOUS_COMMIT` (detach), dump pra-deploy →
  `transcendence_restore`, `.env` → restore, restart, health.
- Jalur FIRST-DEPLOY (`PREVIOUS_COMMIT=NONE`): `systemctl stop` →
  DATABASE UTAMA dikembalikan ke kondisi pra-deploy → `.env` TIDAK
  diubah → service TIDAK di-restart (retry manual, runbook
  provisioning §10.3). TANPA dump: exit 1 tanpa perubahan apa pun.
- Gate SEBELUM migrasi: first-deploy pada DB yang SUDAH berisi skema →
  deploy DIBATALKAN tanpa perubahan (status tidak konsisten).

**Test** (`backend/tests/test_rollback_script.py`, 5 — SEMUA LULUS,
stub `systemctl/dropdb/createdb/psql/git/pip/curl/sudo`):
- Normal: `git checkout --detach <prev>` + `psql -q
  transcendence_restore` + restart + `.env` menunjuk restore + "ROLLBACK OK".
- First-deploy: `systemctl stop` ADA, `systemctl restart` TIDAK ADA,
  `git checkout` TIDAK ADA, DB utama di-restore, `.env` tetap,
  output memerintahkan retry manual.
- Tanpa dump: exit 1, NOL pemanggilan systemctl/dropdb/psql, `.env`
  utuh, "intervensi manual".
- Health gagal pasca-rollback normal → exit 1.

## TEMUAN 4 — Health check readiness (bukan 200 kosongan)

**Temuan.** `/api/v1/health` selalu 200 tanpa memeriksa PostgreSQL.

**Perbaikan** (`backend/api/routes.py`):
- Readiness: 200 hanya bila koneksi DB hidup DAN 8 tabel inti (brief
  §5) ada; 503 + `missing_tables` bila tidak; `get_db` gagal → 503
  terstruktur (bukan 500).
- Terminal MT5 dilaporkan TERPISAH (`mt5_terminals`) — TIDAK pernah
  menentukan 200/503 (tanpa kewajiban MT5 live).
- Baru: `GET /api/v1/admin/health/terminals` (token admin) — probe TCP
  + handshake RPyC per customer, `?deep=1` menambah `terminal_info`
  read-only; hasil jujur (`reachable=False` tanpa listener).

**Test** (`backend/tests/test_health_readiness.py`, 8 — SEMUA LULUS):
skema lengkap → 200; `DROP TABLE mt5_orders` → 503 + missing_tables;
`get_db` gagal → 503 + pesan; tanpa MT5 live → tetap 200; respons
tanpa kredensial; admin endpoint 401 untuk anonim & token customer;
probe admin jujur; koneksi tidak idle-in-transaction pasca request.

## TAMBAHAN — Cleanup scope RLS antar request (teardown Flask)

**Temuan.** Koneksi thread-local bertahan antar request; scope
`app.customer_id` (memori `_customer_id` untuk reapply) tidak pernah
dilepas → request unauth/admin BERIKUTNYA mewarisi scope customer
sebelumnya begitu transaksi baru di-commit.

**Perbaikan** (`backend/db.py`, `backend/app.py`, `backend/services/sync.py`):
- `PostgresConn.clear_customer_context()`: rollback transaksi tersisa
  (no-op bila idle — request sukses sudah commit, TIDAK dibatalkan) →
  `set_config('app.customer_id','',false)` (reset session) → hapus
  memori scope.
- Dipanggil: `teardown_appcontext` (tiap request) dan `finally`
  `sync_account` (worker).

**Test** (`backend/tests/test_rls_no_leak_requests.py`, 6 — SEMUA LULUS,
PostgreSQL nyata + Flask test client):
- A→B: scope lepas pasca request; commit baru TIDAK menghidupkan GUC;
  request B pada koneksi sama hanya melihat data B.
- A→unauth: 401/login-gagal/health tidak mewarisi scope.
- A→admin: admin tidak mewarisi scope; role aplikasi tanpa context
  melihat 0 baris ber-RLS.
- Data yang di-commit selama request tetap utuh setelah teardown.
- NEGATIVE CONTROL: cleanup dimatikan → scope MENYISA dan commit
  menghidupkan kembali GUC (membuktikan celah nyata, bukan test kosong).
- Worker `sync_account` membersihkan scope di `finally`.

## Ringkasan test

```text
backend/tests: 158 collected — 157 passed, 1 skipped (live-sudo)
  test_analytics.py 8 | test_api.py 11 | test_auth.py 13
  test_date_filters.py 16 | test_deploy_backup_permission.py 6+1skip
  test_health_readiness.py 8 | test_mt5server_cli.py 8
  test_multi_customer.py 9 | test_phase11_fixes.py 7
  test_postgres_migration.py 6 | test_provider_readonly.py 13
  test_race_conditions.py 4 | test_readonly_static.py 5
  test_reconstruct.py 8 | test_rls_no_leak_requests.py 6
  test_rls_postgres.py 6 | test_sync.py 7 | test_terminal_health.py 6
  test_terminals.py 5 | test_rollback_script.py 5
```

## Batas jujur

- Live MT5 penuh (Wine + terminal64.exe + mt5server.exe + akun uji)
  TETAP belum dijalankan — membutuhkan VPS; tidak ada klaim live MT5.
- Test live-sudo (izin backup pada direktori root-owned sungguhan)
  di-skip di sandbox tanpa passwordless sudo; di CI ubuntu-latest test
  ini berjalan penuh.
- Tidak ada push, merge, atau deployment yang dijalankan.
