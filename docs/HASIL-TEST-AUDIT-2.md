# Hasil Test — Perbaikan Audit Round-2

> Branch: `fix/audit2-cli-deploy-rls` (dari `fix/audit-1-rpyc-rls-deploy`
> @ `7221f6e`). Tiga temuan auditor round-2, semua ditangani dan diuji.
> Log lengkap tersedia di paket deliverable (SHA256SUMS.txt).

## TEMUAN 1 — CLI mt5server tidak selaras dengan CLI resmi

**Temuan.** Wrapper `run-terminal.sh` round-1 menjalankan
`mt5server.exe "$RPYC_HOST" "$RPYC_PORT"` (argumen POSISIONAL), sedangkan
CLI resmi binary mt5server (rilis repo mt5linux; entrypoint identik dengan
`python -m mt5linux` = rpyc-classic server) hanya menerima **switch**
`--host <host>` dan `-p/--port <port>` — bukti live:
`Error: Expected at most 0 positional arguments, got [...]` (exit 2).
Wrapper lama membuat unit systemd crash-loop dan tidak pernah start.

**Perbaikan** (`backend/services/terminals.py`):
- Baris exec wrapper kini: `exec "$WINE_BIN" $SERVER_BIN --host
  "$RPYC_HOST" --port "$RPYC_PORT"` — host SELALU `127.0.0.1`
  (ditulis eksplisit, default rpyc pun localhost).
- Helper `mt5server_command()` = sumber tunggu argv resmi (wrapper + test).
- Health check tidak lagi menganggap TCP probe cukup: `probe_rpyc_session()`
  melakukan HANDSHAKE RPyC nyata (`rpyc.connect` + round-trip ke root);
  hasil dilaporkan jujur di `rpyc_handshake` (True/False/None bila rpyc
  tidak terpasang).
- Dokumen diselaraskan: `docs/MT5-UBUNTU-NO-DOCKER.md` §4/§8 dan
  `docs/MULTI-CUSTOMER-INSTALASI-PROVISIONING.md` §5 + `.env.example`
  kini mencontohkan `wine mt5server.exe --host 127.0.0.1 --port 18812`.

**Test** (`backend/tests/test_mt5server_cli.py`, 8 test — SEMUA LULUS):
- Artefak: wrapper memakai flag CLI, bukan posisional; unit systemd
  mendokumentasikan CLI resmi; helper argv benar.
- LIVE (sandbox, entrypoint identik mt5server.exe tanpa Wine):
  `python -m mt5linux --host 127.0.0.1 --port <bebas>` → server hidup,
  bind HANYA `127.0.0.1` (dicek `/proc/net/tcp`, bukan 0.0.0.0/ipv6),
  probe TCP + handshake RPyC nyata LULUS; bentuk posisional DITOLAK
  CLI (exit 2, "Expected at most 0 positional arguments").

**Batas jujur:** binary `mt5server.exe` via Wine + terminal MT5 + akun
Exness TIDAK tersedia di sandbox — live test memakai entrypoint Python
yang identik; klaim live MT5 penuh tidak pernah dibuat (wajib diuji
admin di VPS).

## TEMUAN 2 — Deploy workflow (environment, ENV_FILE, rollback)

**Perbaikan** (`.github/workflows/backend-deploy.yml`):
1. `environment: production` pindah dari level **workflow** ke **job
   deploy** — GitHub Environment approval hanya valid di level job.
2. `ENV_FILE` disatukan dengan dokumentasi systemd:
   `/opt/transcendence-hlwon-/backend/.env` (= EnvironmentFile pada
   `transcendence-backend.service`, docs §7). Tidak ada lagi
   `/etc/default/transcendence-backend`.
3. Rollback tidak lagi `git reset --hard origin/main`: commit
   **ter-deploy sebelumnya** dicatat ke `$BACKUP_DIR/PREVIOUS_COMMIT`
   sebelum deploy; rollback mengembalikan kode ke commit tersebut
   DIPASANGKAN dengan dump DB pra-deploy (diambil SEBELUM migrasi →
   skema cocok dengan kode lama = pasangan versi kompatibel).
4. Langkah normalisasi `.env` di awal deploy (bekas rollback ke DB
   restore dikembalikan ke DB utama sebelum kode baru jalan).

**Workflow baru** (`.github/workflows/backend-lint.yml`, tidak menjalankan
deployment): ruff subset fatal, validasi YAML semua workflow, guard
kebijakan deploy (trigger workflow_dispatch saja; environment hanya di
job deploy; ENV_FILE/APP_DIR path aktual; rollback wajib PREVIOUS_COMMIT;
dilarang reset origin/main), dan tripwire checksum 4 file kalkulator
(baseline `fbf844f`). Guard dijalankan lokal — LULUS.

## TEMUAN 3 — RLS diuji ulang dengan role non-owner/NOBYPASSRLS

Dijalankan ulang pada PostgreSQL **sungguhan** (pgserver, binary resmi
PostgreSQL):

- `backend/tests/test_rls_postgres.py` — 6 test LULUS, kini mencakup:
  atribut role dari `pg_roles` (rolsuper=f, rolbypassrls=f, rolcreatedb=f,
  rolcreaterole=f), `relrowsecurity` semua tabel ber-customer, matriks
  isolasi A→B (**SELECT 0 baris, UPDATE 0 baris, INSERT ditolak 42501**),
  kontrol A/B update data sendiri sukses, dan bukti **level psycopg2
  mentah** bahwa context (`set_config is_local=true`) tidak bocor antar
  transaksi (GUC kembali kosong setelah ROLLBACK; koneksi paralel tanpa
  context tetap default-deny).
- `backend/tools/rls_evidence.py` — transkrip bukti 19/19 LULUS:
  migrasi, role non-owner, RLS enable, default-deny, isolasi SELECT/
  UPDATE/INSERT antar customer, kontrol tulis sendiri, tidak-ada-leak
  antar transaksi & antar koneksi paralel, owner bypass (semantik
  migrasi; bukan jalur aplikasi).

RLS tetap defense-in-depth (brief §5) — bukan pengganti filter
`customer_id` di authorization backend (tetap diuji test lain).

## Ringkasan test

| Suite | Hasil |
| --- | --- |
| `pytest tests/` penuh | **132 passed** (round-1: 119; +8 CLI, +5 RLS) |
| `tests/test_rls_postgres.py` | 6 passed (PostgreSQL nyata via pgserver) |
| `tools/rls_evidence.py` | 19/19 bukti LULUS |
| `tests/test_mt5server_cli.py` | 8 passed (termasuk 2 LIVE CLI RPyC) |
| Guard workflow lint (lokal) | LULUS |
| Checksum 4 file kalkulator | identik baseline `fbf844f` |
| `main.yml` & `lib/` | tidak berubah (diff kosong) |

**Tidak dijalankan / tidak diklaim:** deployment (workflow tidak dieksekusi),
push/merge, live MT5 penuh (Wine + terminal + akun nyata tidak tersedia
di sandbox), flutter test (SDK tidak tersedia di sandbox; `lib/` tidak
diubah ronde ini).
