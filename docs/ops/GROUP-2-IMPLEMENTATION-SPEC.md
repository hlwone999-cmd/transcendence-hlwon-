# GROUP 2 — Runtime & API Integration: Audit dan Implementation Specification

Status: **SPESIFIKASI v7 / BELUM DIIMPLEMENTASIKAN**. Dokumen ini hasil
audit read-only, direvisi sesuai review arsitektur. Tidak ada kode, file
repo, database, VPS, atau deployment yang diubah.

## Riwayat revisi

| Versi | Isi |
|---|---|
| v1 | Audit + spesifikasi awal (40 temuan A-01…H-04, 18 kritis; inventaris X1–X13) |
| v2 | Lima koreksi wajib dari review. Semua temuan v1 dipertahankan. |
| v3 | Tiga koreksi review final (K1 lock, K2 TCP6, K3 staging 009). Semua 40 temuan v1 serta I-01/I-02 dipertahankan; temuan baru I-03. |
| v4 | Tiga penajaman terakhir (asal `account_id` + batas registry, jalur `/proc/sys` vs `/sys`, snapshot backup konsisten). Semua 40 temuan v1, I-01…I-03 dipertahankan. |
| v5 | Tiga penajaman review akhir (bukti efektivitas `ipv6.disable=1`, fingerprint isi baris backup, inventaris writer M2). Seluruh desain, temuan, dan D1–D7 dipertahankan. |
| v6 | Lima koreksi review akhir (presisi namespace, versi minimum + capability probe SHA-256, urutan kanonik tabel tanpa PK, framing byte digest + versi format manifest, deteksi perubahan inventaris writer selama jendela). Semua temuan, X1–X13, D1–D7, dan desain v1–v5 dipertahankan. |
| **v7** | Tiga koreksi review independen (verifikasi inode namespace di kernel staging lewat jalur sudo nyata, urutan deterministik + agregasi duplikat untuk tabel tanpa PK, verifier password role hanya dibandingkan di dalam DB). Semua temuan, X1–X13, D1–D7, dan desain v1–v6 dipertahankan. |

Koreksi v2:
- **K-D4:** default helper non-produksi → `ro`; tidak ada classic diam-diam (§2.10 I-01, §4, §8, §11).
- **K-D2:** staging `migration` hanya setelah terminal RO teruji; kriteria bukti koneksi investor-password (§9.2).
- **K-LOCK:** urutan lock per jalur, semantik `evict`, tes concurrency dan provider disconnect (§3.5, §8).
- **K-009:** pengujian RLS PostgreSQL dengan role aplikasi, role sistem, dan migrator yang sebenarnya; indeks unik parsial; privilege; rollback; larangan menjalankan migrasi di DB produksi (§3.3, §8).
- **K-SG:** selfcheck memverifikasi helper yang **benar-benar** dipakai provisioner; startup berhenti sebelum HTTP dan scheduler (§3.6, §8).

Koreksi v3:
- **K1 (lock):** account lock tidak pernah dihapus atau diganti selama proses hidup, termasuk saat akun dihapus; hanya hilang saat restart. Ditambah tes race `account_lock()`/`evict()`/hapus akun (§3.5.1, §3.5.6, §8, §9.1).
- **K2 (TCP6):** fail-closed dipertahankan. Status IPv6 dibuktikan eksplisit (`enabled` / `disabled_verified` / `unproven`); readiness merah bila tidak terbukti (§2.10 I-03, §3.7, §8, §9.1).
- **K3 (staging 009):** urutan deployment staging eksplisit S0–S9; tidak ada penerapan migrasi otomatis dari kode G2 (§9.3).

Penajaman v4:
- **P1 (lock):** `account_id` hanya dari jalur yang sudah dibatasi autentikasi dan kepemilikan customer; lock baru hanya untuk akun yang terbukti ada dan dimiliki; batas keras registry; lock tetap tidak pernah dihapus (§3.5.1, §3.5.7, §8 LK-01…LK-09).
- **P2 (TCP6):** E2/E3 ditulis sebagai path procfs lengkap (`/proc/net/if_inet6`, `/proc/sys/net/ipv6`), dibedakan tegas dari sysfs `/sys`; tes empat skenario (§3.7.2, §8 TCP6-15…TCP6-20).
- **P3 (backup):** hitungan pembanding S1–S2 dari snapshot konsisten yang sama dengan backup, atau penulisan dihentikan selama snapshot; identitas snapshot, waktu UTC, dan bukti restore dicatat; DB aktif tidak diubah (§9.3 S1/S2, §9.3.1).

Penajaman v5:
- **Q1 (IPv6 disabled):** token `ipv6.disable=1` bukan bukti tunggal. Ditambah bukti efektivitas yang dapat diverifikasi tanpa sysfs: probe `socket(AF_INET6)` → `EAFNOSUPPORT`, ketiadaan berkas procfs IPv6 lain, token tunggal tanpa duplikat, dan netns helper = netns init. Tidak terbukti → `unproven`, merah (§3.7.2–3.7.4, §8 TCP6-21…TCP6-30).
- **Q2 (integritas isi backup):** fingerprint deterministik seluruh isi baris dengan serialisasi bertipe stabil, kolom sensitif di-hash di dalam DB dengan kunci per-run, hashing per chunk, perbandingan sumber vs restore, serta keterbatasan dan risiko collision (§9.3.2).
- **Q3 (inventaris writer M2):** inventaris semua role, sesi, transaksi prepared, replikasi, unit, timer, cron, dan integrasi eksternal; pemantauan selama jendela; pengecualian operator eksplisit dan terbatas; inventaris tidak lengkap → backup tidak sah (§9.3.3).

Koreksi v6:
- **R1 (namespace):** `/proc/1/ns/net` = netns PID 1 **di PID namespace yang terlihat helper**, bukan otomatis host. V5 kini juga mensyaratkan helper berada di PID dan user namespace **awal** (inode tetap kernel), sehingga PID 1 yang dibandingkan adalah init host. Asumsi lingkungan helper didokumentasikan; tidak terbukti → `unproven`, merah (§3.7.2b, §8 TCP6-31…TCP6-37).
- **R2 (SHA-256):** minimum versi ditetapkan PostgreSQL **16** (versi yang didukung proyek), ditambah *capability probe* wajib (fungsi `pg_catalog.sha256(bytea)` ada + vektor uji standar cocok). Gagal → fingerprint tidak dijalankan, backup tidak sah. Tanpa memasang ekstensi (§9.3.2).
- **R3 (tanpa PK):** satu urutan kanonik untuk semua tabel tanpa PK: urut bytewise `rh`. Aturan cakupan dan algoritma diselaraskan; PK teks diurutkan `COLLATE "C"` (§9.3.2).
- **R4 (framing):** format byte eksplisit `trx-manifest-v2` (length-prefix untuk semua komponen: nama tabel, kolom, tipe, jumlah baris, nomor chunk, parameter, label domain) (§9.3.2a).
- **R5 (perubahan inventaris):** deteksi perubahan role/privilege, `pg_hba`, unit/timer/cron, workflow, dan integrasi eksternal selama jendela (W9–W10). Tidak dapat dipantau → backup tidak sah (§9.3.3).

Koreksi v7:
- **T1 (namespace):** inode PID/user namespace awal **tidak** dianggap terverifikasi otomatis. Prosedur KV-1…KV-5 memverifikasinya terhadap kernel staging, lewat helper yang dipanggil melalui jalur `sudo` yang sebenarnya. Bukti diikat ke versi kernel (N6). Tidak cocok / tidak terbukti → `unproven`, merah. V5 tidak dilonggarkan (§3.7.2b, §8 TCP6-38…TCP6-42).
- **T2 (tanpa PK):** urutan total `(rh, row_bytes)` dengan agregasi duplikat identik menjadi `(rh, count)`; chunk berisi ≤ N kelompok berbeda; tanpa keyset `rh`. Format naik ke `trx-manifest-v3` (§9.3.2, §9.3.2a, MF-11…MF-16).
- **T3 (verifier password):** digest `pg_authid.rolpassword` tidak pernah keluar dari DB. Nilai awal disimpan di memori sesi monitor; setiap sampel hanya mengeluarkan `unchanged`/`changed` (§9.3.3, PW-01…PW-07).

Daftar perubahan v1→v2 ada di §13; v2→v3 di §14; v3→v4 di §15; v4→v5 di §16; v5→v6 di §17; v6→v7 di §18.

## 0. Baseline dan metode

| Item | Nilai |
|---|---|
| Branch | `security/remediation-checkpoint-b2-b12` |
| Commit | `273c774cf63def01eb75af0514c9f6d504c39eb3` (Group 1 v4) |
| Working tree | bersih (0 perubahan) saat audit |
| Cakupan | seluruh `backend/` non-tes, ditambah `backend/.env.example`, migrasi 005/006, `docs/RPYC-SECURITY.md` §6 |

**Metode.** Setiap temuan ditelusuri dari titik masuk sampai pemanggilan
provider atau helper. Hasil grep hanya dipakai untuk mencari titik awal.

Titik masuk yang ditelusuri:
- HTTP route di `api/routes.py`;
- thread scheduler (`MultiAccountScheduler._loop`);
- thread job aktivasi (`ConnectCoordinator.start`);
- thread sync awal (`_on_connected._sync`);
- pemulihan saat startup (`create_app`);
- `run.py`.

Nomor baris mengacu ke commit `273c774`.

### 0.1 Peta alur eksekusi (hasil penelusuran)

```
run.py:main ─ create_app (app.py:30)
   ├─ migrate()                                   (app.py:38)
   ├─ factory = _provider_factory(config)         (app.py:45,128)  → MT5LinuxProvider (classic) kecuali MT5_PROVIDER=json
   ├─ TerminalRegistry(config, provider_builder=None di produksi)  (app.py:51)
   ├─ build_provisioner → HelperProvisioner|MockProvisioner        (app.py:60)
   ├─ coordinator.recover_expired / retry_uncertain_removals        (app.py:64-76)
   ├─ SyncService(config, factory, registry)                       (app.py:78)
   └─ MultiAccountScheduler (produksi) | SyncScheduler (test/legacy) (app.py:81-86)
run.py:155 sched.start()   — TANPA guard konfigurasi/produksi/selfcheck

Scheduler thread:
 _dispatch_cycle (scheduler.py:86) → registry.active_account_ids (terminals.py:615)
   → registry.runtime_for (terminals.py:584) → _new_provider → _account_config (dekripsi kredensial)
       → MT5ReadOnlyServiceProvider (ro) | MT5LinuxProvider (classic, fallback untuk nilai apa pun ≠ 'ro')
 _worker (scheduler.py:117) → SyncService.sync_account (sync.py:155) → runtime_for → rt.lock
   → _run_sync → provider.connect/account_info/history/positions/symbol_info
   → error mirip auth broker → _handle_broker_rejection: DELETE mt5_credentials (sync.py:291)

HTTP:
 POST /accounts/bind (routes.py:481) → provision_terminal → _store_credentials (DELETE+INSERT kredensial)
   → [MT5_ENABLED=0] provisioner.install(TerminalSpec tanpa rpc_mode) (routes.py:577)
   → [MT5_ENABLED=1] create_job → thread run_job (provisioning.py:456)
        → spec_for → _acquire_terminal(job:) → install → start → _healthy (probe classic/ro)
        → live_verify_account (verification.py:65) → runtime_for → provider.connect
        → gagal → fail → _cleanup: DELETE mt5_credentials akun 'pending' (provisioning.py:665)
 POST /accounts/unbind (routes.py:612) → evict → DELETE mt5_credentials (routes.py:634) → remove_terminal
 POST /mt5/connect (routes.py:859)
   ├─ akun active → live_verify_account → auth_failed/master/identity → DELETE kredensial (routes.py:979)
   ├─ unbound/credentials_rejected → provision_terminal → _store_credentials → job reactivate
   └─ baru → register_customer → provision_terminal → _insert_account → job self_link
 GET /mt5/status (routes.py:1209) → runtime_for → provider.connect/account_info/disconnect (TANPA rt.lock)
 POST /mt5/sync (routes.py:1290) → sync_account
 GET /admin/health/terminals (routes.py:313) → health_check (probe classic/ro; deep → runtime_for tanpa lock)
```

**Ringkasan kondisi:**
- Tidak ada satu pun jalur runtime yang memanggil `security/rpc_policy.py`,
  dan tidak ada yang memanggil `fence_*`, `selfcheck`, atau `audit_all`.
  Group 1 belum ter-wire. Ini sesuai laporan checkpoint G1.
- Tabel `mt5_rpc_migrations` belum ada.

---

## 1. Batas Group 2 terhadap Group 1 dan Group 3

| Group | Isi | Status |
|---|---|---|
| **G1** (selesai, `273c774`) | `rpc_policy.py` (`check_terminal_usable`, `Capability`, `migration_readiness_problems`, `audit_inventory_problems`, secret/selfcheck/lease); helper (production, fence, `--expect-pid`/`--expect-mode`, `selfcheck`, `audit-all` fail-closed); `services/provisioner.py` (FenceError/PolicyError, argv baru) | ter-commit, BELUM dipakai runtime |
| **G2** (dokumen ini) | Wiring kebijakan ke **semua** jalur runtime/API/scheduler/sync; default `ro` (API **dan** helper non-produksi); produksi menolak classic; tanpa fallback; lock akun independen dari cache dengan urutan lock tetap; perlindungan kredensial saat migrasi; startup guard yang mengikat identitas helper; skema `mt5_rpc_migrations` (tabel saja, **tanpa worker**) | spesifikasi v2 |
| **G3** | Worker migrasi (state machine, CLAIM/takeover, `fence-advance`, lease, heartbeat), snapshot jurnal, backup + verify restore, endpoint admin lewat Unix socket SO_PEERCRED, CLI `rpc_migrate`, readiness RD1–RD11 lengkap | belum dimulai |

Prinsip pemisahan:
- **G2 hanya membaca baris migrasi; G2 tidak pernah menulisnya** (kecuali
  tes). Seluruh penulisan migrasi milik G3.
- G2 menyediakan antarmuka yang akan dipakai worker G3:
  - parameter `capability=` pada runtime/sync;
  - `registry.account_lock(account_id)`;
  - `registry.evict(account_id)`.
- Kontrak antarmuka itu dibekukan di §3.4.

---

## 2. Temuan audit

Format tiap temuan:
- **Kondisi:** apa yang terjadi sekarang.
- **Risiko:** dampak keamanan atau integritas.
- **Perubahan:** perubahan yang diperlukan.
- **Tes:** tes yang harus ditambahkan (ID didefinisikan di bagian 8).
- **Dep:** ketergantungan ke Group 1 atau Group 3.

Tingkat: **K** = kritis (classic, kehilangan kredensial, atau bypass
kebijakan), **T** = tinggi, **S** = sedang.

### 2.1 rpc_mode default dan konfigurasi

**A-01 [K] Default mode terminal baru = classic.**
- **Lokasi:** `config.py:140` (`mt5_rpc_mode_default = "classic"`), `config.py:256-258`, `.env.example:96`.
- **Kondisi:** terminal baru di-provision classic kecuali env diubah.
- **Risiko:** setiap customer baru mendapat server RPyC classic yang tidak terautentikasi. Server itu memungkinkan eksekusi kode arbitrer oleh proses apa pun yang bisa mencapai port loopback.
- **Perubahan:** default `"ro"`; `.env.example` diubah ke `MT5_RPC_MODE_DEFAULT=ro`.
- **Tes:** CFG-01, CFG-02.
- **Dep:** G1 `parse_rpc_mode`.

**A-02 [K] Parsing config jatuh diam-diam ke classic.**
- **Lokasi:** `config.py:256-258`.
- **Kondisi:** `"ro" if env == "ro" else "classic"`. Typo atau nilai tak dikenal seperti `"RO "`, `"r0"`, atau `"readonly"` menjadi classic tanpa peringatan.
- **Risiko:** fallback diam-diam ke classic.
- **Perubahan:**
  - pakai `rpc_policy.parse_rpc_mode`, sehingga nilai tak dikenal menghasilkan `ValueError`;
  - di `load_config`, galat itu menjadi `ConfigError` dan startup gagal (exit 78).
- **Tes:** CFG-03 (parametrize nilai salah).
- **Dep:** G1.

**A-03 [K] Tidak ada flag produksi maupun enforcement di sisi API.**
- **Lokasi:** `config.py` (field tidak ada).
- **Kondisi:** helper punya `production` (G1), API tidak punya.
- **Risiko:** API tidak tahu harus menolak classic, sehingga perilaku produksi bergantung pada konfigurasi helper saja.
- **Perubahan:**
  - **Field baru:**
    - `mt5_production: bool` (env `MT5_PRODUCTION`, default False);
    - `mt5_rpc_enforcement` (env `MT5_RPC_ENFORCEMENT`), divalidasi `rpc_policy.parse_enforcement`.
  - **Default enforcement:**
    - production → `strict` (nilai lain ditolak);
    - `TERMINAL_PROVISION_MODE=real` non-produksi → `migration`;
    - `mock` → `off`.
  - **`validate_production(cfg)`** menghasilkan daftar masalah. Produksi wajib:
    - enforcement `strict`;
    - `mt5_rpc_mode_default == "ro"`;
    - `terminal_provision_mode == "real"`;
    - `secret_strength_problem(mt5_rpc_secret)` bernilai None;
    - `mt5_enforce_investor_readonly` True;
    - `mt5_provider` bukan `mt5linux`/`json`.
- **Tes:** CFG-04…CFG-08.
- **Dep:** G1 `ENFORCEMENTS`, `secret_strength_problem`.

**A-04 [T] `MT5_PROVIDER=mt5linux` sebagai default.**
- **Lokasi:** `config.py:29`, `config.py:198`, `.env.example:7`.
- **Kondisi:**
  - nilai ini hanya dipakai factory legacy (A-05) dan field `provider` di `/mt5/status` (`routes.py:1213`);
  - provider per terminal dipilih dari `rpc_mode`, bukan dari config ini.
- **Risiko:** menyesatkan operator, dan mengaktifkan factory classic legacy.
- **Perubahan:**
  - default menjadi `"mt5ro"`;
  - di produksi, `mt5linux`/`json` ditolak oleh `validate_production`;
  - `/mt5/status` melaporkan `rpc_mode` terminal, bukan nilai config.
- **Tes:** CFG-06, API-12.
- **Dep:** —

**A-05 [K] Factory provider global: semua nilai selain `json` menjadi classic.**
- **Lokasi:** `app.py:128-136`, dipakai `app.py:45-46` dan `app.py:78`.
- **Kondisi:**
  - `MT5_PROVIDER=mt5ro` tetap menghasilkan `MT5LinuxProvider` (classic);
  - factory disimpan di `app.extensions["mt5_provider_factory"]` dan diteruskan ke `SyncService`.
- **Penelusuran:**
  - di produksi (`provider_factory=None`) jalur `SyncService.sync()` hanya dipanggil `SyncScheduler` (`scheduler.py:203`), yang hanya dibuat bila factory diinjeksi (`app.py:81-83`);
  - jadi saat ini tidak tercapai di produksi, tetapi tetap merupakan jalur classic laten yang hidup.
- **Risiko:** pemanggil baru (atau kode G3) yang memakai `mt5_provider_factory` diam-diam mendapat classic.
- **Perubahan:**
  - di produksi (dan bila enforcement ≠ `off`), factory global menjadi `_refusing_factory` yang melempar `PolicyViolation("legacy_factory_disabled")`;
  - `SyncService.sync()` legacy menolak bila enforcement ≠ `off`;
  - `mt5ro` tidak pernah dipetakan ke classic.
- **Tes:** RT-10, RT-11.
- **Dep:** —

**A-06 [S] Kolom DB `rpc_mode` DEFAULT `'classic'`.**
- **Lokasi:** `migrations/postgres/005_terminal_rpc_mode.sql:7-8` (SQLite sama).
- **Kondisi:** INSERT yang tidak menyebut `rpc_mode` menghasilkan classic. Satu-satunya INSERT (`terminals.py:392-400`) sudah menyebutnya secara eksplisit.
- **Risiko:** jalur INSERT baru di masa depan diam-diam menjadi classic.
- **Perubahan:** tabel `mt5_terminals` tidak diubah (sesuai spesifikasi final). Sebagai gantinya ditambahkan tes statis yang mewajibkan setiap `INSERT INTO mt5_terminals` menyebut `rpc_mode`.
- **Tes:** ST-03.
- **Dep:** —

**A-07 [S] Dokumen rollout masih mengizinkan kembali ke classic.**
- **Lokasi:** `docs/RPYC-SECURITY.md:118-122`.
- **Kondisi:**
  - fase 2 menuliskan rollback "kembali `classic`";
  - fase 3 menuliskan "unbind → connect ulang" dan rollback "connect ulang dgn default `classic`".
- **Risiko:**
  - prosedur resmi melanggar target "tanpa fallback";
  - "unbind" **menghapus kredensial** (lihat C-02).
- **Perubahan:** dokumen diperbarui. Migrasi hanya lewat worker G3, dan rollback ke classic dilarang. (Ini perubahan dokumen; bisa dilakukan di G2 atau G3.)
- **Tes:** —
- **Dep:** G3 untuk prosedur.

### 2.2 TerminalSpec dan provisioning

**B-01 [K] `TerminalSpec.rpc_mode` default `"classic"`.**
- **Lokasi:** `services/provisioner.py:116-121`.
- **Kondisi:** pemanggil yang lupa menyebut mode mendapat classic. Ditunda sengaja di G1.
- **Risiko:** helper menginstal classic. Di produksi helper menolak (exit 9), tetapi di staging/non-produksi instalasi berhasil.
- **Perubahan:**
  - default menjadi `"ro"`;
  - `validate_spec` menolak mode di luar `KNOWN_RPC_MODES`;
  - bila `cfg.mt5_rpc_enforcement != "off"`, `validate_spec` menolak `classic` untuk op `install`/`start` sebelum helper dipanggil (`PolicyError`).
- **Tes:** PV-01, PV-02.
- **Dep:** G1 `validate_spec`.

**B-02 [K] Install saat `MT5_ENABLED=0` tanpa `rpc_mode`.**
- **Lokasi:** `routes.py:574-583`, yang memanggil `TerminalSpec(code, port, provision_id)`.
- **Kondisi:**
  - `provision_terminal` mungkin mencatat `rpc_mode='ro'` di DB (bila default ro), tetapi helper diminta menginstal **classic**;
  - DB dan marker `.install_complete` pun tidak cocok;
  - kegagalan install hanya di-log, lalu `lifecycle='stopped'` tetap ditulis.
- **Risiko:**
  - classic terpasang padahal DB mengatakan ro;
  - start berikutnya ditolak, karena `--expect-mode` atau start produksi melihat classic dan keluar dengan exit 9;
  - terminal tidak konsisten.
- **Perubahan:**
  - spec dibangun dari baris DB lewat `coord.spec_for()`, dengan `rpc_mode` dan `expect_mode` dari DB;
  - install gagal → **tidak** menulis `stopped`; terminal ditandai `failed` dengan `last_error='install_failed'`.
- **Tes:** API-05.
- **Dep:** G1 marker `rpc_mode`.

**B-03 [K] `spec_for` jatuh ke classic.**
- **Lokasi:** `provisioning.py:268-270` (`rpc_mode=row["rpc_mode"] or "classic"`).
- **Kondisi:** nilai NULL atau kosong menjadi classic.
- **Risiko:** fallback diam-diam.
- **Catatan:** `rpc_policy.parse_rpc_mode(None)` justru menghasilkan `"ro"`. Fungsi itu **tidak boleh** dipakai untuk nilai DB, karena NULL di DB berarti data korup, bukan "default".
- **Perubahan:**
  - mode DB divalidasi ketat (`row["rpc_mode"] in KNOWN_RPC_MODES`, selain itu `PolicyViolation("rpc_mode_invalid")`, sehingga job gagal);
  - spec mengisi `expect_mode=row["rpc_mode"]`, sehingga helper start memverifikasi marker (`--expect-mode`, G1).
- **Tes:** PV-03, PV-04.
- **Dep:** G1 `--expect-mode`.

**B-04 [K] `provision_terminal` memakai ulang terminal milik customer tanpa melihat migrasi.**
- **Lokasi:** `terminals.py:306-308` dan `terminals.py:314-316` (`_own_terminal`).
- **Kondisi:** bind/reactivate mengembalikan terminal yang sedang dimigrasi (`op_owner='migrate:*'`). Kredensial baru sudah ditulis (`routes.py:558`, `routes.py:1051`) sebelum job gagal di `_acquire_terminal`.
- **Risiko:**
  - aktivasi bersaing dengan migrasi;
  - kredensial ditulis di tengah migrasi;
  - cleanup job menghapus kredensial akun `pending` (C-04).
- **Perubahan:** pemeriksaan migrasi dilakukan di **awal handler** (bagian 4.3), sebelum `_claim_pending` dan sebelum kredensial disentuh. `provision_terminal` juga menolak `_own_terminal` yang `op_owner` bukan NULL/`job:` dengan `TerminalBusy` (defense-in-depth).
- **Tes:** API-01, API-02, API-03.
- **Dep:** G2 tabel migrasi; G3 penulis migrasi.

**B-05 [T] Provisioning untuk classic lama saat reactivate.**
- **Lokasi:** `provisioning.py:496-502`, `routes.py:1032-1064`.
- **Kondisi:** akun `unbound`/`credentials_rejected` dengan terminal classic yang masih ada (remove sebelumnya gagal atau tertahan) menyebabkan job menjalankan `start` classic.
  - Produksi: helper exit 9 → `PolicyError` → job `provision_failed`.
  - Non-produksi `migration`: start classic berhasil, tetapi runtime menolak (classic_blocked) → verifikasi `unreachable`.
- **Risiko:** UX buruk dan siklus job gagal. Tidak ada eskalasi akses.
- **Perubahan:** handler mengembalikan 409 `terminal_needs_migration` sebelum `_claim_pending` bila terminal customer classic dan enforcement ≠ `off`. Migrasi ke ro adalah tugas G3.
- **Tes:** API-04.
- **Dep:** G3 (jalur migrasi).

**B-06 [S] `create_job` tidak menyimpan `rpc_mode`.**
- **Lokasi:** `provisioning.py:291-314`.
- **Kondisi:** job tidak mencatat mode terminal saat dibuat.
- **Risiko:** pemulihan tidak dapat membedakan job yang dibuat sebelum dan sesudah perubahan mode.
- **Perubahan:** tidak ada kolom baru. Mode tetap dibaca dari baris terminal di `spec_for` (sumber kebenaran tunggal), sehingga tidak ada duplikasi state.
- **Tes:** —
- **Dep:** —

### 2.3 API routes dan service layer

**C-01 [K] Unbind saat migrasi berjalan menghapus kredensial.**
- **Lokasi:** `routes.py:630-646`.
- **Kondisi:** hanya `state='pending'` yang ditolak (`routes.py:630`). Saat terminal dipegang `migrate:*`, kredensial tetap DIHAPUS (`routes.py:634`) dan akun menjadi `unbound`. `remove_terminal` lalu dilewati karena klaim gagal (`provisioning.py:818-829`, op_owner bukan NULL/pending).
- **Risiko:** migrasi kehilangan kredensial investor yang masih valid di tengah jalan, sehingga verifikasi G3 gagal. Ini melanggar target "migrasi tidak boleh menghapus kredensial valid".
- **Perubahan:** pemeriksaan migrasi (bagian 4.3) dilakukan sebelum `evict`/DELETE. Bila migrasi non-final atau `op_owner` `migrate:*` → 409 `migration_in_progress`, dan **tidak ada** yang diubah.
- **Tes:** API-06.
- **Dep:** G2 tabel migrasi.

**C-02 [K] `/mt5/connect` akun aktif bisa menghapus kredensial saat migrasi.**
- **Lokasi:** `routes.py:959-981`.
- **Kondisi:** `live_verify_account` dijalankan pada terminal yang mungkin sedang dimigrasi (terminal lama stop, baru belum commit). Bila klasifikasi `auth_failed`/`master_password`/`identity_mismatch` → state `credentials_rejected`, DELETE kredensial, dan semua sesi dicabut.
- **Risiko:**
  - di tengah migrasi, terminal baru dengan login atau server berbeda, atau galat initialize yang cocok dengan `BROKER_AUTH_MARKERS`, dapat menghapus kredensial yang valid;
  - customer bisa memicu ini sendiri dengan login ulang.
- **Perubahan:**
  - bila migrasi aktif: **tidak ada** live verify dan **tidak ada** jalur penghapusan;
  - setelah cocok password tersimpan (konstanta-waktu), token tetap diterbitkan dengan `connection_state="migration_in_progress"`, `live_verified=false`;
  - bila terminal classic dan enforcement ≠ off: `connection_state="terminal_needs_migration"`, tanpa verify.
- **Tes:** API-07, API-08.
- **Dep:** G2 tabel migrasi.

**C-03 [T] `/mt5/status` memakai provider bersama tanpa lock dan tanpa kebijakan.**
- **Lokasi:** `routes.py:1234-1251`.
- **Kondisi:** `runtime_for` → `connect` → `account_info` → `disconnect` tanpa `rt.lock`. Provider yang sama dipakai sync yang sedang berjalan.
- **Risiko:**
  - `disconnect()` status memutus sesi sync di tengah jalan (`MT5ReadOnlyServiceProvider.disconnect` memanggil `root.shutdown()`, sehingga terminal shutdown);
  - status ikut berbicara dengan terminal classic atau terminal yang sedang dimigrasi.
- **Perubahan:**
  - gerbang kebijakan dijalankan dulu; bila diblokir → `reason=<policy reason>`, `connected=false`, tanpa koneksi;
  - lock akun diambil non-blocking; bila sibuk → `reason="sync_in_progress"`, tanpa koneksi;
  - field `provider` diganti `rpc_mode` terminal.
- **Tes:** API-09, API-10, API-12.
- **Dep:** G2 lock akun (D-03).

**C-04 [T] Cleanup job menghapus kredensial akun pending.**
- **Lokasi:** `provisioning.py:661-676`.
- **Kondisi:** saat job gagal, kredensial akun `pending` DIHAPUS. Penelusuran: kredensial akun pending selalu kredensial yang **baru dikirim** di request itu.
  - bind: hanya dari `unbound` (`routes.py:519` menolak active/pending), dan kredensial lama sudah dihapus saat unbind atau credentials_rejected;
  - reactivate: dari `unbound`/`credentials_rejected`, juga tanpa kredensial lama yang sah;
  - self_link: akun baru.
- **Risiko:** saat ini tidak menghapus kredensial valid lama. Risiko muncul bila B-04 dibiarkan (job dibuat di atas terminal yang sedang dimigrasi).
- **Perubahan:**
  - tidak ada perubahan logika;
  - pertahankan invarian "akun pending tidak pernah memegang kredensial lama yang sah" dengan assert di `_store_credentials`: hanya boleh dipanggil saat akun `pending` dan prev_state ∈ (`unbound`, `credentials_rejected`), dan tidak pernah saat migrasi aktif;
  - tambahkan tes invarian.
- **Tes:** API-11.
- **Dep:** —

**C-05 [T] Health admin: probe classic dan deep tanpa lock atau kebijakan.**
- **Lokasi:** `routes.py:313-353`, ke `terminals.py:446-508`.
- **Kondisi:**
  - non-ro → `probe_rpyc_session` (handshake ke server classic, `terminals.py:479`);
  - `deep=1` → `runtime_for` + `connect` + `terminal_info` tanpa lock (`terminals.py:486-499`).
- **Risiko:**
  - probe classic menjadikan classic "sehat";
  - deep memutus sync atau berbicara dengan terminal yang sedang dimigrasi.
- **Perubahan:**
  - output menyertakan `rpc_mode`, `op_owner_kind` (`none|job|remove|pending|migrate`), dan `policy` (`ok` atau reason);
  - bila enforcement ≠ off dan terminal classic: **tidak** ada handshake classic, cukup `rpyc_handshake=null` dan `policy="classic_blocked"`;
  - deep hanya bila kebijakan `ok`, memakai lock akun non-blocking.
- **Tes:** HC-01…HC-04.
- **Dep:** D-03.

**C-06 [S] `mt5_sync` manual: galat kebijakan tidak dipetakan.**
- **Lokasi:** `routes.py:1290-1320`.
- **Kondisi:** hanya `SyncLockBusy` yang dipetakan (409). `PolicyViolation` dari runtime akan menjadi 500.
- **Risiko:** 500 mentah dan audit menyesatkan.
- **Perubahan:** `PolicyViolation` → 409 dengan `reason`. Audit `sync_manual` memuat `status="blocked_policy"`.
- **Tes:** API-13.
- **Dep:** —

### 2.4 Runtime MT5 dan siklus hidup RPC

**D-01 [K] `runtime_for` / `_new_provider` tanpa kebijakan; classic sebagai fallback.**
- **Lokasi:** `terminals.py:553` (`"mt5ro" if rpc_mode == "ro" else "mt5linux"`), `terminals.py:573-581`, `terminals.py:584-597`.
- **Kondisi:**
  - setiap nilai ≠ `ro` menjadi `MT5LinuxProvider`;
  - tidak ada `check_terminal_usable`;
  - provider dibuat walau terminal sedang dimigrasi.
- **Risiko:** classic dipakai sync, status, dan verifikasi tanpa hambatan. Inilah jalur utama aktivasi classic.
- **Perubahan:** **gerbang kebijakan tunggal** (bagian 4.1) di `runtime_for(conn, account_id, *, capability=None)`:
  1. baca baris terminal dan baris migrasi non-final lewat `system_conn`;
  2. jalankan `check_terminal_usable(terminal, migration, enforcement=cfg.mt5_rpc_enforcement, capability=capability)`;
  3. mode diambil dari baris, tanpa fallback (`ro` → `MT5ReadOnlyServiceProvider`; `classic` → `MT5LinuxProvider` **hanya** bila enforcement `off`);
  4. selain itu `PolicyViolation`.
- **Tes:** RT-01…RT-06.
- **Dep:** G1 `check_terminal_usable`; G2 tabel migrasi.

**D-02 [K] Cache runtime tidak memvalidasi ulang identitas atau kebijakan.**
- **Lokasi:** `terminals.py:586-589`.
- **Kondisi:**
  - cache hit dikembalikan tanpa membaca ulang DB;
  - perubahan `rpc_mode`, `provision_id`, `port`, atau `op_owner` (migrasi CLAIM/COMMIT) tidak terlihat;
  - provider lama dengan token lama, atau provider classic, tetap dipakai.
- **Risiko:** setelah COMMIT ke ro, runtime classic lama tetap dipakai. Saat migrasi, cache melewati kebijakan.
- **Perubahan:**
  - `AccountRuntime` menyimpan `identity = (terminal_id, provision_id, rpc_mode, port)`;
  - **setiap** `runtime_for` membaca ulang baris (murah: satu SELECT berindeks), menjalankan kebijakan, dan bila identitas berbeda → provider lama diputus dan dibuat ulang;
  - kebijakan dijalankan pada cache hit maupun miss.
- **Tes:** RT-07, RT-08.
- **Dep:** —

**D-03 [K] Lock sync per akun hidup di dalam objek cache.**
- **Lokasi:** `terminals.py:245-252` (`AccountRuntime.lock`), `terminals.py:599-607` (`evict`), `verification.py:148-149`.
- **Kondisi:**
  - `evict` membuang runtime beserta lock-nya, lalu `runtime_for` berikutnya membuat lock **baru**;
  - `live_verify_account` memanggil `evict` saat timeout ketika thread verifikasi masih hidup dan masih memegang lock lama.
- **Risiko:**
  - dua sync, atau sync dan verifikasi, berjalan bersamaan untuk akun yang sama;
  - duplikasi dan balapan tulis jurnal;
  - worker G3 tidak dapat menjamin "GATE + rt.lock".
- **Perubahan:**
  - lock dipindah ke `TerminalRegistry._account_locks: dict[int, threading.Lock]`, yang **tidak pernah** di-evict;
  - `AccountRuntime.lock` menjadi properti yang menunjuk lock registry;
  - `evict` hanya memutus provider, dan bila lock sedang dipegang, provider ditandai `stale` dan diputus oleh pemegang lock saat melepas;
  - API `registry.account_lock(account_id)` untuk G3.
- **Tes:** RT-09, CC-01, CC-02.
- **Dep:** G3 memakai `account_lock`.

**D-04 [T] Probe classic dipakai sebagai bukti sehat.**
- **Lokasi:** `provisioning.py:444-453` (`_healthy`), `terminals.py:479-480`.
- **Kondisi:** mode real + non-ro → `probe_rpyc_session` (handshake classic tanpa autentikasi).
- **Risiko:** server classic apa pun di port itu dianggap sehat.
- **Perubahan:**
  - `_healthy` hanya menerima ro (`probe_ro_session`) bila enforcement ≠ off; classic → `False`, sehingga job timeout lalu gagal;
  - `probe_rpyc_session` tetap ada hanya untuk enforcement `off` dan tes;
  - di produksi, `probe_ro_session` juga memeriksa bahwa probe **tanpa** token ditolak (RD8 ringan), sehingga server classic di port ro terdeteksi.
- **Tes:** HC-05, HC-06.
- **Dep:** —

**D-05 [S] `derive_token` dipanggil walau provider diinjeksi tes.**
- **Lokasi:** `terminals.py:555-560`.
- **Kondisi:** `rpc_mode='ro'` dan secret kosong → `MT5ConnectionError` di `_account_config` sebelum `provider_builder` sempat dipanggil.
- **Risiko:** bukan risiko keamanan, tetapi saat default berubah ke ro, banyak tes lama yang memakai builder mock akan gagal.
- **Perubahan:** token diturunkan di `_new_provider` hanya untuk provider nyata. Builder tes menerima `acc_cfg` tanpa token.
- **Tes:** RT-12.
- **Dep:** —

**D-06 [T] `MT5LinuxProvider` mengeksekusi kode di server classic.**
- **Lokasi:** `mt5client/mt5linux_provider.py:124-128` (`self._conn.execute(_SERVER_HELPER)`).
- **Kondisi:** desain classic memang menjalankan helper Python di server.
- **Risiko:** saluran eksekusi kode yang inheren pada classic.
- **Perubahan:**
  - `MT5LinuxProvider.__init__` menolak (`PolicyViolation("classic_blocked")`) bila konfigurasi yang dibawa `acc_cfg` menandai enforcement ≠ off. Defense-in-depth, walau gerbang D-01 sudah memblokir;
  - modul tidak dihapus di G2 karena masih diperlukan untuk enforcement `off` dan tes.
- **Tes:** RT-13.
- **Dep:** —

### 2.5 Scheduler dan background worker

**E-01 [K] Scheduler membuat runtime dan mensinkronkan tanpa kebijakan.**
- **Lokasi:** `scheduler.py:96-115` (`active_account_ids` → `runtime_for`), `terminals.py:615-623`.
- **Kondisi:**
  - akun `state='active'` dipilih tanpa melihat terminal;
  - `runtime_for` mendekripsi kredensial dan membuat provider untuk terminal classic atau yang sedang dimigrasi.
- **Risiko:** scheduler adalah pengguna classic terbesar (setiap interval). Ia juga bisa bersaing dengan worker migrasi.
- **Perubahan:**
  - `_dispatch_cycle` memanggil gerbang kebijakan;
  - `PolicyViolation` dicatat `per_account[id] = {last_status: "skipped_policy", reason}`;
  - **tidak** dihitung sebagai kegagalan atau backoff, dan tidak pernah memicu penghapusan kredensial;
  - `active_account_ids` tidak diubah (filter tetap di gerbang, sumber kebenaran tunggal).
- **Tes:** SC-01…SC-04.
- **Dep:** D-01.

**E-02 [T] Celah TOCTOU: kebijakan dicek sebelum lock.**
- **Lokasi:** `sync.py:173-185`.
- **Kondisi:** `runtime_for` lalu `rt.lock.acquire`. Bila worker G3 melakukan CLAIM di antaranya, sync tetap berjalan.
- **Risiko:** sync menulis jurnal setelah migrasi CLAIM (merusak snapshot_before G3).
- **Perubahan:**
  - urutan: `account_lock` → **gerbang kebijakan di dalam lock** → set scope → sync;
  - kontrak untuk G3: worker menulis `op_owner=migrate:*` (CLAIM) **sebelum** mengambil `account_lock`. Dengan begitu setiap sync yang mendapat lock setelah CLAIM pasti diblokir, dan sync yang sudah berjalan ditunggu worker.
- **Tes:** CC-03.
- **Dep:** G3 urutan CLAIM → lock.

**E-03 [S] Thread pemulihan startup tidak sadar migrasi.**
- **Lokasi:** `app.py:64-76`, `provisioning.py:919-946`, `provisioning.py:1019-1166`.
- **Kondisi (penelusuran):**
  - `recover_expired` hanya menyentuh `mt5_connect_jobs`;
  - tahap 1 `_retry_uncertain_removals` hanya `op_owner = 'job:'||id`;
  - tahap 2 hanya `removal-pending`/`remove:*`;
  - `_remove_terminal` hanya mengklaim `op_owner IS NULL` atau `removal-pending` atau milik job;
  - `claim_token` hanya `op_owner IS NULL OR LIKE 'job:%'`;
  - `_release_terminal` hanya milik job.
  - **Kesimpulan: tidak ada jalur pemulihan yang menyentuh `migrate:*`.** Ini hasil penelusuran, belum dibuktikan tes.
- **Risiko:** regresi di masa depan bisa diam-diam mengambil baris migrasi.
- **Perubahan:** tidak ada perubahan logika. Tambahkan tes regresi yang mengunci perilaku ini.
- **Tes:** RC-01…RC-05.
- **Dep:** —

**E-04 [S] `_on_connected` sync awal tanpa kebijakan.**
- **Lokasi:** `provisioning.py:583-597`.
- **Kondisi:** thread sync awal memanggil `sync_account`.
- **Risiko:** sudah tertutup oleh D-01/E-02 setelah perubahan. Tidak perlu jalur khusus.
- **Perubahan:** —
- **Tes:** SC-05.
- **Dep:** D-01.

### 2.6 Sync dan data jurnal

**F-01 [K] Heuristik teks dapat menghapus kredensial.**
- **Lokasi:** `sync.py:249-256` dan `sync.py:274-299`, dengan `mt5client/provider.py:64-73` (`BROKER_AUTH_MARKERS`).
- **Kondisi:** `MT5ConnectionError` yang teksnya cocok marker → `credentials_rejected`, DELETE kredensial, dan semua sesi dicabut.
- **Risiko:**
  - di luar migrasi: perilaku produk yang disetujui (Tahap 2);
  - di dalam migrasi: sync verifikasi G3 (dengan capability) **tidak boleh** menghapus kredensial, karena keputusan FAILED_CREDENTIALS milik worker (koreksi #5);
  - galat yang bukan dari broker tetapi mengandung marker juga bisa menghapus kredensial.
- **Perubahan:**
  - `_handle_broker_rejection` dilewati bila `capability is not None`; galat dikembalikan ke pemanggil sebagai `summary["auth_rejected"]=True` tanpa mutasi;
  - `_handle_broker_rejection` membaca ulang kebijakan di dalam transaksi: bila terminal kini `migrate:*` atau ada migrasi non-final → tidak menghapus apa pun, hanya mencatat `log.warning`.
- **Tes:** SY-01, SY-02, SY-03.
- **Dep:** G3 memakai `summary["auth_rejected"]`.

**F-02 [T] Sync tanpa parameter capability untuk worker.**
- **Lokasi:** `sync.py:155`.
- **Kondisi:** tidak ada cara bagi worker G3 menjalankan sync verifikasi pada terminal yang sedang dimigrasi.
- **Risiko:** G3 terpaksa membuat jalur sync kedua, yang membuka risiko bypass kebijakan.
- **Perubahan:**
  - signature `sync_account(account_id, ..., conn=None, capability: Capability | None = None)`;
  - capability diteruskan ke gerbang;
  - API dan scheduler **tidak pernah** mengirim capability (tes statis ST-02).
- **Tes:** SY-04, ST-02.
- **Dep:** G1 `Capability`; G3 pemakai.

**F-03 [S] Investor-only sudah ditegakkan.**
- **Lokasi:** `sync.py:307-308`.
- **Kondisi:** `enforce_investor_session` sebelum menulis apa pun. Fail-closed.
- **Risiko:** —
- **Perubahan:** pertahankan. Produksi wajib `MT5_ENFORCE_INVESTOR_READONLY=1` (A-03).
- **Tes:** CFG-07.
- **Dep:** —

**F-04 [S] Guard identitas akun sudah ada.**
- **Lokasi:** `sync.py:317-322`.
- **Kondisi:** login yang disajikan terminal ≠ akun ter-bind → sync batal.
- **Risiko:** —
- **Perubahan:** pertahankan.
- **Tes:** —
- **Dep:** —

### 2.7 Bind, unbind, reconnect, dan recovery

**G-01 [K] Pemeriksaan migrasi tidak ada di handler mana pun.**
- **Lokasi:** `routes.py:481` (bind), `612` (unbind), `859` (connect).
- **Kondisi:** lihat B-04, C-01, dan C-02.
- **Perubahan:** fungsi `_migration_guard(conn, customer_id)` di **baris pertama** setelah autentikasi dan validasi input, sebelum `recover_expired`, `_claim_pending`, enkripsi, atau DELETE.
  - Membaca lewat `system_conn`: terminal customer (`rpc_mode`, `op_owner`) dan migrasi non-final.
  - Hasil:
    - `none` → lanjut;
    - `in_progress` → 409 `migration_in_progress`;
    - `needs_migration` (classic dan enforcement ≠ off) → 409 untuk bind/reactivate; untuk connect-aktif, login tanpa verify (C-02);
    - `unknown` (tabel atau query gagal) → **503 fail-closed**.
- **Tes:** API-01…API-08, API-14.
- **Dep:** tabel migrasi G2.

**G-02 [T] Reconnect dan verifikasi memakai runtime ter-cache basi.**
- **Lokasi:** `verification.py:95`.
- **Kondisi:** `runtime_for` cache hit membawa identitas terminal lama (D-02).
- **Perubahan:** tertutup oleh D-02.
- **Tes:** RT-07.
- **Dep:** —

**G-03 [S] Verifikasi timeout melakukan `evict` saat thread hidup.**
- **Lokasi:** `verification.py:148-152`.
- **Perubahan:** tertutup oleh D-03, karena lock tidak ikut terbuang.
- **Tes:** CC-02.
- **Dep:** —

### 2.8 Pemakaian helper dan rpc_policy

**H-01 [K] Tidak ada pemanggil kebijakan G1.**
- **Lokasi:** seluruh backend (diverifikasi lewat penelusuran §0.1).
- **Kondisi:** `check_terminal_usable`, `audit_inventory_problems`, `selfcheck`, `audit_all`, dan `fence_*` tidak dipanggil dari runtime.
- **Perubahan:**
  - `check_terminal_usable` lewat gerbang D-01;
  - `selfcheck` dan validasi produksi di startup (H-02);
  - `audit_all` dan `audit_inventory_problems` di readiness G3;
  - `fence_*` di worker G3.
- **Tes:** seluruh RT/SC/API.
- **Dep:** G1; G3.

**H-02 [K] Startup tanpa guard produksi.**
- **Lokasi:** `run.py:151-163`, `app.py:30-86`.
- **Kondisi:** backend start dan scheduler berjalan walau konfigurasi produksi tidak lengkap, atau secret API ≠ helper.
- **Perubahan:** `startup_guard(cfg, provisioner)` di `create_app` **sebelum** `migrate()` dan sebelum scheduler.
  1. `validate_production(cfg)` (A-03).
  2. Mode real dan produksi:
     - `provisioner.selfcheck(nonce)`;
     - `verify_selfcheck_proof(cfg.mt5_rpc_secret, nonce, proof)`;
     - `production is True`;
     - `production_problems == []`.
  3. Gagal → `StartupGuardError` → `run.py` keluar dengan **exit 78** (EX_CONFIG) tanpa melayani HTTP.
  - Non-produksi: hanya peringatan.
- **Tes:** SG-01…SG-06.
- **Dep:** G1 `selfcheck`, `verify_selfcheck_proof`.

**H-03 [T] `expect_mode` tidak pernah diisi.**
- **Lokasi:** `provisioner.py:366-367` (argv mendukung), `provisioning.py:268` (spec tanpa `expect_mode`).
- **Kondisi:** helper tidak pernah memverifikasi mode marker saat start dari jalur job.
- **Perubahan:** `spec_for` mengisi `expect_mode` (B-03).
- **Tes:** PV-04.
- **Dep:** G1.

**H-04 [S] Status produksi wajib `--expect-pid`.**
- **Lokasi:** `provisioner.py:363-365`, ke helper `op_status` (G1).
- **Kondisi:** terminal lama dengan `provision_id` NULL → status tanpa `--expect-pid` → di produksi exit 2 → `listener_check` raise → `unreachable`.
- **Risiko:** fail-closed (benar), tetapi perlu terlihat.
- **Perubahan:** health/status melaporkan `policy="provision_id_missing"` untuk terminal ini.
- **Tes:** HC-07.
- **Dep:** G1.

### 2.9 Semua jalur yang berpotensi mengaktifkan classic RPyC (inventaris)

| # | Jalur | File:baris | Setelah G2 |
|---|---|---|---|
| X1 | provision default mode | `config.py:140,256`, `terminals.py:352-354` | `ro`; nilai salah → startup gagal |
| X2 | `TerminalSpec` default | `provisioner.py:121` | `ro`; classic ditolak di `validate_spec` bila enforcement ≠ off |
| X3 | install saat MT5 nonaktif | `routes.py:577` | spec dari DB + `expect_mode` |
| X4 | `spec_for` fallback | `provisioning.py:270` | validasi ketat, tanpa fallback |
| X5 | provider per akun | `terminals.py:553,580` | gerbang kebijakan; classic hanya bila `off` |
| X6 | factory global | `app.py:128-136` | menolak di enforcement ≠ off |
| X7 | sync legacy | `sync.py:141`, `scheduler.py:156-203` | menolak di enforcement ≠ off |
| X8 | probe classic (job) | `provisioning.py:452` | hanya `off` |
| X9 | probe classic (health) | `terminals.py:479` | hanya `off`; selain itu `classic_blocked` |
| X10 | health deep | `terminals.py:486-499` | lewat gerbang |
| X11 | helper non-produksi default classic | `mt5_provision_helper.py:1104,2190` | **v2:** default helper menjadi `ro` di semua mode (I-01). API tetap selalu mengirim `--rpc-mode` eksplisit (`provisioner.py:359`). Classic hanya bila `--rpc-mode classic` diminta eksplisit dan helper non-produksi |
| X12 | dokumen rollback ke classic | `docs/RPYC-SECURITY.md:118-122` | dihapus / dilarang |
| X13 | `.env.example` | baris 7, 96 | `mt5ro` / `ro` |

**Jalur order execution:** penelusuran tidak menemukan `order_send` dan
sejenisnya di kode non-tes.
- Provider punya daftar `FORBIDDEN` (`provider.py:45-58`) dan `__getattr__` yang menolak.
- `ro_service.READ_ONLY_METHODS` diassert identik dengan metode exposed (`ro_service.py:158-161`).
- Satu-satunya saluran eksekusi arbitrer adalah **server classic** itu sendiri (D-06), dan saluran itu ditutup oleh enforcement.
- Tes statis saat ini (`tests/test_readonly_static.py:29`) belum memindai `provisioner/`. Perluasan ini masuk ST-01.

### 2.10 Temuan tambahan v2 (dari review)

**I-01 [K] Helper non-produksi mengaktifkan classic bila `--rpc-mode` tidak diberikan.**
- **Lokasi:** `provisioner/mt5_provision_helper.py:2186-2190` (`a.rpc_mode = "classic"`, non-produksi), `mt5_provision_helper.py:1103-1104` (`op_install(..., rpc_mode: str = "classic")`).
- **Kondisi:**
  - pemanggilan CLI `install` tanpa `--rpc-mode` di helper non-produksi menginstal **classic**;
  - pemanggilan in-process `op_install` tanpa argumen mode juga classic.
- **Risiko:** jalur pemanggil lain (operator manual, skrip, tes, atau regresi API) dapat mengaktifkan classic diam-diam di staging.
- **Perubahan:**
  - `--rpc-mode` tidak diberikan → `"ro"`, di semua mode;
  - `op_install` default parameter `rpc_mode="ro"`;
  - bila helper tidak mampu menjalankan ro (`ro_service` nonaktif, template tanpa `ro_service.py`, secret lemah) → **exit 4 dengan pesan `mode ro tidak tersedia`**. Tidak pernah turun ke classic (perilaku `op_install:1110-1111` sudah fail-closed);
  - classic hanya lewat `--rpc-mode classic` eksplisit di helper non-produksi. Produksi tetap exit 9 (G1);
  - argparse `choices` tetap `("classic","ro")`, sehingga nilai lain menghasilkan exit 2.
- **Dampak tes lama (estimasi dari grep, bukan hasil run):**
  - ±31 pemanggilan `op_install(` tanpa `rpc_mode` di 6 berkas: `test_provision_helper.py`, `test_audit_pr4_parity.py`, `test_f2_b8_symlink.py`, `test_f2_tombstone.py`, `test_helper_fence_twoproc.py`, `test_helper_hardening.py`;
  - ≤34 baris argv `"install"` tanpa `--rpc-mode` di baris yang sama di 10 berkas (batas atas, karena argv bisa multi-baris).
  - Jumlah tes yang benar-benar gagal: **NOT MEASURED**.
- **Aturan pembaruan tes lama:**
  - tes yang menguji perilaku **classic** (skrip `RUN_TEMPLATE`, mt5server CLI, tombstone/symlink) diubah menjadi eksplisit `rpc_mode="classic"` / `--rpc-mode classic`;
  - tes yang tidak bergantung mode diubah ke konfigurasi ro-capable (`ro_service=True`, template berisi `ro_service.py`, secret kuat);
  - tes `test_h5_non_produksi_default_lama_classic` (G1) **diganti** dengan `test_h5_non_produksi_default_ro` (CLI-02);
  - **tidak ada tes yang dihapus atau di-skip** untuk membuat suite hijau.
- **Tes:** CLI-01…CLI-07.
- **Dep:** G1 helper (`dispatch`, `op_install`); parity `HELPER_KNOWN_RPC_MODES` tidak berubah.

**I-02 [K] Startup guard belum mengikat identitas helper.**
- **Lokasi:** `services/provisioner.py:575-578` (`build_provisioner`), `HelperProvisioner._spawn` (`sudo -n <provisioner_helper> ...`), helper `op_selfcheck` (`mt5_provision_helper.py:1800-1829`).
- **Kondisi:** `secret_proof = HMAC(secret, DOMAIN||nonce)` hanya membuktikan "proses yang menjawab mengetahui secret". Tidak terbukti bahwa:
  - proses itu adalah berkas helper yang dikonfigurasi di `PROVISIONER_HELPER`;
  - berkas itu tidak bisa ditulis non-root;
  - berkas itu berjalan sebagai root.
- **Risiko:** helper pengganti (path salah, symlink, salinan lama, berkas writable) yang membaca config yang sama tetap lolos selfcheck.
- **Perubahan:** §3.6.
- **Tes:** SG-07…SG-16.
- **Dep:** G1 `selfcheck`/`selfcheck_proof` (format bukti diperluas; parity diperbarui).

**I-03 [K] (v3) Ketidakhadiran `/proc/net/tcp6` tidak dapat dibedakan antara "IPv6 mati" dan "inventaris tidak lengkap".**
- **Lokasi:** `provisioner/mt5_provision_helper.py:1941-1956` (`_listening_ports_checked`, `for name in ("tcp", "tcp6")`), `mt5_provision_helper.py:2036-2158` (`op_audit_all`, `audit_complete`), `security/rpc_policy.py:277-294` (`audit_inventory_problems`).
- **Kondisi:**
  - tcp6 hilang → `proc_net_tcp6:tidak_ada` → `audit_complete=false` → readiness merah, **selalu**;
  - host dengan IPv6 yang sengaja dimatikan tidak pernah bisa hijau;
  - sebaliknya, tidak ada cara membuktikan IPv6 benar-benar mati.
- **Risiko:**
  - operator terdorong mematikan pemeriksaan;
  - asumsi "semua host punya IPv6", atau sebaliknya, tidak terverifikasi;
  - listener dual-stack (`::` dengan `IPV6_V6ONLY=0`) muncul **hanya** di tcp6, sehingga melewatkan tcp6 berarti melewatkan listener IPv4-mapped.
- **Perubahan:** §3.7. Fail-closed dipertahankan; ditambah bukti status IPv6 yang eksplisit.
- **Tes:** TCP6-01…TCP6-14.
- **Dep:** G1 helper audit-all dan `audit_inventory_problems` (diperluas di G2; parity diperbarui).

---

## 3. Arsitektur target G2

```
                      ┌──────────────── security/rpc_policy.py (G1, murni) ───────────────┐
                      │ check_terminal_usable · Capability · parse_rpc_mode/enforcement   │
                      └───────────────▲───────────────────────────▲───────────────────────┘
                                      │                           │
 config.validate_production ──► startup_guard (app.py) ──► selfcheck(nonce) helper (G1)
                                      │
            ┌─────────────────────────┴───────────────────────────┐
            │  services/rpc_gate.py (BARU, G2)                    │
            │  load_gate_rows(cfg, account_id|customer_id)        │  ← system_conn, read-only
            │  usable(cfg, rows, capability=None) -> GateResult   │  ← check_terminal_usable
            │  migration_guard(cfg, customer_id) -> none|in_progress|needs_migration|unknown
            └───────▲───────────────▲──────────────▲──────────────┘
                    │               │              │
   TerminalRegistry.runtime_for   routes (bind/unbind/connect/status/sync/health)
   (lock registry, identitas,      scheduler._dispatch_cycle (skipped_policy)
    provider tanpa fallback)       sync_account (gerbang DI DALAM account_lock)
```

### 3.1 Prinsip
1. **Satu gerbang.**
   - Semua jalur yang akan berbicara dengan terminal atau mengubah kredensial lewat `rpc_gate`.
   - Tidak ada pemeriksaan `rpc_mode == "ro"` yang tersebar di tempat lain, selain pemilihan kelas provider sesudah gerbang lolos.
2. **Fail-closed.** Kondisi berikut menghasilkan `PolicyViolation` atau 503, tidak pernah "lanjut":
   - query gerbang gagal;
   - tabel migrasi tidak ada;
   - nilai mode tidak dikenal;
   - enforcement tidak sah;
   - baris terminal hilang;
   - owner `migrate:*` tanpa baris migrasi.
3. **Tanpa fallback.** Mode dibaca dari DB dan diteruskan apa adanya ke provider dan helper (`--rpc-mode`, `--expect-mode`).
4. **Kredensial dilindungi selama migrasi.** Tidak ada DELETE `mt5_credentials` bila gerbang melaporkan migrasi aktif. Ini berlaku untuk unbind, connect, dan sync.
5. **Lock akun independen dari cache.**
6. **Pembacaan gerbang via koneksi role sistem.** Mengikuti pola B12.1: `system_conn`, SELECT saja, rollback. Scope customer pemanggil tidak berubah.

### 3.2 `services/rpc_gate.py` (baru)

```python
@dataclass(frozen=True)
class GateRows:
    terminal: Mapping | None        # id, customer_id, rpc_mode, op_owner, provision_id, rpyc_port, lifecycle
    migration: Mapping | None       # baris mt5_rpc_migrations NON-FINAL (maks 1) atau None

def load_gate_rows(cfg, *, account_id=None, customer_id=None) -> GateRows
    # system_conn; SELECT terminal JOIN akun; SELECT migrasi WHERE terminal_id=?
    # AND state NOT IN MIGRATION_FINAL_STATES. >1 baris non-final -> PolicyViolation("migration_multiple").
    # Galat DB / tabel tidak ada -> PolicyViolation("gate_unavailable").

def check(cfg, rows, *, capability=None) -> None   # raise PolicyViolation
    # check_terminal_usable(rows.terminal, rows.migration,
    #                       enforcement=cfg.mt5_rpc_enforcement, capability=capability)

def migration_guard(cfg, customer_id) -> str       # 'none'|'in_progress'|'needs_migration'|'unknown'
```

### 3.3 Skema `mt5_rpc_migrations` (migrasi 009, hanya menambah)

- **Kolom:** sesuai spesifikasi final §2.1:
  - `id`, `customer_id`, `terminal_id`, `account_id`, `code`, `port`;
  - `old_provision_id`, `new_provision_id`;
  - `state` dengan CHECK ∈ `MIGRATION_STATES`;
  - `generation`, `owner_tag`, `lease_until`, `step_deadline`;
  - `backup_proof`, `blocked_reason`, `last_error`;
  - `created_at`, `updated_at`.
- **Indeks unik parsial:** satu migrasi non-final per terminal.
  - PG: `CREATE UNIQUE INDEX ... ON mt5_rpc_migrations(terminal_id) WHERE state NOT IN ('done','cancelled','failed_credentials')`;
  - SQLite: sama (didukung).
- **RLS PostgreSQL**, mengikuti pola 007/008:
  - role aplikasi: `SELECT` dibatasi customer scope;
  - role sistem: `SELECT` lintas customer;
  - `INSERT`/`UPDATE` hanya role sistem (dipakai G3).
- **Rollback:** `rollback/009_rpc_migrations.down.sql` menolak (`RAISE EXCEPTION`) bila ada baris non-final.
- **Di G2 tabel ini selalu kosong di produksi**, sehingga gerbang hanya bergantung pada `rpc_mode` dan `op_owner`. Tabel tetap wajib ada agar gerbang fail-closed bekerja dan G3 tidak mengubah kontrak.

#### 3.3.1 Model hak akses 009 (v2, dibuat tegas)

| Role | Asal | SELECT | INSERT | UPDATE | DELETE | DDL |
|---|---|---|---|---|---|---|
| `transcendence_app` | 002 | hanya baris customer scope (`app_rls_allows(customer_id)`); tanpa scope → 0 baris | **ditolak** | **ditolak** | **ditolak** | ditolak |
| `transcendence_system` | 008 | lintas customer saat `app.scope='system'` (pola 008) | ya (untuk G3) | ya (untuk G3) | **ditolak** (riwayat migrasi tidak dihapus) | ditolak |
| migrator non-superuser | B12.4 | — | — | — | — | menjalankan 009 dan down 009 |

- Tabel **owner = migrator**, bukan role aplikasi.
- `ENABLE` + `FORCE ROW LEVEL SECURITY`.
- `REVOKE ALL ... FROM PUBLIC`.
- GRANT kolom/tabel eksplisit per baris tabel di atas.

#### 3.3.2 Aturan pengujian 009 (v2)

- **Hanya pada PostgreSQL ephemeral lokal (`pgserver`) atau DB tes CI.**
  - Tidak pernah memakai `DATABASE_URL` / `DATABASE_SYSTEM_URL` dari lingkungan.
  - Fixture menolak berjalan bila DSN tidak menunjuk ke cluster sementara yang dibuat tes itu sendiri. Cek: socket/datadir di bawah `tmp_path`, dan nama database berawalan `trx_test_`.
- Role yang dipakai tes adalah **role yang sebenarnya**, dibuat oleh migrasi 002/008 dan fixture migrator non-superuser B12.4 (`tests/test_f2_b124_hardening.py:198`, fixture `migr_db`):
  - tes membuka koneksi **login sebagai** `transcendence_app` dan `transcendence_system`;
  - tes tidak pernah memakai superuser dengan `SET ROLE` untuk membuktikan RLS, karena superuser dan `BYPASSRLS` melewati RLS;
  - setiap tes mengassert `rolsuper=false` dan `rolbypassrls=false` untuk role yang diuji.
- **Migrasi 009 dijalankan oleh migrator non-superuser**, lalu dijalankan ulang untuk membuktikan idempoten.
- **Down 009 dijalankan manual oleh migrator** (pola `rollback/008_system_role_rls.down.sql`: owner DSN, transaksi tunggal).
- **SQLite:** CHECK state dan indeks unik parsial diuji terpisah. RLS tidak berlaku di SQLite (dicatat; SQLite hanya dev/test).
- **Larangan:** implementasi G2 tidak menjalankan 009 pada database produksi atau staging. Penerapan ke staging/produksi adalah langkah deploy terpisah yang butuh persetujuan eksplisit (di luar G2).

### 3.4 Kontrak yang dibekukan untuk G3
- `TerminalRegistry.account_lock(account_id) -> threading.Lock` (sama untuk seluruh umur proses). **(v4)** Hanya mengembalikan lock yang **sudah ada**; lock baru dibuat lewat `ensure_account_lock` (§3.5.7), yang dipanggil `session()`.
- `TerminalRegistry.runtime_for(conn, account_id, *, capability=None)`.
- `TerminalRegistry.evict(account_id)`. Aman dipanggil kapan saja: provider diputus dan lock tidak berubah.
- `SyncService.sync_account(..., capability=None)` → `summary` dengan kunci tambahan `auth_rejected: bool`, `policy: str|None`.
- `rpc_gate.load_gate_rows`, `rpc_gate.check`.
- Urutan wajib worker:
  - CLAIM DB (`op_owner=migrate:<id>:<gen>`) → `evict` → `account_lock` (tunggu) → … → COMMIT → `evict` → … → DONE → `evict`.
- **(v2)** `TerminalRegistry.session(conn, account_id, *, blocking, timeout, capability=None)`: context manager yang menjadi **satu-satunya** cara memakai provider (§3.5).

### 3.5 Account lock: urutan dan aturan (v2)

#### 3.5.1 Objek lock
- `TerminalRegistry._account_locks: dict[int, threading.Lock]`, dijaga `TerminalRegistry._lock` (mutex dict).
- Lock dibuat sekali per `account_id` dan **tidak pernah diganti atau dihapus** selama proses backend hidup. Ini berlaku tanpa pengecualian, termasuk saat:
  - `evict`;
  - perubahan identitas;
  - unbind;
  - migrasi;
  - **penghapusan baris akun atau customer** (cleanup job, `_drop_customer_if_empty`, admin, CASCADE).
- **(v3)** Pengecualian v2 "dihapus bila baris akun dihapus" **dihapus**. Satu-satunya cara lock hilang adalah **restart proses** (registry baru, dict kosong).
- Pembuatan lock: `self._account_locks.setdefault(account_id, threading.Lock())` di bawah L3. Hanya ada satu objek per `account_id`, walau banyak thread memanggil pertama kali bersamaan.
- `account_lock(account_id)` **tidak** membaca DB. Lock untuk `account_id` yang tidak (lagi) ada tetap dikembalikan; validasi akun dilakukan gerbang di dalam lock.
- **(v4)** Klarifikasi: aturan di atas berlaku untuk lock yang **sudah ada**. **Pembuatan** lock baru hanya lewat `ensure_account_lock` dengan bukti keberadaan dan kepemilikan akun (§3.5.7). Lock yang sudah dibuat tetap tidak pernah dihapus (K1 tidak berubah).
- `AccountRuntime` **tidak lagi memiliki lock sendiri**. `rt.lock` adalah alias read-only ke `registry.account_lock(account_id)`, demi kompatibilitas tes lama.

#### 3.5.2 Hierarki lock (tanpa deadlock)

Lock diambil dari atas ke bawah, tidak pernah sebaliknya:

| Level | Lock | Catatan |
|---|---|---|
| L0 | Klaim DB migrasi (`op_owner=migrate:*`, G3) | baris DB, bukan lock thread; selalu **sebelum** L2 |
| L1 | `capacity_guard` / advisory lock provisioning | **tidak pernah** dipegang bersamaan dengan L2 |
| L2 | `account_lock(account_id)` | maksimum **satu** account lock per thread; tidak pernah dua akun sekaligus |
| L3 | `TerminalRegistry._lock` (mutex dict) | hanya untuk operasi dict; tidak pernah menunggu L2 sambil memegang L3 |
| L4 | `mt5_lock` di `ro_service` (sisi terminal) | di luar proses API |

Aturan tambahan:
- **Tidak ada panggilan helper (subprocess sudo) saat memegang L2**, kecuali `status` di `listener_check`. Pengecualian ini dibatasi `provisioner_timeout_seconds` dan sudah di luar lock pada alur job saat ini (`verification.py:85-93` sebelum `rt.lock`).
- Gerbang kebijakan dijalankan **di dalam** L2 (menutup TOCTOU, E-02).

#### 3.5.3 Urutan per jalur

| Jalur | Akuisisi L2 | Di dalam L2 (berurutan) | Bila L2 sibuk |
|---|---|---|---|
| **sync** (`sync_account`, scheduler worker, `/mt5/sync`, sync awal) | non-blocking | gerbang (+capability) → set scope RLS → `provider.connect` → baca → tulis jurnal → `provider.disconnect` → clear scope | `SyncLockBusy` (409 di API; scheduler skip tanpa backoff) |
| **verification** (`live_verify_account`: job aktivasi, connect akun aktif) | blocking, timeout ≤ 2 s | gerbang → `disconnect` (bersih) → `connect` (retry sampai deadline) → `account_info` → `disconnect` | `busy` |
| **verification timeout** | — | thread verifikasi **tetap** pemegang L2 sampai selesai sendiri (R5). Pemanggil hanya `evict` (§3.5.4), lock **tidak** diganti | sync/status berikutnya mendapat `busy` sampai thread selesai |
| **status** (`/mt5/status`) | non-blocking | gerbang → `connect` → `account_info` → `disconnect` | `reason="sync_in_progress"`, tanpa koneksi |
| **health non-deep** (`health_check`) | **tidak** mengambil L2 | probe TCP + `probe_ro_session` dengan koneksi **terpisah** (tidak memakai provider runtime) | — |
| **health deep** (`?deep=1`) | non-blocking | gerbang → `connect` → `terminal_info` → `disconnect` | `mt5={"skipped":"busy"}` |
| **scheduler dispatcher** | **tidak** mengambil L2 | hanya membaca `lock.locked()` sebagai petunjuk | skip siklus |
| **worker migrasi (G3)** | blocking dengan deadline, **setelah** L0 | gerbang + capability | tunggu sampai deadline → BLOCKED |

#### 3.5.4 Semantik `evict` (v2)
1. `evict(account_id)` mengambil runtime dari cache di bawah L3, lalu **melepas L3**.
2. Mencoba `account_lock.acquire(blocking=False)`:
   - **berhasil** (tidak ada pemegang): `provider.disconnect()` → release. Runtime dibuang;
   - **gagal** (sedang dipegang): runtime ditandai `stale=True`. `evict` **tidak** memanggil `disconnect` dan **tidak** mengganti lock.
3. Pemegang L2, saat keluar dari `session()`, memeriksa `stale`: bila True → `disconnect()`, dan runtime tidak dikembalikan ke cache.
4. `runtime_for` setelah `evict` membuat runtime/provider **baru** yang memakai **lock yang sama**. Pemakaian provider baru tetap menunggu L2.

#### 3.5.5 Invarian provider
- Provider hanya dipakai di dalam `registry.session(...)`, yang berarti sedang memegang L2.
- `runtime_for` tetap tersedia untuk kompatibilitas, tetapi pemanggilan method provider di luar `session` ditolak. Provider dibungkus guard yang memeriksa `account_lock` dipegang thread ini (`threading.get_ident()` dicatat saat acquire), dan pelanggaran menghasilkan `RuntimeError("provider_used_without_lock")`.
- Provider yang sudah `disconnect()` lalu dipakai lagi tanpa `connect()` → `MT5ConnectionError` (perilaku `is_connected` yang ada). Tes memakai provider palsu yang **menghitung** pemakaian setelah disconnect dan memastikan nilainya 0.

#### 3.5.6 Penghapusan akun saat lock dipegang (v3)
- Penghapusan baris akun/customer **tidak** menyentuh registry lock.
- Pemegang lock yang sedang berjalan menyelesaikan langkahnya. Tulisan berikutnya ke tabel yang ber-FK akun gagal karena FK/RLS (perilaku DB yang ada) dan ditangkap sebagai galat sync biasa. Tidak ada data yatim, karena FK `ON DELETE CASCADE` sudah ada.
- `session()` berikutnya untuk akun itu: gerbang → `terminal_missing` / akun tidak ada → `PolicyViolation`, tanpa provider dan tanpa dekripsi kredensial. Lock **tetap** objek yang sama.
- Runtime (provider) untuk akun terhapus dibuang dari cache oleh `evict` (§3.5.4). Lock tidak ikut dibuang.
- Penggunaan ulang ID:
  - PostgreSQL `BIGSERIAL` tidak memakai ulang ID;
  - di SQLite, ID bisa dipakai ulang bila tabel tanpa `AUTOINCREMENT`. Akun baru dengan ID sama memakai lock yang sama. Ini aman, karena lock hanya mutual-exclusion dan tidak membawa state akun;
  - tes CC-18 mengunci perilaku ini.
- Batas memori: jumlah lock = jumlah `account_id` berbeda yang pernah dilihat proses. Satu `threading.Lock` ≈ ratusan byte; dibatasi jumlah akun di DB. Health admin melaporkan `account_locks_count` untuk pemantauan.
  - **(v4)** Karena lock hanya dibuat untuk akun yang terbukti ada (§3.5.7), jumlahnya ≤ jumlah baris akun yang pernah ada selama umur proses, dan ditambah batas keras `ACCOUNT_LOCKS_MAX`.

#### 3.5.7 Asal `account_id` dan batas registry (v4, P1)

**Asal `account_id` per jalur.** `account_id` tidak pernah diambil langsung dari body, URL, atau header request.

| Jalur | Sumber `account_id` | Pembatas sebelum masuk registry |
|---|---|---|
| `/mt5/status`, `/mt5/sync`, `/accounts/unbind`, `/accounts/current` | `_own_account(conn)` (`routes.py:135`): baris akun customer dari token sesi, di bawah scope RLS customer | `require_customer` (token sah) + RLS |
| `/accounts/bind` | baris akun milik `g.customer_id` atau akun yang baru dibuat di request itu | `require_customer` + RLS + `_migration_guard` |
| `/mt5/connect` (akun aktif) | `_find_account` (login+server), **setelah** password cocok konstanta-waktu dengan kredensial tersimpan (`routes.py:940-949`) | rate limit + lockout + verifikasi password |
| `/mt5/connect` (reactivate/baru) | baris akun yang di-claim (`_claim_pending`) atau baru di-INSERT oleh request itu | rate limit + lockout + `_migration_guard` |
| job aktivasi / verifikasi | `mt5_connect_jobs.account_id` (ditulis handler di atas) | job dibuat hanya oleh handler terautentikasi |
| scheduler | `active_account_ids` dari DB | koneksi role sistem; hanya akun `active` |
| sync awal | `job["account_id"]` | sama dengan job |
| health admin | baris DB per customer | token admin (`_admin_token_ok`) |
| worker G3 | `mt5_rpc_migrations.account_id` | ditulis G3 (CLI admin lewat socket SO_PEERCRED) |

**Pembuatan lock (`ensure_account_lock(conn, account_id, *, expected_customer_id)`):**
1. Lock sudah ada → kembalikan objek itu (tanpa DB; K1).
2. Belum ada → **di luar L3**, baca lewat `system_conn`:
   `SELECT customer_id FROM mt5_accounts WHERE id=?`
   - baris tidak ada → `PolicyViolation("account_missing")`; **lock tidak dibuat**;
   - `expected_customer_id` diberikan dan ≠ `customer_id` baris → `PolicyViolation("account_owner_mismatch")`; lock tidak dibuat;
   - `account_id` bukan int positif (bool, str, negatif, > 2^63−1) → `PolicyViolation("account_id_invalid")` tanpa query.
3. Lolos → di bawah L3: bila `len(_account_locks) >= ACCOUNT_LOCKS_MAX` → `PolicyViolation("lock_registry_full")`, lock tidak dibuat, log ERROR, dan health admin `account_locks_full=true`. Selain itu `setdefault`.
4. Race (akun dihapus antara langkah 2 dan 3): lock tetap dibuat untuk ID yang tadi terbukti ada. Ini dibatasi jumlah baris yang pernah ada, dan gerbang di dalam lock menolak sesi berikutnya (§3.5.6).

`expected_customer_id`:
- **wajib** di semua jalur bertoken customer (`g.customer_id`);
- jalur sistem (scheduler, job, admin, G3) mengirim `customer_id` dari baris DB yang sama yang menghasilkan `account_id`.

**Batas keras:**
- `ACCOUNT_LOCKS_MAX` (config, default `max(64, 4 × (max_registered_customers + max_customers))`; default config 4 × 204 = 816).
- Saat batas tercapai, sistem **fail-closed**: sesi untuk akun yang **belum** punya lock ditolak (API 503 `lock_registry_full`, scheduler `skipped_policy`). Akun yang sudah punya lock tidak terdampak.
- Lock **tidak** dihapus untuk memberi ruang (K1). Pemulihan hanya dengan restart terencana; peringatan dimulai di 80%.

**Pertumbuhan lewat ID arbitrer tertutup** karena:
- tidak ada jalur yang meneruskan ID dari input klien;
- ID yang tidak ada tidak pernah membuat lock;
- ID milik customer lain tidak pernah membuat lock dari jalur bertoken;
- pembuatan akun sendiri dibatasi rate limit/lockout registrasi dan self-link yang sudah ada, ditambah batas keras.

### 3.6 Startup guard (v2): identitas helper yang benar-benar dipakai

#### 3.6.1 Urutan startup

Langkah 1–6 di `create_app`; langkah 7–8 di `run.py`.

1. `load_config()` → `validate_production(cfg)`.
2. `provisioner = build_provisioner(cfg)`. **Objek yang sama** dipakai untuk guard, coordinator, dan seluruh runtime. Tidak ada jalur helper kedua.
3. `startup_guard(cfg, provisioner)`, lihat 3.6.2. Gagal → `StartupGuardError`.
4. `migrate()`.
5. `TerminalRegistry`, `ConnectCoordinator`, `SyncService`, objek scheduler (belum di-`start`).
6. Pemulihan (`recover_expired`, `retry_uncertain_removals`). Ini memanggil helper `remove`, sehingga **wajib setelah guard**.
7. `run.py`: hanya bila `create_app` sukses → `sched.start()` → `serve()`.
8. `run.py`: `StartupGuardError`/`ConfigError` → log tanpa secret → `sys.exit(78)`.
   - **Sebelum** `sched.start()` dan **sebelum** `serve()`.
   - Tidak ada thread `mt5-sync-dispatch` / `provision-*` / `initial-sync-*`, dan tidak ada socket HTTP yang dibuka.

Mode mock (`TERMINAL_PROVISION_MODE=mock`) di produksi → `validate_production` gagal (A-03), sehingga produksi tidak pernah memakai `MockProvisioner`.

#### 3.6.2 Pemeriksaan (mode real; wajib di produksi, peringatan di non-produksi)

| # | Pemeriksaan | Sisi |
|---|---|---|
| SG-a | `provisioner` adalah `HelperProvisioner`; argv selfcheck dibangun oleh method yang sama dengan op lain (`_argv`/`_spawn`), sehingga `sudo` dan helper sama dengan yang dipakai install/start/stop/remove | API |
| SG-b | `cfg.provisioner_helper` dan `cfg.provisioner_sudo` absolut; `os.path.realpath(helper) == helper` (bukan symlink); berkas reguler | API |
| SG-c | helper dan setiap direktori induk sampai `/` dimiliki uid 0, tidak writable group/other (`st_mode & 0o022 == 0`) | API (`lstat`) |
| SG-d | API menghitung `sha256` berkas helper (`api_helper_sha256`) | API |
| SG-e | produksi: `api_helper_sha256 == cfg.provisioner_helper_sha256` (env baru `PROVISIONER_HELPER_SHA256`, pinned saat deploy; wajib di produksi) | API |
| SG-f | `selfcheck --nonce N` lewat `provisioner.selfcheck(N)` dengan `sudo -n`. Prompt password, timeout, atau exit ≠ 0 → gagal | API→helper |
| SG-g | output helper (G1 diperluas di G2): `helper_path` (= `os.path.realpath(__file__)` helper), `helper_sha256` (hash berkas yang sedang dieksekusi, dibaca dari path itu), `euid` | helper |
| SG-h | `helper_path == cfg.provisioner_helper`, `helper_sha256 == api_helper_sha256`, `euid == 0` | API |
| SG-i | **bukti terikat identitas:** `secret_proof = HMAC(secret, "transcendence-selfcheck-v3|" + N + "|" + helper_sha256)`. API memverifikasi dengan `helper_sha256` yang **dihitung API sendiri** (SG-d), sehingga jawaban dari berkas lain tidak lolos walau secret sama | API (rpc_policy) + helper (parity) |
| SG-j | `production is True`, `production_problems == []`, `secret_ok`, `lock_dir_ok`, `unclean_count == 0` | helper output |
| SG-k | nonce 32 byte acak baru setiap startup; tidak pernah disimpan | API |

**Catatan batas:** pemeriksaan ini mengikat **berkas** helper, bukan interpreter
Python yang menjalankannya. Shebang/interpreter dikunci oleh aturan sudoers dan
oleh SG-c pada direktori sistem, dan dicatat sebagai **asumsi deploy** di
runbook. Penegakan sudoers di luar cakupan kode.

### 3.7 Status IPv6 dan audit tcp6 (v3, K2)

#### 3.7.1 Konfigurasi deklaratif helper
- Kunci baru di `provisioner.json` (root-owned): `"ipv6_expected": "enabled" | "disabled"`.
  - **Tidak ada default diam-diam:** kunci hilang = `"enabled"` (paling ketat, karena tcp6 wajib ada).
  - Nilai lain → config tidak sah (exit 4).
- Deklarasi **tidak cukup**. Status hanya diterima bila bukti kernel sesuai (3.7.2).

#### 3.7.2 Bukti yang dibaca helper (semua relatif `proc_root`, read-only)

**(v4) Jalur absolut.** `proc_root` default `Path("/proc")` (`mt5_provision_helper.py:208`). Semua bukti dibaca **hanya** di bawah procfs `proc_root`. Path produksi:

| Bukti | Path produksi (procfs) | Path di tes |
|---|---|---|
| E1 | `/proc/net/tcp6` | `<tmp>/proc/net/tcp6` |
| E2 | `/proc/net/if_inet6` | `<tmp>/proc/net/if_inet6` |
| E3 | `/proc/sys/net/ipv6` (direktori sysctl procfs) | `<tmp>/proc/sys/net/ipv6` |
| E4 | `/proc/cmdline` | `<tmp>/proc/cmdline` |
| E5 (v4, informatif) | `/proc/sys/net/ipv6/conf/all/disable_ipv6` dan `.../conf/default/disable_ipv6` | `<tmp>/proc/sys/net/ipv6/conf/{all,default}/disable_ipv6` |
| E6 (v5) | `/proc/net/snmp6` | `<tmp>/proc/net/snmp6` |
| E7 (v5) | `/proc/net/ipv6_route` | `<tmp>/proc/net/ipv6_route` |
| E8 (v5) | probe kernel langsung: `socket(AF_INET6, SOCK_STREAM)` di proses helper (tanpa bind/listen/connect; ditutup segera) | probe diinjeksi (`af_inet6_probe`) |
| E9 (v5; v6 diperluas, lihat §3.7.2b) | namespace: `readlink /proc/self/ns/{net,pid,user}` dan `readlink /proc/1/ns/net` | `<tmp>/proc/self/ns/net`, `<tmp>/proc/1/ns/net` (symlink palsu) |

**`/proc/sys` ≠ `/sys`:**
- `/proc/sys/...` adalah antarmuka **sysctl** di **procfs**; itulah yang dipakai E3/E5.
- `/sys/...` adalah **sysfs** (mis. `/sys/module/ipv6`, `/sys/class/net`). Sysfs **tidak** dibaca dan **tidak** menjadi bukti apa pun.
- Helper tidak memakai konfigurasi `sys_root`. Tes memastikan bahwa memalsukan `<tmp>/sys/...` (sysfs palsu) tidak memengaruhi keputusan (TCP6-19).
- Penamaan di dokumen v3 (`sys/net/ipv6` relatif `proc_root`) berarti persis `/proc/sys/net/ipv6`.

| Bukti | Sumber | Arti |
|---|---|---|
| E1 `tcp6_present` | `/proc/net/tcp6` ada dan terbaca (`proc_root/net/tcp6`) | stack IPv6 aktif (termasuk saat `disable_ipv6=1` via sysctl) |
| E2 `if_inet6_present` | `/proc/net/if_inet6` ada (`proc_root/net/if_inet6`) | stack IPv6 aktif |
| E3 `sys_ipv6_present` | direktori `/proc/sys/net/ipv6` ada (`proc_root/sys/net/ipv6`; **procfs**, bukan sysfs `/sys`) | stack IPv6 dimuat (sysctl IPv6 terdaftar) |
| E4 `cmdline_ipv6_disable` | `/proc/cmdline` (`proc_root/cmdline`) memuat token **persis** `ipv6.disable=1` (dipisah spasi; tidak menerima `ipv6.disable=10`, `xipv6.disable=1`, `ipv6.disable=1x`) | IPv6 dimatikan saat boot (kernel tidak membuat E1–E3) |

#### 3.7.2a Bukti efektivitas `ipv6.disable=1` (v5, Q1)

Token E4 hanya membuktikan parameter **diberikan** ke kernel, bukan bahwa kernel
**menerapkannya**: modul bisa saja tidak membaca parameter itu, parameter
ditimpa kemunculan berikutnya, atau kernel lain sedang berjalan. Karena itu
`disabled_verified` mensyaratkan **semua** bukti berikut. Semuanya berasal dari
procfs atau perilaku syscall; **sysfs tidak dipakai**.

| # | Bukti | Syarat `disabled_verified` | Alasan |
|---|---|---|---|
| V1 | E4 token | `/proc/cmdline` memuat **tepat satu** kemunculan kunci `ipv6.disable=`, dan nilainya `1` (token persis). Kemunculan lain (`ipv6.disable=0` sebelum/sesudah, duplikat) → gagal | parameter berulang: nilai terakhir yang berlaku, jadi ambiguitas ditolak |
| V2 | E1, E2, E7, E6 | `/proc/net/tcp6`, `/proc/net/if_inet6`, `/proc/net/ipv6_route`, `/proc/net/snmp6` **tidak ada** (`FileNotFoundError`; galat lain → gagal) | stack IPv6 yang dimatikan saat boot tidak mendaftarkan entri procfs ini |
| V3 | E3 | `/proc/sys/net/ipv6` **tidak ada** | tabel sysctl IPv6 tidak didaftarkan |
| V4 | E8 probe | `socket(AF_INET6, SOCK_STREAM)` gagal dengan `errno == EAFNOSUPPORT` **persis** | bukti langsung bahwa keluarga alamat AF_INET6 tidak terdaftar di kernel. Sukses → IPv6 efektif → kontradiksi. Errno lain (`EMFILE`, `ENFILE`, `EPERM`/seccomp, `ENOMEM`) → **tidak terbukti** |
| V5 | E9 netns (v6: syarat lengkap N1–N5 di §3.7.2b; v7: + N6 dan prosedur KV) | target `/proc/self/ns/net` == target `/proc/1/ns/net` (string `net:[<inode>]` sama) **dan** helper terbukti berada di PID & user namespace awal (N1–N5) | probe V4 dan berkas V2 mencerminkan namespace jaringan proses helper; helper harus berada di netns host (sama dengan init). Berbeda / tidak terbaca → tidak terbukti |
| V6 | deklarasi | `ipv6_expected == "disabled"` | sengaja, bukan kebetulan |

Catatan:
- **(v6) Presisi V5:** `/proc/1/ns/net` menunjuk netns dari proses yang berstatus **PID 1 di PID namespace milik procfs yang dibaca helper**. Bila helper berjalan di container atau PID namespace lain, PID 1 itu adalah init container, bukan init host. Kesamaan `self` == `1` saja hanya membuktikan "helper satu netns dengan init yang terlihat olehnya". Karena itu V5 v6 mensyaratkan N1–N5 (§3.7.2b).
- `ipv6.disable=1` mematikan IPv6 kernel-wide, sehingga tidak ada namespace yang memiliki IPv6. V5 memastikan pengamatan dilakukan di namespace yang sama dengan unit terminal (unit helper tidak memakai `PrivateNetwork`).
- Probe V4 tidak membuka port, tidak bind, tidak listen, dan socket langsung ditutup. Helper sudah berjalan sebagai root lewat sudo; probe ini read-only terhadap keadaan sistem.
- **Sysfs (tidak diadopsi).** `/sys/module/ipv6/parameters/disable` akan menjadi bukti langsung nilai parameter modul, tetapi berada di sysfs. Perluasan cakupan ke sysfs **tidak** dilakukan di G2; bila diinginkan, itu perubahan cakupan yang wajib didokumentasikan dan direview terpisah (keputusan baru, bukan bagian D1–D7). Tes TCP6-20 tetap melarang akses `/sys`.
- **Kewajiban audit tcp6 tetap:** bila E1 ada, tcp6 **wajib** diaudit apa pun deklarasi dan bukti lain (baris 1 tabel 3.7.3 tidak berubah).

#### 3.7.2b Asumsi lingkungan helper dan bukti namespace (v6, R1)

**Asumsi lingkungan eksekusi helper (didokumentasikan; dibuktikan sejauh mungkin):**
- **A1.** Helper berjalan **langsung di host** (bukan di container / PID atau user namespace terpisah), dipanggil `sudo -n` oleh layanan API yang juga berjalan di host.
- **A2.** `proc_root` (`/proc`) adalah procfs yang di-mount untuk PID namespace helper itu sendiri, bukan bind-mount procfs dari namespace lain.
- **A3.** Unit terminal yang dirender helper tidak memakai `PrivateNetwork=`, `NetworkNamespacePath=`, `JoinsNamespaceOf=`, atau `RestrictAddressFamilies=` yang mengubah netns/keluarga alamat.
- **A4.** Kernel Linux ≥ 3.8 (nomor inode namespace awal tetap, `include/linux/proc_ns.h`).

**Bukti (semua procfs/`readlink`, tanpa sysfs):**

| # | Bukti | Syarat | Membuktikan |
|---|---|---|---|
| N1 | `readlink /proc/self/ns/pid` | sama persis dengan `pid:[4026531836]` (`PROC_PID_INIT_INO` = 0xEFFFFFFC) | helper di PID namespace **awal**, sehingga PID 1 yang terlihat adalah init host (A1) |
| N2 | `readlink /proc/self/ns/user` | sama persis dengan `user:[4026531837]` (`PROC_USER_INIT_INO` = 0xEFFFFFFD) | helper di user namespace awal (bukan container rootless) |
| N3 | `readlink /proc/self` | menghasilkan angka == `os.getpid()` helper | procfs yang dibaca milik PID namespace helper (A2) |
| N4 | `readlink /proc/self/ns/net` == `readlink /proc/1/ns/net` | sama persis, format `net:[<angka>]` | helper satu netns dengan init host (berkat N1) |
| N5 | tes statis/render template unit helper | tidak memuat direktif A3 | unit terminal berada di netns host (A3) |
| N6 (v7) | bukti KV terikat kernel: `ns_verified_osrelease` (kunci baru `provisioner.json`, root-owned, ditulis operator setelah KV-1…KV-5 lulus) == isi `/proc/sys/kernel/osrelease` saat audit | sama persis | konstanta N1/N2 sudah diverifikasi **untuk kernel yang sedang berjalan**; kernel baru → bukti lama tidak berlaku |

- Netns **tidak** punya nomor inode awal yang tetap, sehingga netns host dibuktikan **transitif**: N1 (PID ns awal) → PID 1 = init host → N4 (netns sama).
- Gagal salah satu N1–N4 atau **(v7)** N6, format `readlink` tidak sesuai, berkas tidak terbaca, atau nilai berbeda → `ipv6_state=unproven`, kode `ipv6_v5_ns_<n>`, readiness merah.
- **Konsekuensi ke audit-all secara umum.** Inventaris listener (`/proc/net/tcp`, `/proc/net/tcp6`) juga hanya bermakna di netns host, sehingga hasil N1–N4 dicatat di `audit-all` (`namespace_identity`) untuk **semua** `ipv6_state`.
  - N1–N4 atau **(v7)** N6 gagal → `inventory_errors += "namespace:tidak_terbukti"` → `audit_complete=false` → merah.
  - Ini memperketat (bukan melemahkan) aturan G1.
- **Keterbatasan:** konstanta inode awal adalah ABI kernel yang stabil sejak 3.8, tetapi perilakunya di kernel staging **NOT VERIFIED**. Tes memakai symlink palsu di `<tmp>/proc`.
- **(v7, T1)** Konstanta `4026531836`/`4026531837` **tidak** dianggap otomatis benar untuk setiap kernel. Nilai itu baru diterima setelah prosedur KV di bawah membuktikannya untuk kernel staging, dan bukti itu terikat ke `osrelease` (N6). N1–N6 semuanya wajib.

**Prosedur verifikasi inode namespace (KV, v7) — dilakukan operator di host staging, read-only:**

| # | Langkah | Bukti | Lulus bila |
|---|---|---|---|
| KV-1 | Pastikan shell operator berada di host, bukan container: `systemd-detect-virt --container` → `none`; `/proc/1/comm` → `systemd`; `/proc/1/cgroup` → `0::/init.scope` | keluaran ketiga perintah | ketiganya sesuai |
| KV-2 | Baca nilai **init host** dari shell root host: `readlink /proc/1/ns/pid`, `readlink /proc/1/ns/user`, `readlink /proc/1/ns/net`; dan `lsns -t pid -t user -t net -p 1` sebagai pembanding independen (util-linux) | keluaran mentah | `pid:[4026531836]`, `user:[4026531837]`; `lsns` menampilkan NS id yang sama |
| KV-3 | Jalankan helper lewat **jalur sudo yang sebenarnya**: sebagai user layanan API (bukan root; mis. `sudo -u <api_user> sudo -n <PROVISIONER_HELPER> selfcheck --nonce <N>`, atau melalui startup guard API sungguhan). Helper melaporkan `namespace_identity` mentah: hasil `readlink` `/proc/self/ns/{pid,user,net}`, `/proc/1/ns/net`, `/proc/self` vs `getpid()`, dan `osrelease` | JSON selfcheck (tanpa secret) | `pid`/`user` = nilai KV-2 = konstanta; `net` self == `net` PID 1 == `net` KV-2; N3 cocok |
| KV-4 | Pastikan jalur KV-3 sama dengan jalur runtime: argv `sudo` & path helper sama dengan `PROVISIONER_SUDO`/`PROVISIONER_HELPER`; `helper_sha256` = pin `PROVISIONER_HELPER_SHA256` (SG-e/SG-h) | log startup guard + JSON | sama |
| KV-5 | Catat `uname -r` dan `/proc/sys/kernel/osrelease`; setelah KV-1…KV-4 lulus, operator menulis `ns_verified_osrelease` ke `provisioner.json` (perubahan konfigurasi helper di staging = langkah deploy terpisah dengan persetujuan, di luar G2) | nilai osrelease + persetujuan | ditulis setelah persetujuan |

- Setiap ketidakcocokan KV-1…KV-4 → **tidak** menulis `ns_verified_osrelease`; helper tetap melaporkan `unproven`; readiness merah.
- **Tidak ada** konfigurasi untuk mengganti nilai inode yang diharapkan. Konstanta tetap di kode, dan N1/N2 tidak dapat dilonggarkan lewat config.
- Upgrade kernel (`osrelease` berubah) → N6 gagal → `unproven` sampai KV diulang.
- Laporan KV dilampirkan ke laporan staging (S6/S7).

#### 3.7.3 Keputusan (`ipv6_state`)

| # | Kondisi | `ipv6_state` | tcp6 | Galat inventaris |
|---|---|---|---|---|
| 1 | E1 ada | `enabled` | **wajib diaudit** penuh (header, baris; G1) | sesuai isi; bila `ipv6_expected=disabled` → tambah `ipv6_state:kontradiksi` |
| 2 | E1 tidak ada, `ipv6_expected=enabled` | `unproven` | — | `proc_net_tcp6:tidak_ada` (perilaku G1 tetap) |
| 3 | E1 tidak ada **dan** V1–V6 (§3.7.2a) semuanya terpenuhi (v5) | `disabled_verified` | tidak ada (diterima) | **tidak ada** |
| 4 | E1 tidak ada, `ipv6_expected=disabled`, tetapi salah satu V1–V5 gagal (v5: termasuk token tunggal saja, tanpa bukti efektivitas) | `unproven` | — | `ipv6_state:tidak_terbukti` + kode bukti gagal (`ipv6_v1_token`, `ipv6_v2_<berkas>`, `ipv6_v3_sysctl`, `ipv6_v4_af_inet6_ok` / `ipv6_v4_errno_<n>`, `ipv6_v5_netns`) (+ `proc_net_tcp6:tidak_ada`) |
| 5 | bukti mana pun tidak terbaca (izin ditolak, `cmdline` hilang, galat OS) | `unproven` | — | `ipv6_evidence_<sumber>:<jenis>` |

Catatan:
- **IPv6 dimatikan lewat sysctl** (`net.ipv6.conf.*.disable_ipv6=1`) **bukan** `disabled_verified`. Di keadaan itu tcp6 tetap ada (E1) dan **wajib** diaudit (baris 1), karena socket AF_INET6 tetap bisa dibuat dan listener dual-stack tetap muncul di tcp6.
- **(v4) E5 (sysctl `disable_ipv6`)** dicatat di `ipv6_evidence.sysctl_disable_all`/`sysctl_disable_default` (bool). Nilainya **tidak** mengubah `ipv6_state` (tetap `enabled` bila E1 ada; tcp6 wajib diaudit).
  - Bila E3 ada tetapi E5 **tidak terbaca** atau isinya bukan `0`/`1` → galat `ipv6_evidence_sysctl:<jenis>`, sehingga merah (bukti tidak lengkap).
  - Bila E3 tidak ada, E5 tidak dibaca.
- `audit-all` menambah field `ipv6_state` dan `ipv6_evidence` (`{"tcp6": bool, "if_inet6": bool, "sys_ipv6": bool, "cmdline_disable": bool, "expected": "..."}`). Field ini tanpa isi `cmdline` mentah, hanya boolean.
- `audit_complete` tetap `not inventory_errors`.

#### 3.7.4 Readiness (`rpc_policy.audit_inventory_problems`, diperluas)
Hijau hanya bila:
- syarat G1 terpenuhi; **dan**
- `ipv6_state ∈ {"enabled","disabled_verified"}`; **dan**
- bila `enabled`: `ipv6_evidence.tcp6 is True`; **dan**
- bila `disabled_verified`: `ipv6_evidence` = `{tcp6: false, if_inet6: false, sys_ipv6: false, cmdline_disable: true, expected: "disabled"}` **dan (v5)** `{snmp6: false, ipv6_route: false, af_inet6: "eafnosupport", cmdline_token_count: 1, netns_is_init: true}`. Field v5 yang hilang (helper lama) → merah.

`ipv6_state` hilang (helper lama), `unproven`, atau nilai lain → `audit_ipv6_unproven` (merah).

#### 3.7.5 Staging
- Status IPv6 **tidak diasumsikan** untuk host staging mana pun.
- Langkah S6/ST-3 (§9.3) mewajibkan `ipv6_state` dan `ipv6_evidence` dicatat di laporan bukti.
- Host tanpa IPv6 hanya lulus dengan `ipv6_expected=disabled` **dan** `ipv6.disable=1` di kernel cmdline (baris 3). Selain itu merah, dan dicatat sebagai temuan staging, bukan dilewati.

---

## 4. Daftar file G2

| # | File | Perubahan | Temuan |
|---|---|---|---|
| 1 | `backend/config.py` | `mt5_rpc_mode_default="ro"` (parse ketat); `mt5_production`; `mt5_rpc_enforcement` (default terturunkan); `mt5_provider` default `mt5ro`; **(v2)** `provisioner_helper_sha256` (env `PROVISIONER_HELPER_SHA256`, wajib di produksi); `ConfigError`; `validate_production()` | A-01…A-04, I-02 |
| 2 | `backend/migrations/postgres/009_rpc_migrations.sql`, `backend/migrations/sqlite/009_rpc_migrations.sql`, `backend/migrations/postgres/rollback/009_rpc_migrations.down.sql` | Tabel + indeks unik parsial + RLS/GRANT | §3.3 (**Keputusan D1**) |
| 3 | `backend/services/rpc_gate.py` (baru) | Gerbang tunggal §3.2 | D-01, G-01 |
| 4 | `backend/services/terminals.py` | lock registry; identitas runtime; gerbang di `runtime_for`; provider tanpa fallback; token hanya untuk provider nyata; `health_check` dengan `rpc_mode`/`policy`, tanpa probe classic di enforcement ≠ off, deep lewat lock; `provision_terminal` menolak reuse terminal `migrate:*` | D-01…D-05, C-05, B-04 |
| 5 | `backend/services/provisioner.py` | `TerminalSpec.rpc_mode="ro"`; `validate_spec` menolak classic untuk install/start bila enforcement ≠ off | B-01 |
| 6 | `backend/services/provisioning.py` | `spec_for` ketat + `expect_mode`; `_healthy` tanpa classic (≠ off) | B-03, D-04, H-03 |
| 7 | `backend/services/sync.py` | `capability`; lock → gerbang → scope; `_handle_broker_rejection` dilewati saat capability/migrasi; `sync()` legacy menolak (≠ off) | E-02, F-01, F-02, A-05 |
| 8 | `backend/services/scheduler.py` | `skipped_policy` tanpa backoff; `SyncScheduler` legacy menolak (≠ off) | E-01, A-05 |
| 9 | `backend/services/verification.py` | memakai `account_lock`; `evict` aman | D-03, G-03 |
| 10 | `backend/mt5client/mt5linux_provider.py` | penolakan defense-in-depth bila enforcement ≠ off | D-06 |
| 11 | `backend/api/routes.py` | `_migration_guard` di bind/unbind/connect; connect-aktif tanpa verify saat migrasi/classic; install MT5-nonaktif dari `spec_for`; `/mt5/status` (gerbang + lock non-blocking + `rpc_mode`); `/mt5/sync` memetakan `PolicyViolation`; health admin | B-02, B-05, C-01…C-06, G-01 |
| 12 | `backend/app.py`, `backend/run.py` | `startup_guard` (§3.6) sebelum `migrate()` dan sebelum pemulihan; scheduler hanya di-`start` setelah guard sukses; factory classic menolak; exit 78 | H-02, A-05, I-02 |
| 13 | `backend/.env.example` | `MT5_PROVIDER=mt5ro`, `MT5_RPC_MODE_DEFAULT=ro`, `MT5_PRODUCTION`, `MT5_RPC_ENFORCEMENT` (tanpa nilai secret) | A-01, A-04 |
| 14 | `backend/tests/test_readonly_static.py` | `SCAN_DIRS += ["provisioner"]`; ST-02/ST-03 | ST |
| 15 | `backend/tests/test_g2_*.py` (baru) | bagian 8 | semua |
| 16 | `docs/RPYC-SECURITY.md` | hapus rollback classic & "unbind → connect ulang" | A-07 (**Keputusan D5**) |
| 17 | `backend/provisioner/mt5_provision_helper.py` (**v2/v3**) | default `--rpc-mode` → `ro` di semua mode; `op_install(rpc_mode="ro")`; `selfcheck` menambah `helper_path`, `helper_sha256`, `euid`; bukti `selfcheck-v3` terikat `helper_sha256`; **(v3)** kunci config `ipv6_expected`, bukti E1–E4, `ipv6_state`/`ipv6_evidence` di `audit-all` (§3.7) | I-01, I-02, I-03 |
| 18 | `backend/security/rpc_policy.py` (**v2/v3**) | `SELFCHECK_DOMAIN` v3 + `selfcheck_proof(secret, nonce, helper_sha256)`; `verify_selfcheck_proof` dengan sha; **(v3)** `audit_inventory_problems` mensyaratkan `ipv6_state` terbukti (§3.7.4); parity diperbarui | I-02, I-03 |
| 19 | `backend/services/provisioner.py` (**v2**, tambahan baris 5) | `selfcheck()` mengembalikan field identitas; `MockProvisioner.selfcheck` melaporkan `production=false` dan identitas palsu yang **selalu** gagal SG-h di produksi | I-02 |
| 20 | Tes lama yang bergantung default classic helper (**v2**) | dibuat eksplisit `rpc_mode="classic"`/`--rpc-mode classic`, atau dipindah ke konfigurasi ro-capable; tanpa skip/hapus | I-01 |
| 21 | `backend/tests/conftest.py` atau fixture PG (**v2**) | guard fixture: menolak DSN yang bukan cluster `pgserver` sementara milik tes | K-009 |

**Tidak diubah di G2:**
- `security/rpc_policy.py` dan helper, **kecuali** perubahan tertulis di baris 17–18 (v2);
- `.github/workflows/*`, `backend/deploy/*`;
- `lib/` (Flutter), termasuk `engine.dart` dan `montecarlo.dart` yang terkunci;
- tabel `mt5_terminals` dan `mt5_connect_jobs`;
- `mt5client/ro_service.py`.

---

## 5. Urutan implementasi (berhenti untuk review di setiap titik ■)

0. **G2.0 Default helper ro** (file 17 bagian default mode, file 20) + tes CLI-01…07.
   - Dilakukan pertama agar seluruh suite sudah berjalan di atas default `ro` sebelum wiring runtime.
1. **G2.1 Konfigurasi** (file 1, 13) + tes CFG.
   - Belum ada perilaku runtime yang berubah, selain parsing ketat.
2. **G2.2 Skema** (file 2, 21) + tes migrasi MIG-01…MIG-16 di SQLite dan PostgreSQL ephemeral lokal (`pgserver`) dengan role sebenarnya (§3.3.2). Tidak pernah DB produksi/staging.
   - ■ **Review 1** (config + skema).
3. **G2.3 Gerbang + registry** (file 3, 4, 9, 10) + tes RT/CC/HC.
4. **G2.4 Provisioner/provisioning** (file 5, 6) + tes PV, HC-05/06.
5. **G2.5 Sync + scheduler** (file 7, 8) + tes SY/SC/CC-03.
   - ■ **Review 2** (runtime).
6. **G2.6 Routes** (file 11) + tes API.
7. **G2.7 Startup guard** (file 12, 17 bagian selfcheck, 18, 19) + tes SG-01…SG-16 dan parity selfcheck v3.
8. **G2.8 Statis + dokumen** (file 14, 16) + RC regresi pemulihan.
   - ■ **Review 3** (API + startup + regresi penuh).

Setiap langkah:
- satu run penuh backend suite (JUnit XML);
- angka collected/passed/failed/errors/skipped;
- tanpa commit sampai disetujui.

---

## 6. Transisi status

### 6.1 Status kegunaan terminal (turunan, tidak disimpan)

| Input (terminal, migrasi, enforcement) | Hasil gerbang | Sync / status / verify | Bind / reactivate | Unbind | Connect (akun aktif) |
|---|---|---|---|---|---|
| ro, op_owner NULL, tanpa migrasi | `ok` | jalan | jalan | jalan | verify live |
| ro, `job:X` | `ok` (bukan wewenang policy) | verify job jalan; sync jalan | job lain ditolak (`_acquire_terminal`) | 409 `pending` (sudah ada) | verify live |
| ro/classic, `remove:*` / `removal-pending` | `ok` dari policy; baris dipegang penghapusan | jalan bila akun active (jarang) | ditolak (sudah ada) | — | verify live |
| classic, enforcement `off` | `ok` | jalan (dev/test saja) | jalan | jalan | verify live |
| classic, `migration`/`strict` | `classic_blocked` | **skip** (`skipped_policy`) | 409 `terminal_needs_migration` | jalan (Keputusan D3) | login tanpa verify, `terminal_needs_migration` |
| `op_owner=migrate:*` / migrasi non-final, tanpa capability | `migration_in_progress` | **skip** | 409 | **409** (kredensial utuh) | login tanpa verify, `migration_in_progress` |
| migrasi committed/synced + capability cocok | `ok` | hanya sync verifikasi worker G3 | 409 | 409 | login tanpa verify |
| `migrate:*` tanpa baris migrasi | `migration_owner_orphan` | skip | 409 | 409 | login tanpa verify |
| mode tidak dikenal / terminal hilang / gate DB gagal | `rpc_mode_invalid` / `terminal_missing` / `gate_unavailable` | skip | 503 | 503 | 503 |

### 6.2 Pemegang `op_owner` (setelah G2)

```
NULL ──job create (_acquire_terminal)──► job:<id> ──connected/fail──► NULL
NULL ──remove claim────────────────────► remove:<uuid> ──ok──► (baris dihapus)
                                                      └─gagal──► removal-pending ──retry──► remove:<uuid>
NULL ──CLAIM migrasi (G3)──────────────► migrate:<id>:<gen> ──takeover (G3)──► migrate:<id>:<gen+1>
                                                      └─DONE/CANCELLED/FAILED_CREDENTIALS (G3)──► NULL
```

- Setiap transisi `migrate:*` hanya dilakukan G3.
- G2 menjamin tidak ada jalur lain yang mengklaim, melepas, atau menghapus baris `migrate:*` (RC-01…RC-05).

### 6.3 Siklus hidup kredensial (`mt5_credentials`)

| Jalur | Menulis | Menghapus | Setelah G2 |
|---|---|---|---|
| bind / reactivate / self_link | `_store_credentials` / `_insert_account` (akun `pending`) | `_cleanup` job (hanya akun `pending`) | diblokir di awal handler bila migrasi/classic (≠ off) |
| unbind | — | DELETE | 409 bila migrasi aktif, sebelum DELETE |
| connect akun aktif → broker menolak | — | DELETE + `credentials_rejected` | tidak ada verify (dan tidak ada DELETE) saat migrasi/classic (≠ off) |
| sync → galat auth broker | — | DELETE + `credentials_rejected` | dilewati bila capability atau migrasi aktif |
| worker migrasi G3 | — | **tidak pernah** | FAILED_CREDENTIALS mengikuti koreksi #5 (worker G3) |

### 6.4 State migrasi (dibaca G2, ditulis G3)

```
claimed → snapshot_before → backed_up → stopping → stopped → removing → removed
→ pid_recorded → installed → started → probed → verified → committed → synced → done
gagal: cancelled (≤ stopped) · quarantined · blocked · failed_credentials
```

- G2 hanya membedakan **final** (`done`, `cancelled`, `failed_credentials`) dan **non-final**.
- Capability hanya sah di `committed`/`synced` (G1).

---

## 7. Risiko

| # | Risiko | Dampak | Mitigasi |
|---|---|---|---|
| R1 | Mengaktifkan `migration`/`strict` sebelum G3 tersedia | **Semua terminal classic berhenti sync** (scheduler `skipped_policy`); reactivate ditolak 409 | G2 boleh di-merge, tetapi **tidak boleh dideploy ke produksi dengan `strict`** sebelum G3 siap dan terminal sudah dimigrasi. Staging memakai `migration` **hanya setelah gerbang bukti §9.2 lulus** (**Keputusan D2, v2**) |
| R2 | Default `ro` (API dan helper) mematahkan tes lama | Tes yang membuat terminal lewat `provision_terminal` + builder mock, health check, dan ±31 `op_install(` / ≤34 argv `install` helper tanpa mode | D-05 (token lazy); aturan pembaruan I-01 (eksplisit classic atau konfigurasi ro-capable; tanpa skip/hapus). Jumlah tes gagal **NOT MEASURED** |
| R10 | Helper tanpa `--rpc-mode` di staging yang belum ro-capable | install gagal (exit 4) | Disengaja (fail-closed, tanpa classic diam-diam). Staging wajib ro-capable sebelum G2 dideploy (§9.2 langkah 1) |
| R11 | Format bukti selfcheck v3 tidak kompatibel dengan helper G1 yang terpasang | startup produksi gagal sampai API dan helper diperbarui bersama | Deploy helper dan API dalam satu rilis; `PROVISIONER_HELPER_SHA256` di-pin bersamaan. Gagal → exit 78 (bukan jalan dengan helper tak terverifikasi) |
| R12 | Guard provider "tanpa lock" (§3.5.5) memicu `RuntimeError` di jalur yang terlewat audit | 500 di jalur itu | Itu tujuannya (fail-closed). Ditangkap tes CC-12 yang memanggil setiap route/worker dengan provider berguard |
| R3 | Gerbang menambah SELECT per `runtime_for` | Latensi kecil (≤4 akun, setiap 300 s) | Satu query berindeks via `system_conn`; diukur di tes kapasitas |
| R4 | Lock registry tidak pernah dibuang (v3: termasuk saat akun dihapus) | Pertumbuhan memori kecil (≤ jumlah `account_id` berbeda selama umur proses) | Tidak ada penghapusan lock (K1). **(v4)** Lock hanya untuk akun yang terbukti ada dan dimiliki; batas keras `ACCOUNT_LOCKS_MAX`, fail-closed saat penuh (§3.5.7). Dipantau lewat `account_locks_count`; dilepas hanya saat restart |
| R13 (v4) | `ACCOUNT_LOCKS_MAX` tercapai karena churn akun (banyak self-link gagal) | Akun baru tidak bisa disync/diverifikasi sampai restart | Batas dihitung dari kapasitas portal; peringatan di 80%; rate limit/lockout self-link membatasi churn; restart terencana adalah pemulihan |
| R5 | Startup guard memblokir boot produksi | Downtime bila secret atau konfigurasi tidak cocok | Itu tujuannya (fail-closed). Pesan galat spesifik tanpa nilai secret; prosedur di runbook |
| R6 | Migrasi 009 di PostgreSQL produksi | Perubahan skema | Hanya menambah tabel; rollback menolak bila ada baris non-final; dijalankan oleh migrator (B12.4), bukan role aplikasi |
| R7 | Login saat migrasi tanpa verify live | Customer mendapat token tanpa bukti broker | Sama seperti S6 (`terminal_pending`) yang sudah disetujui. Token hanya untuk membaca jurnal tersimpan; tidak ada mutasi |
| R8 | Heuristik `BROKER_AUTH_MARKERS` tetap menghapus kredensial di luar migrasi | Penghapusan karena galat salah klasifikasi | Perilaku produk yang sudah ada (Tahap 2). Tidak diubah G2; dicatat untuk review terpisah |
| R9 | TOCTOU antara gerbang dan helper | Helper dipanggil setelah gerbang lolos tetapi baris sudah diklaim migrasi | Helper menolak lewat fence (G1) hanya bila fence pernah di-advance. Job memakai `op_owner job:` atomik; G3 wajib `fence-advance` sebelum langkah apa pun |

---

## 8. Test matrix

Semua di bawah ini **rencana**. Tidak ada yang dijalankan di audit ini.

| ID | Skenario | Lulus bila |
|---|---|---|
| CFG-01 | Config tanpa env | `mt5_rpc_mode_default == "ro"`, `mt5_provider == "mt5ro"` |
| CFG-02 | `MT5_RPC_MODE_DEFAULT=classic` non-produksi | diterima (`classic`) |
| CFG-03 | nilai `RO `, `r0`, `readonly`, `""` | ValueError/ConfigError; tidak pernah `classic` |
| CFG-04 | produksi + enforcement ≠ strict | `validate_production` memuat `enforcement_not_strict` |
| CFG-05 | produksi + default classic | masalah `rpc_mode_default_not_ro` |
| CFG-06 | produksi + `MT5_PROVIDER=mt5linux`/`json` | masalah `legacy_provider` |
| CFG-07 | produksi + investor readonly off | masalah |
| CFG-08 | turunan enforcement: mock→off, real→migration, prod→strict | sesuai |
| MIG-01 | 009 diterapkan migrator non-superuser (SQLite & PG ephemeral); dijalankan ulang | tabel & indeks ada; run ulang tanpa DDL/galat |
| MIG-02 | dua migrasi non-final terminal sama | UNIQUE violation |
| MIG-03 | down 009 dengan baris non-final | ditolak; tabel, indeks, policy utuh |
| MIG-04 | RLS: login `transcendence_app` scope customer A | baris A saja; baris B 0 |
| MIG-05 | login `transcendence_app` INSERT/UPDATE/DELETE | ditolak (privilege), termasuk dengan scope cocok |
| MIG-06 (v2) | login `transcendence_app` tanpa scope | 0 baris |
| MIG-07 (v2) | login `transcendence_system` dengan `app.scope='system'` | semua baris; INSERT/UPDATE berhasil |
| MIG-08 (v2) | login `transcendence_system` DELETE | ditolak |
| MIG-09 (v2) | login `transcendence_system` tanpa `app.scope` | 0 baris (pola 008) |
| MIG-10 (v2) | role yang diuji | `rolsuper=false`, `rolbypassrls=false`; tabel `relrowsecurity` & `relforcerowsecurity` true; owner = migrator; `has_table_privilege(PUBLIC, ...)` false |
| MIG-11 (v2) | indeks parsial: 1 non-final + N final terminal sama | diterima |
| MIG-12 (v2) | indeks parsial: non-final → final, lalu non-final baru | diterima |
| MIG-13 (v2) | indeks parsial: dua final terminal sama | diterima; dua non-final terminal **berbeda** diterima |
| MIG-14 (v2) | CHECK state: nilai di luar `MIGRATION_STATES` | ditolak (SQLite & PG); daftar CHECK == `rpc_policy.MIGRATION_STATES` (parity) |
| MIG-15 (v2) | down 009 tanpa baris non-final, lalu 009 lagi | down menghapus tabel/indeks/policy/grant dalam satu transaksi; terapkan ulang berhasil; `schema_migrations` konsisten dengan pola down 008 |
| MIG-16 (v2) | fixture menerima DSN di luar cluster tes (mis. env `DATABASE_URL` diset) | fixture **menolak** dan tes gagal keras (tidak pernah tersambung) |
| RT-01 | ro, bebas | provider `MT5ReadOnlyServiceProvider` |
| RT-02 | classic + `migration`/`strict` | `PolicyViolation(classic_blocked)`; provider **tidak** dibuat; kredensial **tidak** didekripsi |
| RT-03 | classic + `off` | `MT5LinuxProvider` |
| RT-04 | `op_owner=migrate:*` + migrasi non-final | `migration_in_progress` |
| RT-05 | `migrate:*` tanpa baris | `migration_owner_orphan` |
| RT-06 | rpc_mode di luar CHECK (simulasi) / gate DB gagal | `rpc_mode_invalid` / `gate_unavailable` |
| RT-07 | cache hit lalu `provision_id`/`rpc_mode` berubah | provider dibuat ulang, provider lama diputus |
| RT-08 | cache hit lalu CLAIM migrasi | kebijakan dievaluasi ulang → diblokir |
| RT-09 | `evict` saat lock dipegang | lock yang sama setelah evict; `account_lock` identik |
| RT-10 | factory global enforcement ≠ off | menolak |
| RT-11 | `SyncService.sync()` legacy enforcement ≠ off | menolak |
| RT-12 | rpc_mode ro + secret kosong + builder mock | builder dipanggil tanpa galat token |
| RT-13 | `MT5LinuxProvider` dibuat dengan enforcement ≠ off | menolak |
| CC-01 | dua `sync_account` paralel akun sama, salah satu setelah `evict` | tepat satu berjalan, lainnya `SyncLockBusy` |
| CC-02 | verifikasi timeout (thread hidup) + scheduler | sync scheduler menunggu/skip; tidak paralel |
| CC-03 | CLAIM migrasi di antara lookup dan lock (sync menunggu lock) | sync setelah lock diblokir; tidak ada tulisan jurnal |
| CC-04 (v2) | `/mt5/status` saat sync memegang L2 (provider palsu memblokir di `history_deals_get`) | status `sync_in_progress`; provider sync **tidak** menerima `disconnect`; sync selesai normal |
| CC-05 (v2) | `evict` saat L2 dipegang | `account_lock` identik (objek sama); provider tidak di-`disconnect` sampai pemegang keluar; `disconnect` tepat 1× saat keluar |
| CC-06 (v2) | `evict` saat L2 bebas | `disconnect` tepat 1× segera; runtime baru memakai lock yang sama |
| CC-07 (v2) | verifikasi timeout (thread masih di `connect`), lalu scheduler + `/mt5/status` | keduanya `busy`/`sync_in_progress` sampai thread selesai; lock dilepas oleh thread verifikasi; tidak ada pemakaian paralel |
| CC-08 (v2) | identitas berubah (rpc_mode/provision_id) saat L2 dipegang | pemegang menyelesaikan dengan provider lama; berikutnya provider baru; lock sama |
| CC-09 (v2) | stres: 8 thread × 50 iterasi (sync, status, health deep, verify, evict) satu akun; provider palsu mencatat konkurensi | konkurensi maksimum provider = 1; pemakaian setelah disconnect = 0; tidak ada deadlock (timeout tes 30 s) |
| CC-10 (v2) | dua akun berbeda paralel | berjalan bersamaan (lock per akun, bukan global) |
| CC-11 (v2) | health non-deep | tidak pernah memanggil `runtime_for`/provider runtime; L2 tidak diambil |
| CC-12 (v2) | provider berguard dipakai di luar `session()` (setiap route/worker dipanggil dalam tes) | tidak ada `provider_used_without_lock`; pemakaian langsung sengaja → `RuntimeError` |
| CC-13 (v2) | pengecualian di dalam `session()` (provider melempar) | L2 dilepas, scope RLS dibersihkan, `disconnect` dipanggil |
| CC-14 (v3) | 16 thread memanggil `account_lock(A)` pertama kali bersamaan (barrier) | semua menerima objek identik (`is`); `len(_account_locks)` bertambah tepat 1 |
| CC-15 (v3) | race tiga arah, 200 iterasi: thread-1 `session(A)` (memegang lock), thread-2 `evict(A)` berulang, thread-3 `DELETE` baris akun A (lalu customer, CASCADE) | `account_lock(A)` sebelum/selama/sesudah semuanya identik (`id()` sama); `_account_locks` tidak pernah mengecil; tidak ada `disconnect` pada provider yang sedang dipakai; tidak ada deadlock (timeout 30 s) |
| CC-16 (v3) | setelah akun A dihapus: `session(A)` baru | `PolicyViolation` (`terminal_missing`/akun tidak ada); provider tidak dibuat; kredensial tidak didekripsi; lock objek sama, dilepas setelah penolakan |
| CC-17 (v3) | hapus akun saat sync A memegang lock (provider palsu memblokir) | sync selesai atau gagal sebagai galat sync biasa (FK/RLS); lock dilepas; tidak ada baris yatim; `evict` selama itu tidak memutus provider |
| CC-18 (v3) | SQLite: akun A dihapus, akun baru mendapat ID sama | `account_lock` mengembalikan objek yang sama; tidak ada state akun lama terbawa (runtime baru, identitas baru) |
| CC-19 (v3) | restart (registry baru) | dict lock kosong; satu-satunya jalur pembersihan lock |
| CC-20 (v3) | tes statis: tidak ada `del`/`pop`/`clear`/penugasan ulang pada `_account_locks` di kode non-tes selain inisialisasi | ya |
| LK-01 (v4) | `session()` / `ensure_account_lock` dengan `account_id` yang tidak ada di DB | `PolicyViolation(account_missing)`; `len(_account_locks)` tidak berubah; tidak ada dekripsi kredensial / provider |
| LK-02 (v4) | 10 000 `account_id` acak yang tidak ada (termasuk negatif, 0, 2^63, bool, str) | dict tidak bertambah; ID tidak sah ditolak tanpa query |
| LK-03 (v4) | jalur bertoken customer A meminta akun milik customer B (simulasi pemanggil langsung) | `account_owner_mismatch`; lock tidak dibuat |
| LK-04 (v4) | `ACCOUNT_LOCKS_MAX` tercapai | akun baru → `lock_registry_full` (API 503 / scheduler skip); akun yang sudah punya lock tetap jalan; tidak ada lock yang dihapus; peringatan 80% tercatat |
| LK-05 (v4) | akun ada → lock dibuat; akun dihapus; `ensure_account_lock` lagi | lock **lama** dikembalikan (tanpa DB, K1); sesi ditolak gerbang |
| LK-06 (v4) | race: akun dihapus antara cek keberadaan dan `setdefault` | paling banyak satu lock untuk ID itu; sesi berikutnya ditolak gerbang; tidak ada pengecualian tak tertangani |
| LK-07 (v4) | tes statis: setiap pemanggilan `session(`/`ensure_account_lock(`/`runtime_for(` di `api/` memakai `account_id` dari baris DB (`_own_account`, `_find_account` pasca-verifikasi, baris job/claim), bukan dari `request.*` | ya (AST: argumen tidak berasal dari `request.get_json`/`request.args`/`view_args`) |
| LK-08 (v4) | route customer dengan token sah tetapi akun milik customer lain (manipulasi body/URL) | tidak mungkin memilih akun lain (ID dari `_own_account`); 404/409 perilaku lama; registry tidak berubah |
| LK-09 (v4) | lock tetap selama proses hidup setelah LK-01…08 | semua lock yang pernah dibuat masih ada (`is` identik); CC-19 restart tetap satu-satunya pembersihan |
| HC-01 | health ro | `rpc_mode=ro`, probe ro |
| HC-02 | health classic + enforcement ≠ off | tidak ada handshake classic; `policy=classic_blocked` |
| HC-03 | health deep, lock sibuk | `sync_in_progress`, tidak connect |
| HC-04 | health deep, migrasi aktif | tidak connect |
| HC-05 | `_healthy` classic + enforcement ≠ off | False → job timeout → gagal |
| HC-06 | port ro menjawab handshake tanpa token (server classic menyamar) | probe gagal (sehat = False) |
| HC-07 | terminal tanpa `provision_id` di produksi | `policy=provision_id_missing` |
| PV-01 | `TerminalSpec()` default | `rpc_mode == "ro"` |
| PV-02 | install/start classic + enforcement ≠ off | `PolicyError` sebelum spawn helper |
| PV-03 | `spec_for` rpc_mode NULL/korup | job gagal `rpc_mode_invalid`, helper tidak dipanggil |
| PV-04 | `spec_for` → argv start memuat `--expect-mode <mode DB>` | ya |
| SY-01 | galat auth broker saat sync biasa | perilaku lama (credentials_rejected) tetap |
| SY-02 | galat auth broker + capability | **tidak** ada DELETE/UPDATE; `summary.auth_rejected=True` |
| SY-03 | galat auth broker + migrasi diklaim di tengah sync | tidak ada DELETE |
| SY-04 | capability cocok (committed/synced) | sync jalan; capability basi → ditolak |
| SC-01 | scheduler: 1 classic + 1 ro (enforcement migration) | ro disync, classic `skipped_policy` |
| SC-02 | `skipped_policy` | tidak menaikkan `consecutive_failures` / backoff |
| SC-03 | migrasi aktif | skip, kredensial utuh |
| SC-04 | gate DB gagal | skip seluruh siklus untuk akun itu (fail-closed), log |
| SC-05 | sync awal `_on_connected` pada terminal migrasi | diblokir |
| API-01 | bind saat migrasi | 409, akun/kredensial/terminal tidak berubah |
| API-02 | reactivate (connect unbound) saat migrasi | 409, tidak ada `_claim_pending` |
| API-03 | self_link customer baru, tanpa migrasi | normal (ro) |
| API-04 | reactivate pada terminal classic, enforcement ≠ off | 409 `terminal_needs_migration` |
| API-05 | bind MT5 nonaktif | helper install menerima `--rpc-mode <DB>`; install gagal → `failed`, bukan `stopped` |
| API-06 | unbind saat migrasi | 409; kredensial & state utuh; sesi tidak dicabut |
| API-07 | connect akun aktif saat migrasi | 200 token, `migration_in_progress`, verify **tidak** dipanggil, kredensial utuh |
| API-08 | connect akun aktif, password salah saat migrasi | 401 (perilaku lama); tidak ada DELETE |
| API-09 | `/mt5/status` saat sync berjalan | `sync_in_progress`, provider tidak diputus |
| API-10 | `/mt5/status` classic + enforcement ≠ off | `connected=false`, `reason=classic_blocked`, tanpa koneksi |
| API-11 | invarian `_store_credentials` | hanya untuk akun pending dari unbound/credentials_rejected |
| API-12 | `/mt5/status` melaporkan `rpc_mode`, bukan `MT5_PROVIDER` | ya |
| API-13 | `/mt5/sync` diblokir kebijakan | 409 + reason |
| API-14 | gate tidak tersedia (tabel di-drop di tes) | bind/unbind/connect 503, tanpa mutasi |
| SG-01 | produksi lengkap + selfcheck valid | start |
| SG-02 | produksi + secret API ≠ helper (proof salah) | exit 78, HTTP tidak dilayani, scheduler tidak start |
| SG-03 | produksi + helper `production=false` | exit 78 |
| SG-04 | produksi + `production_problems` tidak kosong | exit 78 |
| SG-05 | produksi + `validate_production` bermasalah | exit 78, sebelum `migrate()` |
| SG-06 | non-produksi bermasalah | peringatan, start |
| SG-07 (v2) | guard gagal (setiap SG-02…05, 08…16) | `run.main` exit 78; spy membuktikan `migrate()`, pemulihan, `sched.start()`, `serve()` **tidak** dipanggil; tidak ada thread `mt5-sync-dispatch`/`provision-*`/`initial-sync-*`; tidak ada socket listen |
| SG-08 (v2) | `PROVISIONER_HELPER` symlink | gagal SG-b |
| SG-09 (v2) | helper atau direktori induk writable group/other, atau bukan milik root | gagal SG-c |
| SG-10 (v2) | `api_helper_sha256 != PROVISIONER_HELPER_SHA256` (produksi) | gagal SG-e |
| SG-11 (v2) | helper melapor `helper_path` berbeda (helper lain dipanggil) | gagal SG-h |
| SG-12 (v2) | helper melapor `helper_sha256` berbeda dari hash API | gagal SG-h dan SG-i |
| SG-13 (v2) | bukti v2 lama / bukti dengan nonce lain / bukti dengan sha lain | gagal SG-i |
| SG-14 (v2) | `euid != 0` | gagal SG-h |
| SG-15 (v2) | sudo meminta password / timeout / exit ≠ 0 | gagal SG-f, tanpa hang (timeout `provisioner_timeout_seconds`) |
| SG-17 (v3) | startup dengan migrasi 009 tertunda di bawah role aplikasi non-owner | `MigrationPrivilegeError` → exit 78; tidak ada DDL dijalankan; tidak ada upaya memakai DSN lain; tidak ada env/flag yang memicu DDL (tes statis) |
| SG-16 (v2) | selfcheck dipanggil lewat objek provisioner yang sama dengan runtime | assert identitas objek (`app.extensions["mt5_provisioner"] is guard_provisioner`) dan argv sama (`sudo -n <helper>`) |
| SC-PAR (v2) | parity `selfcheck_proof` v3 helper ↔ rpc_policy | vektor identik |
| TCP6-01 (v3) | **IPv6 aktif:** tcp6 + if_inet6 + sys/net/ipv6 ada, isi valid | `ipv6_state=enabled`; tcp6 diaudit; `audit_complete=true`; readiness hijau |
| TCP6-02 (v3) | IPv6 aktif, tcp6 berisi baris rusak / header rusak | `enabled`; galat G1 (`baris_rusak`/`header_rusak`); merah |
| TCP6-03 (v3) | IPv6 aktif, listener dual-stack `::` (alamat 32 nol) di jendela port | terdeteksi di `listeners` (tidak terlewat) |
| TCP6-04 (v3) | sysctl `disable_ipv6=1` (tcp6 tetap ada), `ipv6_expected=enabled` | `enabled`; tcp6 **tetap** diaudit |
| TCP6-05 (v3) | **IPv6 nonaktif terverifikasi:** `ipv6_expected=disabled`, cmdline `... ipv6.disable=1 ...`, tanpa tcp6/if_inet6/sys/net/ipv6 | `ipv6_state=disabled_verified`; tidak ada galat tcp6; `audit_complete=true`; readiness hijau |
| TCP6-06 (v3) | `ipv6_expected=disabled`, cmdline tanpa token | `unproven`; `ipv6_state:tidak_terbukti`; merah |
| TCP6-07 (v3) | `ipv6_expected=disabled`, token salah bentuk (`ipv6.disable=10`, `xipv6.disable=1`, `ipv6.disable=1x`, `ipv6.disable=0`) | semua `unproven`; merah |
| TCP6-08 (v3) | `ipv6_expected=disabled`, cmdline benar, tetapi `if_inet6` atau `sys/net/ipv6` ada | `unproven`; merah |
| TCP6-09 (v3) | `ipv6_expected=disabled`, tetapi tcp6 ada | `enabled` + `ipv6_state:kontradiksi`; tcp6 tetap diaudit; merah |
| TCP6-10 (v3) | **status tidak terbukti:** `ipv6_expected=enabled` (atau kunci hilang), tcp6 tidak ada | `unproven`; `proc_net_tcp6:tidak_ada`; merah |
| TCP6-11 (v3) | `cmdline` tidak terbaca (PermissionError simulasi) / hilang | `unproven`; `ipv6_evidence_cmdline:*`; merah |
| TCP6-12 (v3) | `ipv6_expected` nilai tak sah (`"off"`, `""`, `true`) | config ditolak (exit 4) |
| TCP6-13 (v3) | readiness: output audit tanpa `ipv6_state` (helper lama) / nilai lain / bukti tidak konsisten dengan state | `audit_ipv6_unproven`; merah |
| TCP6-14 (v3) | parity: aturan keputusan §3.7.3 helper ↔ `rpc_policy` (vektor bersama) | identik |
| TCP6-15 (v4) | **IPv6 aktif:** E1 `/proc/net/tcp6` valid, E2 `/proc/net/if_inet6` ada, E3 `/proc/sys/net/ipv6` ada, E5 `disable_ipv6=0` (all & default), `ipv6_expected=enabled` | `enabled`; tcp6 diaudit; `sysctl_disable_all=false`; readiness hijau |
| TCP6-16 (v4) | **sysctl disabled:** E1–E3 ada, E5 `conf/all/disable_ipv6=1` dan `conf/default/disable_ipv6=1`, listener dual-stack di tcp6 | `enabled` (bukan `disabled_verified`); `sysctl_disable_all=true`; tcp6 **tetap** diaudit dan listener terdeteksi; dengan `ipv6_expected=disabled` → `ipv6_state:kontradiksi`, merah |
| TCP6-17 (v4) | **boot disabled:** `/proc/cmdline` berisi `ipv6.disable=1`, E1/E2/E3 **tidak ada**, `ipv6_expected=disabled` | `disabled_verified`; E5 tidak dibaca; tidak ada galat; readiness hijau |
| TCP6-18 (v4) | **bukti tidak lengkap** (parametrize): (a) E1 tidak ada, E3 ada; (b) E1 tidak ada, E2 ada; (c) E4 benar tetapi E3 ada; (d) E3 ada, E5 tidak terbaca; (e) E5 isi `2`/kosong; (f) `/proc/cmdline` tidak terbaca; (g) deklarasi `disabled` tanpa E4 | semua `unproven` atau galat bukti → `audit_complete=false`; readiness merah |
| TCP6-19 (v4) | sysfs palsu: `<tmp>/sys/module/ipv6`, `<tmp>/sys/class/net/*` dibuat/dihapus, procfs tidak berubah | keputusan identik dengan tanpa sysfs palsu (sysfs tidak dibaca) |
| TCP6-20 (v4) | tes statis: helper tidak membuka path di bawah `/sys` (sysfs) dan semua bukti IPv6 dibangun dari `proc_root` | ya |
| TCP6-21 (v5) | **token saja:** cmdline `ipv6.disable=1`, E1/E2/E3/E6/E7 tidak ada, tetapi probe AF_INET6 **berhasil** | `unproven` (`ipv6_v4_af_inet6_ok`); merah |
| TCP6-22 (v5) | **boot disabled terbukti:** V1–V6 terpenuhi, probe → `EAFNOSUPPORT` | `disabled_verified`; hijau |
| TCP6-23 (v5) | probe gagal dengan errno lain (`EPERM`, `EMFILE`, `ENOMEM`) atau melempar pengecualian tak terduga | `unproven` (`ipv6_v4_errno_<n>`); merah |
| TCP6-24 (v5) | cmdline `ipv6.disable=1 ipv6.disable=0`, `ipv6.disable=0 ipv6.disable=1`, `ipv6.disable=1 ipv6.disable=1` | `unproven` (`ipv6_v1_token`); merah |
| TCP6-25 (v5) | V1–V4 terpenuhi, tetapi `/proc/net/snmp6` **atau** `/proc/net/ipv6_route` ada | `unproven`; merah |
| TCP6-26 (v5) | V1–V4 terpenuhi, tetapi netns helper ≠ netns init, atau symlink ns tidak terbaca | `unproven` (`ipv6_v5_netns`); merah |
| TCP6-27 (v5) | E6/E7 tidak terbaca karena izin (bukan `FileNotFoundError`) | `unproven`; merah |
| TCP6-28 (v5) | tcp6 ada + deklarasi `disabled` + token benar + probe `EAFNOSUPPORT` (simulasi tidak konsisten) | tcp6 **tetap diaudit**; `ipv6_state:kontradiksi`; merah |
| TCP6-29 (v5) | readiness: output `disabled_verified` tanpa field v5 / `af_inet6 != "eafnosupport"` / `cmdline_token_count != 1` / `netns_is_init != true` | `audit_ipv6_unproven`; merah |
| TCP6-30 (v5) | probe tidak pernah bind/listen/connect (spy pada objek socket) dan socket ditutup | ya |
| TCP6-31 (v6) | N1–N5 terpenuhi (host) | `namespace_identity` ok; V5 lulus |
| TCP6-32 (v6) | **container:** `/proc/self/ns/net` == `/proc/1/ns/net`, tetapi `/proc/self/ns/pid` ≠ `pid:[4026531836]` | `unproven` (`ipv6_v5_ns_1`) dan `namespace:tidak_terbukti`; merah, walaupun V1–V4 lulus |
| TCP6-33 (v6) | user namespace non-awal (rootless) | `unproven` (`ipv6_v5_ns_2`); merah |
| TCP6-34 (v6) | `/proc/self` tidak ada / menunjuk PID lain (procfs namespace lain) | `unproven` (`ipv6_v5_ns_3`); merah |
| TCP6-35 (v6) | format `readlink` tidak sesuai (`net:[abc]`, kosong, tanpa kurung) / `readlink` gagal | `unproven`; merah |
| TCP6-36 (v6) | `ipv6_state=enabled` tetapi N1–N4 gagal | `audit_complete=false` (`namespace:tidak_terbukti`); merah |
| TCP6-37 (v6) | N5: template unit helper tidak memuat `PrivateNetwork=`/`NetworkNamespacePath=`/`JoinsNamespaceOf=`/`RestrictAddressFamilies=` | ya |
| TCP6-38 (v7) | `ns_verified_osrelease` tidak ada di config | `unproven` (`ipv6_v5_ns_6`) dan `namespace:tidak_terbukti`; merah |
| TCP6-39 (v7) | `ns_verified_osrelease` ≠ `/proc/sys/kernel/osrelease` (kernel di-upgrade) | `unproven`; merah |
| TCP6-40 (v7) | `/proc/sys/kernel/osrelease` tidak terbaca | `unproven`; merah |
| TCP6-41 (v7) | N1–N6 semuanya cocok | `namespace_identity` ok |
| TCP6-42 (v7) | tes statis: tidak ada kunci config / env yang mengganti konstanta inode N1/N2 | ya |
| CLI-01 (v2) | CLI helper `install` tanpa `--rpc-mode`, non-produksi, ro-capable | terpasang `ro` (`.install_complete.rpc_mode == "ro"`, `run_script_kind == "ro"`) |
| CLI-02 (v2) | CLI `install` tanpa `--rpc-mode`, non-produksi, `ro_service=false` | exit 4 `mode ro tidak tersedia`; **tidak** ada direktori/skrip classic |
| CLI-03 (v2) | CLI `install --rpc-mode classic`, non-produksi | classic (eksplisit, perilaku lama) |
| CLI-04 (v2) | CLI `install` tanpa `--rpc-mode`, produksi | exit 2 (G1, tetap) |
| CLI-05 (v2) | `op_install(...)` in-process tanpa argumen mode | `ro` / exit 4 bila tidak ro-capable; tidak pernah classic |
| CLI-06 (v2) | `--rpc-mode` nilai tidak sah (`RO`, `""`, `readonly`) | exit 2 |
| CLI-07 (v2) | tes statis: tidak ada literal default `"classic"` pada argumen/parameter helper | ya |
| RC-01 | `recover_expired` dengan terminal `migrate:*` | baris tidak berubah |
| RC-02 | `retry_uncertain_removals` tahap 1 & 2 dengan `migrate:*` | tidak disentuh |
| RC-03 | `remove_terminal` (unbind/cleanup) pada `migrate:*` | klaim gagal, tidak ada helper |
| RC-04 | `claim_token` job pada terminal `migrate:*` | 0 baris |
| RC-05 | `_release_terminal` pada `migrate:*` | tidak disentuh |
| ST-01 | static scan termasuk `provisioner/` | tidak ada panggilan terlarang |
| ST-02 | `capability=` tidak pernah dikirim dari `api/`, `scheduler.py` | ya |
| ST-03 | setiap `INSERT INTO mt5_terminals` menyebut `rpc_mode` | ya |

---

## 9. Acceptance criteria

### 9.1 Kriteria kode (implementasi G2)

1. **Default dan konfigurasi**
   - CFG-01…08 PASS.
   - Konfigurasi produksi tidak lengkap → proses keluar 78 sebelum `migrate()`, scheduler, dan HTTP (SG-02…05).
2. **Tanpa classic di produksi**
   - Dengan enforcement `strict` tidak ada jalur yang:
     - membuat `MT5LinuxProvider`;
     - memanggil `probe_rpyc_session`;
     - meminta helper install/start classic.
   - Dibuktikan oleh RT-02, RT-10, RT-11, RT-13, HC-02, HC-05, PV-02, dan inventaris §2.9 (X1–X13 semua tertutup).
3. **Tanpa fallback:** tidak ada ekspresi `or "classic"` atau `else "mt5linux"` di kode non-tes. Tes statis ST-03 ditambah grep di CI-lokal.
4. **Gerbang kebijakan:** setiap jalur di §0.1 yang berbicara dengan terminal melewati `rpc_gate`. Dibuktikan oleh RT/SC/API/HC.
5. **Migrasi dan kredensial:** saat migrasi aktif, tidak ada DELETE `mt5_credentials` dari jalur mana pun (API-06, API-07, SY-02, SY-03, SC-03).
6. **Lock:** tidak pernah ada dua sync atau verifikasi bersamaan untuk satu akun (CC-01…03).
7. **Pemulihan:** pemulihan tidak pernah menyentuh `migrate:*` (RC-01…05).
8. **Regresi:** full backend suite, dilaporkan dari satu run JUnit XML.
   - Tidak ada kegagalan baru di luar 3 kegagalan lingkungan yang sudah diketahui:
     - `test_deploy_backup_permission` ×2;
     - `test_migrasi_data_sqlite_ke_postgresql`.
   - `test_helper_main_menolak_non_root` tetap skip / NOT VERIFIED.
9. **Lint:** gate CI `ruff --select E9,F63,F7,F82` lulus.
10. **Batas cakupan:** tidak ada perubahan pada Flutter, workflow CI, `deploy/*`, VPS, atau database produksi.
11. **(v2) Default helper:**
    - CLI-01…07 PASS.
    - Tidak ada jalur helper atau API yang menghasilkan classic tanpa `--rpc-mode classic` / `rpc_mode="classic"` eksplisit.
    - Tes lama yang diperbarui dicantumkan per berkas di laporan implementasi, tanpa skip atau hapus.
12. **(v2/v3) Lock:**
    - CC-01…CC-20 PASS.
    - **(v3)** Lock tidak pernah dihapus atau diganti selama proses hidup, termasuk saat penghapusan akun (CC-14…CC-20).
    - **(v4)** LK-01…LK-09 PASS: `account_id` hanya dari jalur terautentikasi + kepemilikan; ID yang tidak ada atau milik customer lain tidak membuat lock; registry berbatas keras dan fail-closed.
    - Urutan lock §3.5.3 dibuktikan per jalur. `evict` tidak pernah mengganti lock (CC-05/06/08).
    - Provider tidak pernah dipakai setelah disconnect, dan tidak pernah dipakai paralel (CC-09).
13. **(v2) Migrasi 009:**
    - MIG-01…MIG-16 PASS di PostgreSQL ephemeral, dengan login role `transcendence_app`/`transcendence_system`/migrator yang sebenarnya (bukan superuser, bukan `SET ROLE`).
    - Log tes memuat DSN tes yang sudah disamarkan dan bukti bahwa DSN itu cluster sementara.
    - Tidak ada koneksi ke DB produksi/staging.
14. **(v2) Startup guard:**
    - SG-01…SG-16 dan SC-PAR PASS.
    - Kegagalan guard menghasilkan exit 78 sebelum `migrate()`, pemulihan, scheduler, dan HTTP (SG-07).
    - **(v3)** SG-17 PASS: tidak ada penerapan migrasi otomatis.
15. **(v3) TCP6/IPv6:**
    - TCP6-01…TCP6-14 PASS.
    - Ketiga kondisi dibuktikan tes: IPv6 aktif (tcp6 wajib diaudit), IPv6 nonaktif terverifikasi (hanya dengan bukti kernel + deklarasi), dan status tidak terbukti (readiness merah).
    - Tidak ada jalur yang menghasilkan `audit_complete=true` tanpa `ipv6_state` terbukti.
    - **(v7)** TCP6-38…TCP6-42 PASS dan prosedur KV-1…KV-5 terdokumentasi di runbook; tanpa bukti KV untuk kernel yang berjalan, `namespace_identity` tidak pernah ok.
    - **(v6)** TCP6-31…TCP6-37 PASS: identitas namespace dibuktikan N1–N5; kesamaan `/proc/1/ns/net` saja tidak pernah cukup (container ditolak).
    - **(v5)** TCP6-21…TCP6-30 PASS: token `ipv6.disable=1` saja **tidak pernah** menghasilkan `disabled_verified`; efektivitas dibuktikan V1–V6 tanpa sysfs; tidak terbukti → merah.
    - **(v4)** TCP6-15…TCP6-20 PASS: IPv6 aktif, sysctl disabled, boot disabled, bukti tidak lengkap; E3 = `/proc/sys/net/ipv6` (procfs); sysfs `/sys` tidak pernah menjadi bukti.
16. **(v3/v4/v5) Staging:** urutan S0–S9 (§9.3) terdokumentasi di runbook; **(v4)** S1/S2 memakai §9.3.1 (M1 snapshot ekspor atau M2 hentikan penulisan), dengan identitas snapshot, waktu UTC, manifest, dan bukti restore; **(v5)** manifest = fingerprint isi baris §9.3.2 (sumber == restore), dan M2 hanya sah dengan inventaris writer lengkap + W1–W8 §9.3.3; **(v6)** capability probe CP-1…CP-8 dan MF-01…MF-10 PASS, format `trx-manifest-v3` (v7), serta W9–W10 (deteksi perubahan dan ketersediaan pemantauan) lulus. Pelaksanaannya di luar G2 dan bukan syarat merge kode G2, tetapi syarat sebelum enforcement `migration` di staging.

### 9.2 Gerbang staging sebelum enforcement `migration` (v2, K-D2)

Enforcement `migration` di staging **tetap** menjadi target. Enforcement itu
**tidak boleh diaktifkan** sebelum staging memiliki minimal **satu terminal
RO yang benar-benar teruji** dengan bukti di bawah.

Selama gerbang belum lulus:
- staging berjalan dengan `MT5_RPC_ENFORCEMENT=off` yang **diset eksplisit**. Ini diizinkan hanya non-produksi; `validate_production` tetap menolaknya di produksi;
- startup mencatat peringatan `staging_ro_gate_pending`.

Langkah dan bukti:

| # | Langkah | Bukti wajib | Lulus bila |
|---|---|---|---|
| ST-1 | Staging ro-capable | `helper selfcheck --nonce` (v3) dari API staging | `ro_service=true`, `wine_python_ok`, `secret_ok`, bukti cocok (SG-h/SG-i); `broker_allow_mode=cidr` |
| ST-2 | Terminal RO dibuat **lewat jalur API normal** (`/mt5/connect` self-link atau `/accounts/bind`), bukan manual | baris `mt5_terminals` + job `connected` | `rpc_mode='ro'`, `lifecycle='connected'`, `op_owner IS NULL`, `provision_id` = marker helper |
| ST-3 | Inventaris helper | `helper audit-all` (v3: termasuk `ipv6_state` + `ipv6_evidence`) | `ipv6_state ∈ {enabled, disabled_verified}` sesuai §3.7 (tidak diasumsikan); `audit_complete=true`, `inventory_errors=[]`, terminal itu `phase=complete`, `complete.rpc_mode=ro`, `run_script_kind=ro`, `listener_owned=true` |
| ST-4 | Probe | `probe_ro_session` dengan token; koneksi tanpa token; koneksi token salah | dengan token OK; tanpa token & token salah **ditolak**; handshake RPyC classic ke port itu **gagal** |
| ST-5 | **Koneksi password investor** | hasil `live_verify_account` (klasifikasi + `account_info` tersaring) | `class="ok"`; `trade_allowed is False`; `login`/`server` sama dengan akun ter-bind; `trade_mode` tercatat |
| ST-6 | Penolakan password master (akun uji yang sama atau akun demo) | percobaan connect dengan password master | `class="master_password"`; **tidak** ada kredensial tersimpan; tidak ada token diterbitkan |
| ST-7 | Sync nyata | satu `sync_account` | `status="ok"`; `enforce_investor_session` lolos; deals/orders/positions terbaca (jumlah dicatat, boleh 0 bila akun baru, dengan alasan tertulis) |
| ST-8 | Tidak ada jalur order | panggilan `order_send`/`order_check`/`positions_close` ke provider dan ke `ro_service` (`conn.root.order_send`) | provider `ReadOnlyViolationError`; layanan `AttributeError`; status akun broker tidak berubah |
| ST-9 | Kebijakan dengan enforcement `migration` disimulasikan pada salinan konfigurasi | gerbang `rpc_gate.check` terhadap terminal ST-2 | `ok` (ro); terminal classic staging lain → `classic_blocked` |
| ST-10 | Laporan bukti | `STAGING-RO-EVIDENCE.md` + JSON | memuat: commit hash, `helper_sha256`, waktu UTC, hasil ST-1…ST-9, operator; **tanpa** password, token, secret, DSN, atau nomor akun penuh (login disamarkan, mis. `12****89`) |

- Persetujuan eksplisit pemilik proyek atas laporan ST-10 → baru `MT5_RPC_ENFORCEMENT=migration` di staging.
- Gagal di langkah mana pun → enforcement tetap `off`, temuan dicatat, **tidak** ada fallback ke classic untuk terminal baru.
- Gerbang ini dijalankan sebagai langkah S7 dari urutan deployment staging §9.3.
- Pelaksanaan gerbang ini adalah kegiatan staging (di luar implementasi kode G2) dan butuh persetujuan terpisah. Implementasi G2 hanya menyediakan alatnya (`selfcheck` v3, `audit_all`, `rpc_gate`, peringatan startup).

### 9.3 Urutan deployment staging (v3, K3)

**Prinsip:**
- **Implementasi G2 tidak berisi jalur apa pun yang menerapkan migrasi 009 ke staging atau produksi secara otomatis.**
  - Startup API berjalan sebagai role aplikasi non-owner. Bila 009 tertunda, `migrate()` melempar `MigrationPrivilegeError` (`db.py:684`, perilaku B12.4 yang ada), dan G2 memetakannya ke exit 78. Startup tidak pernah mencoba memakai kredensial migrator.
  - Tidak ada env, flag, atau endpoint yang membuat API menjalankan DDL.
  - Tes `SG-17` mengunci perilaku ini.
- Setiap langkah di bawah adalah kegiatan **manual operator** dengan persetujuan tertulis. Langkah berikutnya tidak boleh dimulai bila langkah sebelumnya gagal.
- Urutan ini khusus **staging**. Urutan produksi dispesifikasikan terpisah sesudah G3.

| Langkah | Kegiatan | Bukti wajib | Gagal → |
|---|---|---|---|
| **S0** Prasyarat | Commit G2 disetujui dan di-tag; paket rilis API + helper dari commit yang sama; `sha256` helper dicatat untuk `PROVISIONER_HELPER_SHA256` | hash commit, hash helper | berhenti |
| **S1** Backup | **(v4)** sesuai §9.3.1: snapshot konsisten (M1) atau penulisan dihentikan selama snapshot (M2, default); `deploy/backup_db.sh` (skrip yang ada, tidak diubah) pada DB staging, sebelum perubahan apa pun | path berkas, ukuran, `sha256`, waktu UTC mulai/selesai, **identitas snapshot**, manifest hitungan pembanding dari snapshot yang sama | berhenti |
| **S2** Verifikasi backup | **(v4)** restore ke database sementara di **cluster terpisah** (bukan DB staging aktif, dan sebaiknya bukan cluster yang sama; cluster sama hanya dengan persetujuan): restore sukses; `schema_migrations` sama; hitungan + sidik konten per tabel inti sama dengan manifest S1 (dari snapshot yang sama, §9.3.1); lalu DB sementara di-drop | log restore, manifest S1 vs hasil restore per tabel, identitas snapshot, waktu UTC, bukti drop | berhenti (backup tidak dipercaya) |
| **S3** Persetujuan 009 | pemilik proyek menyetujui penerapan 009 di staging, merujuk bukti S1–S2 | persetujuan tertulis (tanggal, nama) | berhenti |
| **S4** Terapkan 009 | dijalankan manual oleh **migrator non-superuser** (pola B12.4, DSN migrator hanya di sesi operator, tidak di `.env` API), satu transaksi; API staging **dihentikan** selama langkah ini | output migrasi, `schema_migrations` memuat 009, `rolsuper=false` migrator | transaksi rollback otomatis; berhenti; analisa |
| **S5** Verifikasi skema (read-only) | sebagai migrator dan **login** role `transcendence_app` / `transcendence_system`: `pg_class.relrowsecurity/relforcerowsecurity`, `pg_policies` tabel 009, `has_table_privilege` per role × (SELECT, INSERT, UPDATE, DELETE) sesuai §3.3.1, `pg_indexes` (indeks unik parsial, predikat persis), CHECK state = `MIGRATION_STATES`, owner = migrator, PUBLIC tanpa hak. **Tanpa** menulis baris uji di staging | keluaran query, dibandingkan dengan matriks §3.3.1 | `rollback/009_rpc_migrations.down.sql` oleh migrator **setelah persetujuan** (tabel kosong, sehingga down diizinkan); berhenti |
| **S6** Deploy helper + API (selfcheck v3) | pasang helper dan API dari paket S0 bersamaan; `PROVISIONER_HELPER_SHA256` = hash S0; `MT5_RPC_ENFORCEMENT=off` **eksplisit**; start API | log startup guard lulus (SG-a…SG-k), `helper selfcheck --nonce` v3 cocok, `audit-all` dengan `ipv6_state` (§3.7) | kembali ke rilis sebelumnya (kode pra-G2 kompatibel dengan tabel 009 yang ada tetapi tidak dipakai); 009 dibiarkan |
| **S7** Gerbang RO | ST-1…ST-10 (§9.2), termasuk koneksi password investor | laporan `STAGING-RO-EVIDENCE.md` + JSON | enforcement tetap `off`; temuan dicatat |
| **S8** Persetujuan enforcement | pemilik proyek menyetujui `MT5_RPC_ENFORCEMENT=migration` berdasarkan laporan S7 | persetujuan tertulis | tetap `off` |
| **S9** Aktifkan `migration` | set env, restart API | log startup; scheduler: terminal RO `ok`, terminal classic `skipped_policy`, tidak ada `credentials_rejected` baru; `/mt5/status` classic → `classic_blocked` | kembali ke `off` (non-produksi saja), tanpa mengubah terminal |

#### 9.3.1 Konsistensi snapshot backup (v4, P3)

**Masalah.** `deploy/backup_db.sh` menjalankan `pg_dump "$DB_NAME"` sendiri (`backup_db.sh:70`) tanpa `--snapshot`. pg_dump konsisten secara internal (satu transaksi), tetapi hitungan `SELECT count(*)` yang diambil terpisah **tidak** dijamin dari snapshot yang sama. Selisih bisa muncul karena sync/scheduler menulis di antaranya, sehingga perbandingan S2 tidak sah. Skrip itu tidak diubah (dilindungi).

**Dua metode. Salah satu wajib; bila M1 tidak dapat dijamin, M2 wajib.**

| | **M1 — snapshot ekspor (identitas sama persis)** | **M2 — hentikan penulisan selama snapshot (default staging)** |
|---|---|---|
| Kapan | operator menjalankan pg_dump manual dengan `--snapshot` (bukan lewat `backup_db.sh`), dengan validasi yang sama (gzip `-t`, ukuran > 0, marker header) | memakai `backup_db.sh` apa adanya |
| Langkah | 1) sesi A: `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();` 2) sesi A (tetap terbuka): manifest fingerprint isi (§9.3.2); 3) `pg_dump --snapshot=<id>`; 4) sesi A `COMMIT` setelah pg_dump selesai | 1) **(v5)** inventaris writer lengkap (§9.3.3) dan hentikan semua writer yang terinventaris; 2) bukti tidak ada writer: pemeriksaan §9.3.3 W1–W6 (bukan hanya sesi `transcendence_app`/`transcendence_system`); 3) manifest **sebelum** (§9.3.2, READ ONLY tx); 4) `backup_db.sh`, dengan pemantauan writer selama berjalan (§9.3.3 W7); 5) manifest **sesudah**; 6) API dan writer lain boleh start lagi **hanya** setelah 5 dan pemeriksaan akhir W1–W6 |
| Bukti konsistensi | manifest dan dump memakai snapshot yang sama (`<id>`) | manifest sebelum == sesudah (setiap tabel); tanpa sesi penulis selama jendela |
| Gagal | snapshot gagal diekspor / sesi A putus sebelum pg_dump selesai → ulangi | manifest sebelum ≠ sesudah, atau ada sesi penulis → backup **tidak sah**, ulangi M2 |

**Manifest (kedua metode).** **(v5) Digantikan oleh §9.3.2.** Versi v4 (hanya `count(*)` + fingerprint kunci primer) **tidak cukup**: ia tidak membuktikan nilai kolom sama, dan daftar tabelnya memakai nama yang salah (`sync_state`/`trades`; nama sebenarnya `mt5_sync_state`/`mt5_trades`). Daftar tabel v5 diturunkan dari katalog, bukan ditulis tangan.

**Identitas snapshot dan waktu (dicatat di laporan S1/S2, tanpa secret):**
- M1: nilai `pg_export_snapshot()`, `txid_current_snapshot()`, `pg_current_wal_lsn()` di sesi A, waktu UTC `BEGIN` dan `COMMIT` sesi A, waktu UTC mulai/selesai pg_dump.
- M2: `pg_current_wal_lsn()` dan `txid_current_snapshot()` pada manifest sebelum dan sesudah, waktu UTC stop API / manifest sebelum / mulai & selesai `backup_db.sh` / manifest sesudah / start API, bukti `pg_stat_activity` (jumlah sesi per role, tanpa query teks).
  - Catatan: LSN boleh maju karena checkpoint/autovacuum. Kesetaraan **manifest** yang menjadi bukti, bukan LSN.
- Keduanya: `STAMP`, path, ukuran, `sha256` berkas backup.

**Bukti restore (S2):**
- log restore (exit code, durasi);
- manifest DB hasil restore, dibandingkan baris demi baris dengan manifest S1 (harus identik);
- `schema_migrations` identik;
- waktu UTC restore dan drop;
- bukti DB sementara sudah tidak ada (`SELECT 1 FROM pg_database WHERE datname=...` kosong).

**Tidak mengubah DB aktif:**
- manifest hanya `SELECT` dalam `READ ONLY`;
- M1 memakai transaksi `READ ONLY`;
- M2 menghentikan **proses penulis** (layanan), tidak mengubah role, privilege, atau konfigurasi DB, dan tidak memakai `ALTER DATABASE`/`default_transaction_read_only`;
- restore hanya ke DB sementara di cluster terpisah.

#### 9.3.2 Fingerprint isi baris (v5, Q2)

**Tujuan.** Membuktikan bahwa **setiap nilai di setiap baris** setiap tabel aplikasi di snapshot sumber identik dengan hasil restore, tanpa mengekspor nilai sensitif.

**Cakupan tabel (diturunkan dari katalog, fail-closed):**
- Semua tabel biasa dan partisi di skema `public` dan `transcendence_meta` (`pg_class.relkind IN ('r','p')`), ditambah `schema_migrations`.
- Daftar, jumlah kolom, dan tipe kolom dicatat di manifest.
- **(v6, R3) Urutan kanonik — satu aturan untuk generator, manifest, dan tes:**
  - **tabel dengan PK:** urut menurut kolom PK sesuai urutan kolom PK. Kolom integer diurutkan numerik; kolom teks `COLLATE "C"` (bytewise UTF-8, tidak bergantung collation/ICU/libc cluster). Tipe PK lain tidak didukung → gagal;
  - **semua tabel tanpa PK (v7, T2; menggantikan "ORDER BY rh" v6):** baris dikelompokkan menurut pasangan `(rh, row_bytes)` dengan multiplisitas `c = count(*)`, lalu kelompok diurutkan **bytewise** `ORDER BY rh, row_bytes` (bytea = memcmp, tanpa collation).
    - Pasangan unik per kelompok, sehingga urutan **total** dan deterministik.
    - Baris identik menjadi **satu** entri dengan `c`, sehingga tidak ada lagi ikatan urutan.
    - Bila dua baris berbeda isi kebetulan ber-`rh` sama (collision), `row_bytes` memutus ikatan secara deterministik.
    - **Analisis v6 (alasan koreksi):** `ORDER BY rh` saja tidak total bila `rh` sama untuk isi berbeda (collision). `row_number() OVER (ORDER BY rh)` memberi nomor yang bergantung urutan eksekusi pada ikatan. Keyset `WHERE rh > :last_rh` melompati atau mengulang kelompok `rh` yang terbelah batas halaman, dan aturan "semua duplikat ke chunk yang sama" membuat chunk melampaui N tanpa batas.
  - Aturan v5 "ORDER BY seluruh kolom ter-serialisasi" **dihapus** karena tidak selaras dengan langkah algoritma.
  - Mode urutan (`pk` atau, v7, `rh_rb_grouped`) dicatat per tabel di manifest; mode yang berbeda antara sumber dan restore → tidak sah.
- Tabel atau kolom bertipe di luar daftar dukung → generator **gagal** (`unsupported_type:<tabel>.<kolom>:<tipe>`). Tidak ada yang dilewati diam-diam.

**Parameter sesi (sumber dan restore identik, dicatat di manifest):**
- `SET TimeZone='UTC'`, `SET DateStyle='ISO, YMD'`, `SET IntervalStyle='iso_8601'`, `SET extra_float_digits=3`, `SET bytea_output='hex'`;
- transaksi `READ ONLY`;
- `server_encoding` harus `UTF8` di kedua sisi;
- **versi major PostgreSQL sumber == restore**. Berbeda → hasil dinyatakan *tidak dapat dibandingkan* (merah), karena representasi teks float/jsonb dapat berubah antar versi.

**Serialisasi kanonik per nilai** (TLV dengan panjang, sehingga tidak ada ambiguitas pemisah):
- **(v6)** Format byte biner pasti didefinisikan di §9.3.2a (`TLV_VALUE`). Bentuk ASCII v5 (`N` / `V<len>:`) diganti. Bentuk v5 sudah tidak ambigu, tetapi diganti agar satu format byte dipakai di seluruh digest.
- `NULL` → `0x00`.
- Non-NULL → `0x01` ‖ `u32be(panjang)` ‖ `<bytes kanonik>`.

| Tipe | Bytes kanonik |
|---|---|
| `text`, `varchar`, `char` | `convert_to(col, 'UTF8')` (apa adanya, tanpa trim/normalisasi; `char(n)` termasuk padding) |
| `smallint`, `integer`, `bigint` | `col::text` (desimal, tanda `-`) |
| `numeric` | `col::text` (mempertahankan skala: `1.50` ≠ `1.5`, sesuai identitas nilai PG); `NaN` → `NaN` |
| `real`, `double precision` | `col::text` dengan `extra_float_digits=3` (round-trip presisi penuh); `NaN`, `Infinity`, `-Infinity` apa adanya; `-0` dipertahankan |
| `boolean` | `t` / `f` |
| `date` | `to_char(col, 'YYYY-MM-DD')` (`infinity` apa adanya) |
| `timestamp` | `to_char(col, 'YYYY-MM-DD"T"HH24:MI:SS.US')` |
| `timestamptz` | `to_char(col AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')` |
| `json` | `convert_to(col::text, 'UTF8')` (json menyimpan teks asli; restore mempertahankannya) |
| `jsonb` | `convert_to(col::text, 'UTF8')` (bentuk kanonik jsonb: urutan kunci & spasi dinormalisasi PG) |
| `bytea` | bytes mentah (`col`); **kolom sensitif → lihat di bawah** |
| `uuid` | `col::text` (huruf kecil) |
| lainnya (array, enum, interval, inet, …) | **tidak didukung** di v5 → gagal (fail-closed); penambahan tipe = revisi versi manifest |

Catatan: di skema saat ini, waktu disimpan sebagai `TEXT` dan angka sebagai `INTEGER`/`BIGINT`/`DOUBLE PRECISION`. Aturan `timestamp`/`json`/`bytea` disertakan agar generator tetap deterministik bila tipe itu muncul.

**Kolom sensitif (hashing internal, tidak pernah diekspor):**
- Daftar eksplisit (direview di runbook), saat ini:
  - `mt5_credentials.enc_password`;
  - `users.password_hash`;
  - `auth_tokens.token_hash`;
  - `mt5_connect_jobs.secret_hash`, `lock_ip_hash`, `lock_acct_hash`;
  - `auth_throttle.key_hash` / `auth_lockouts.key_hash`.
- **Plus aturan default-sensitif:** setiap kolom yang namanya cocok `(password|secret|token|hash|key|enc_)` diperlakukan sensitif kecuali dikecualikan eksplisit di runbook (mis. `key_version` = tidak sensitif, ditandai).
- Kolom tak terklasifikasi yang cocok pola tanpa keputusan → generator gagal.
- Untuk kolom sensitif, bytes kanonik nilai diganti `SENS` (§9.3.2a, 32 byte) di dalam query server-side. Nilai mentah tidak pernah keluar dari sesi DB, dan tidak pernah muncul di manifest, log, atau output klien.

**Kunci per-run `K`:**
- 32 byte acak, dibuat operator untuk satu pasangan sumber/restore;
- dikirim sebagai parameter query (bukan literal di berkas SQL);
- tidak disimpan di disk, dimusnahkan setelah S2;
- manifest hanya mencatat `key_fp` (§9.3.2a, 16 hex pertama) untuk membuktikan kunci yang sama dipakai di kedua sisi.
- `K` mencegah tebakan offline terhadap digest (mis. tabel satu baris dengan nilai berentropi rendah).
- Dibangun dari fungsi bawaan `pg_catalog.sha256(bytea)`; **tanpa** memasang ekstensi `pgcrypto` atau ekstensi apa pun di DB aktif (tidak ada perubahan DB).
  - **(v6, R2)** Klaim v5 "PG ≥ 11" **dicabut** sebagai dasar keputusan.
  - **Minimum versi ditetapkan PostgreSQL 16**: versi yang didukung dan diuji proyek (`docs/MULTI-CUSTOMER-ARSITEKTUR.md:30`, pgserver 16.2 di `docs/HASIL-TEST-MULTI-CUSTOMER.md:4,12`). Versi ini juga syarat `pg_stat_database.sessions` (W9) dan perilaku hak skema PG ≥ 15 yang dipakai `db.py:740`.
  - Versi awal kemunculan `sha256(bytea)` di dokumentasi PostgreSQL **tidak dapat diverifikasi** dari sesi spesifikasi ini (host dokumentasi tidak dapat dijangkau). Karena itu keabsahan **tidak** bergantung pada klaim versi, melainkan pada *capability probe* di bawah.

**Capability probe (v6, R2; wajib, sebelum fingerprint, di sumber DAN restore, dalam transaksi `READ ONLY` yang sama):**

| # | Probe | Lulus bila |
|---|---|---|
| CP-1 | `SELECT current_setting('server_version_num')::int` | ≥ `160000`, dan major sumber == major restore |
| CP-2 | `SET LOCAL search_path = pg_catalog` (hanya parameter sesi, bukan perubahan DB); semua panggilan fungsi di generator ditulis berkualifikasi `pg_catalog.` | — |
| CP-3 | `SELECT p.oid FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='pg_catalog' AND p.proname='sha256' AND p.proargtypes = '17'::oidvector AND p.prorettype = 17` (17 = `bytea`) | tepat 1 baris (fungsi bawaan; bayangan di skema lain tidak berpengaruh) |
| CP-4 | KAT: `encode(pg_catalog.sha256(''::bytea),'hex')` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| CP-5 | KAT: `encode(pg_catalog.sha256('abc'::bytea),'hex')` | `ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad` |
| CP-6 | KAT: `encode(pg_catalog.sha256(convert_to('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq','UTF8')),'hex')` | `248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1` |
| CP-7 | KAT 1 MiB: `encode(pg_catalog.sha256(convert_to(repeat('a', 1048576),'UTF8')),'hex')` dibandingkan dengan nilai yang dihitung `hashlib.sha256` di sisi operator | sama |
| CP-8 | fungsi pendukung framing ada di `pg_catalog`: `int4send(int4)`, `int8send(int8)`, `convert_to(text,name)`, `octet_length(bytea)`, `encode(bytea,text)`, `to_char` | semua ada |

- Gagal satu pun → generator **berhenti** dengan `fingerprint_capability_failed:<CP-n>`. Manifest tidak dibuat; status backup **tidak sah**.
- Tidak ada fallback ke hitungan baris, `md5`, atau pemasangan ekstensi.
- Hasil CP-1…CP-8 dicatat di manifest (`capability_probe`).

**Algoritma (per tabel; tiap tahap SQL berbatas memori):**
1. `row_bytes` = konkatenasi TLV semua kolom dalam urutan `attnum` (kolom yang di-drop dilewati; daftar kolom dicatat).
2. `rh` = digest `ROW` (§9.3.2a): 32 byte per baris; tidak diekspor.
3. Urutan: aturan kanonik R3/T2 di atas (PK: integer numerik, teks `COLLATE "C"`; tanpa PK v7: kelompok `(rh, row_bytes)` dengan `c`, `ORDER BY rh, row_bytes`). Aturan yang **sama** dipakai generator sumber, generator restore, dan implementasi referensi tes.
4. **Chunk:** `chunk_no = (row_number() OVER (ORDER BY <urutan>) - 1) / N`, dengan `N = 10 000` baris (dicatat di manifest; sama di kedua sisi). Per chunk: `cd` = digest `CHUNK` (§9.3.2a), dengan `string_agg(rh, ''::bytea ORDER BY <urutan>)` sebagai bagian akhir input. Ukuran agregat maksimum = N × 32 byte = 320 KB, sehingga tidak ada `string_agg` tak terbatas.
5. **Tabel:** `td` = digest `TABLE` (§9.3.2a). Untuk tabel sangat besar (> 10 000 chunk), digest chunk diagregasi dua tingkat (super-chunk 10 000 digest), sehingga tetap berbatas.
6. **Manifest akhir:** `md` = digest `MANIFEST` (§9.3.2a), dengan tabel diurutkan bytewise menurut nama berkualifikasi (`COLLATE "C"`).
7. Eksekusi per chunk dapat dijalankan sebagai query `WHERE pk > :last ORDER BY pk LIMIT N` berulang (keyset pagination) di **transaksi/snapshot yang sama**, sehingga memori server dan klien berbatas. ~~(v6) keyset `WHERE rh > :last_rh` untuk tabel tanpa PK~~ **dicabut di v7 (T2).** Pengganti untuk tabel tanpa PK, satu query di snapshot yang sama:

```sql
SELECT chunk_no, <CHUNK digest atas string_agg(rh || int8send(c), ''::bytea ORDER BY rh, rb)>
FROM (SELECT rh, rb, c,
             (row_number() OVER (ORDER BY rh, rb) - 1) / :N AS chunk_no
      FROM (SELECT rh, rb, count(*) AS c
            FROM (SELECT <ROW digest> AS rh, <row_bytes> AS rb FROM <tabel>) r
            GROUP BY rh, rb) g) x
GROUP BY chunk_no ORDER BY chunk_no;
```

- **Determinisme:** `row_number()` atas kunci unik `(rh, rb)` → nomor kelompok dan batas chunk pasti. Tidak ada keyset halaman, sehingga tidak ada kelompok terbelah.
- **Batas chunk:** tiap chunk ≤ N **kelompok** (bukan baris). Satu kelompok duplikat sebesar apa pun (mis. 25 000 baris identik) = satu entri `rh ‖ u64be(c)` (40 byte). Agregat per chunk ≤ N × 40 byte = 400 KB.
- **Memori:**
  - `GROUP BY`/`ORDER BY`/window dieksekusi executor PostgreSQL dengan spill ke disk sesuai `work_mem` (sort/hash eksternal), sehingga memori server berbatas;
  - `rb` (row_bytes) hanya hidup di server dan tidak pernah dikirim ke klien;
  - klien hanya menerima `chunk_no` dan digest;
  - baris sangat lebar memperbesar ruang sort sementara di disk (dicatat sebagai biaya), bukan memori.
- **Collision:**
  - dua baris berbeda dengan `rh` sama tetap dua kelompok terpisah (dibedakan `rb`) dan keduanya dihitung;
  - karena entri digest hanya `rh ‖ c`, dua **tabel** berbeda hanya bisa menghasilkan digest sama bila terjadi collision SHA-256 pada `rh` yang menukar isi. Itu dibatasi ≈ 2⁻¹²⁸ (sama dengan §9.3.2 keterbatasan).
- **Identik:** baris identik → `(rh, rb)` identik → kelompok sama dengan `c` sama → digest sama, berapa pun urutan fisiknya. Jumlah duplikat **ikut** di-hash (2 salinan ≠ 3 salinan).
- **Satu generator:** teks SQL generator (setelah substitusi parameter non-rahasia) di-hash `generator_sql_sha256` dan dicatat di `PARAMS`. Sumber dan restore wajib bernilai sama.

**Isi manifest yang diekspor (tanpa nilai baris):**
- versi format (**v7:** `trx-manifest-v3`; manifest `v1`/`v2` tidak dapat dibandingkan dengan `v3`);
- parameter sesi, versi PG, encoding, `N`, `key_fp`;
- per tabel: nama, daftar kolom + tipe + flag sensitif, `count`, jumlah chunk, digest tiap chunk (hex), `td`;
- untuk kunci primer integer, rentang PK pertama/terakhir per chunk (untuk melokalisasi selisih); PK teks hanya nomor chunk;
- `md`;
- **nilai sequence** (`last_value`, `is_called` per sequence di `pg_sequences`), karena sequence tidak tercakup isi tabel;
- **fingerprint skema:** sha256 dari keluaran katalog terurut (kolom, tipe, default, constraint, indeks, policy RLS, grant) di kedua sisi.

#### 9.3.2a Format byte digest `trx-manifest-v3` (v6, R4; v7 naik versi karena `CHUNK_G`)

**Primitif:**
- `u8(x)`: 1 byte.
- `u32be(n)`: 4 byte big-endian, 0 ≤ n < 2³¹. SQL: `pg_catalog.int4send(n)`.
- `u64be(n)`: 8 byte big-endian, 0 ≤ n < 2⁶³. SQL: `pg_catalog.int8send(n)`.
- `STR(t)` = `u32be(octet_length(utf8(t)))` ‖ `utf8(t)`. Semua teks (label, nama skema/tabel/kolom, tipe, kunci & nilai parameter) **selalu** memakai `STR`. Tidak ada konkatenasi teks mentah.
- `LBL(x)` = `STR("trx-manifest-v3/" ‖ x)`: label domain, berbeda per jenis digest.
- `K` = tepat 32 byte (panjang tetap; ditolak bila ≠ 32).
- `H(m)` = `pg_catalog.sha256(m)` (32 byte; sisi Python: `hashlib.sha256`).

**Nilai:** `TLV_VALUE(v)` = `u8(0x00)` bila NULL; selain itu `u8(0x01)` ‖ `u32be(len(b))` ‖ `b`, dengan `b` = bytes kanonik tabel tipe §9.3.2.

**Digest:**

| Nama | Definisi byte |
|---|---|
| `SENS(tbl, col, v)` | `H(K ‖ LBL("sens") ‖ STR(tbl) ‖ STR(col) ‖ TLV_VALUE(v))` |
| `ROW(tbl, row)` | `H(K ‖ LBL("row") ‖ STR(tbl) ‖ u32be(ncol) ‖ TLV_VALUE(c₁) ‖ … ‖ TLV_VALUE(cₙ))`, dengan cᵢ = nilai kolom (kolom sensitif: `TLV_VALUE(SENS(...))`), urutan `attnum` |
| `CHUNK(tbl, i, rh₁…rhₘ)` | `H(K ‖ LBL("chunk") ‖ STR(tbl) ‖ u64be(i) ‖ u32be(m) ‖ rh₁ ‖ … ‖ rhₘ)`; `rhⱼ` tepat 32 byte (mode `pk`) |
| `CHUNK_G(tbl, i, (rh,c)₁…(rh,c)ₘ)` (v7) | `H(K ‖ LBL("chunk-g") ‖ STR(tbl) ‖ u64be(i) ‖ u32be(m) ‖ rh₁ ‖ u64be(c₁) ‖ … ‖ rhₘ ‖ u64be(cₘ))`; m ≤ N kelompok, entri tepat 40 byte (mode `rh_rb_grouped`) |
| `SUPER(tbl, j, cd₁…cdₘ)` | `H(K ‖ LBL("super") ‖ STR(tbl) ‖ u64be(j) ‖ u32be(m) ‖ cd₁ ‖ … ‖ cdₘ)` (hanya bila > 10 000 chunk) |
| `COLSPEC(tbl)` | `u32be(ncol) ‖` untuk tiap kolom: `STR(nama) ‖ STR(tipe format_type) ‖ u8(sensitif 0/1)` |
| `TABLE(tbl)` | `H(K ‖ LBL("table") ‖ STR(tbl) ‖ STR(mode_urutan "pk"/"rh_rb_grouped") ‖ COLSPEC(tbl) ‖ u64be(jumlah_baris) ‖ u64be(jumlah_kelompok; = jumlah_baris untuk mode pk) ‖ u64be(N) ‖ u8(level 1/2) ‖ u64be(jumlah_komponen) ‖ d₁ ‖ … ‖ dₖ)`; dᵢ = `CHUNK`/`CHUNK_G` (level 1) atau `SUPER` (level 2) |
| `PARAMS` | `u32be(np) ‖` untuk tiap parameter terurut bytewise menurut kunci: `STR(kunci) ‖ STR(nilai)`. Isi: versi format, `server_version_num` major, `server_encoding`, `TimeZone`, `DateStyle`, `IntervalStyle`, `extra_float_digits`, `bytea_output`, `N`, **(v7)** `generator_sql_sha256` |
| `SEQS` | `u32be(ns) ‖` tiap sequence terurut nama: `STR(nama) ‖ STR(last_value atau "NULL") ‖ u8(is_called)` |
| `MANIFEST` | `H(K ‖ LBL("manifest") ‖ STR("trx-manifest-v3") ‖ PARAMS ‖ u32be(nt) ‖ (STR(tblᵢ) ‖ TABLE(tblᵢ))… ‖ SEQS ‖ STR(schema_fp_hex))` |
| `key_fp` | `H(LBL("key-fp") ‖ K)`, 16 hex pertama |

**Jaminan tanpa ambiguitas:**
- setiap komponen berpanjang variabel diawali panjangnya (`STR`, `TLV_VALUE`);
- setiap daftar diawali jumlah elemennya;
- komponen berpanjang tetap (`K`, digest 32 byte, `u32be`, `u64be`, `u8`) berada pada posisi yang ditentukan;
- label domain berbeda untuk setiap jenis digest, sehingga digest satu jenis tidak dapat ditafsirkan sebagai jenis lain.

**Versi format:** `trx-manifest-v3` (v7; v2 = rancangan v6, tidak pernah diimplementasikan) tertulis di `PARAMS`, `MANIFEST`, dan JSON manifest. Perubahan apa pun pada tabel ini = versi baru. Pembanding menolak manifest dengan versi berbeda.

**Vektor emas:**
- Implementasi referensi Python (`hashlib`, `struct.pack(">I")`/`">Q"`) menghitung digest untuk set data uji tetap.
- Hasilnya wajib identik dengan generator SQL pada PostgreSQL ephemeral (MF-01).

**Perbandingan (S2):**
- **(v6)** capability probe CP-1…CP-8 lulus di restore;
- manifest restore dihitung dengan SQL, parameter, `N`, dan `K` yang sama;
- syarat lulus: `md` identik, setiap `td` identik, setiap `count` identik, sequence identik, fingerprint skema identik;
- bila berbeda: nomor chunk/rentang PK yang berbeda dilaporkan tanpa nilai, dan backup **tidak sah**.

**Keterbatasan dan risiko collision:**
- SHA-256: peluang collision acak ≈ 2⁻¹²⁸ (batas birthday) per pasangan manifest, dapat diabaikan. Serangan collision terarah terhadap SHA-256 tidak diketahui layak saat ini. Konstruksi `sha256(K ‖ …)` bukan HMAC formal; untuk perbandingan kesamaan dengan kunci rahasia per-run, length-extension tidak relevan (penyerang tidak memegang K dan tidak memilih digest). Bila pgcrypto tersedia **tanpa** perlu dipasang, HMAC dapat dipakai; tidak diwajibkan.
- Fingerprint membuktikan kesamaan **isi tabel yang dicakup** di bawah serialisasi kanonik. **Tidak** mencakup: large objects (`pg_largeobject`; aplikasi tidak memakainya, dicek `count = 0`), statistik/planner, OID internal, posisi fisik baris, dan peran cluster-wide (di luar DB).
- Kesamaan bergantung pada versi major PG dan parameter sesi yang sama (diwajibkan; berbeda → tidak dapat dibandingkan).
- **(v6)** Keabsahan bergantung pada capability probe CP-1…CP-8, bukan pada klaim versi dokumentasi. Implementasi `sha256` server yang salah tertangkap KAT CP-4…CP-7.

**Tes manifest (v6; dijalankan di PostgreSQL ephemeral, bukan staging/produksi):**

| ID | Skenario | Lulus bila |
|---|---|---|
| MF-01 | vektor emas: implementasi Python referensi vs generator SQL | digest `ROW`/`CHUNK`/`TABLE`/`MANIFEST` identik |
| MF-02 | framing: tabel `ab`+kolom `c` vs tabel `a`+kolom `bc`; nilai `''` vs `NULL`; baris `('a','')` vs `('','a')`; parameter `k=ab,v=c` vs `k=a,v=bc` | semua digest berbeda |
| MF-03 | tabel tanpa PK: urutan fisik berbeda (INSERT terbalik, `CLUSTER`, `VACUUM FULL`) di restore | digest identik |
| MF-04 | tabel tanpa PK dengan baris duplikat penuh, duplikat melintasi batas chunk | digest identik sumber/restore; **(v7)** satu kelompok tidak pernah terbelah; tiap chunk ≤ N kelompok |
| MF-11 (v7) | **baris identik:** tabel tanpa PK berisi k salinan baris yang sama, urutan fisik diacak di restore | digest identik; 2 salinan vs 3 salinan → digest **berbeda** (multiplisitas ikut di-hash) |
| MF-12 (v7) | **hash sama, isi berbeda:** collision dipaksa lewat implementasi referensi Python dengan fungsi `rh` tersuntik (mis. konstanta) **dan** generator SQL di DB ephemeral dengan fragmen `ROW` tes yang mengembalikan konstanta | dua kelompok terpisah (dibedakan `row_bytes`); urutan deterministik; hasil Python == SQL; dua run berulang identik |
| MF-13 (v7) | **duplikat di batas chunk:** N kecil (tes, mis. N=3) dengan kelompok duplikat tepat di posisi batas | kelompok utuh di satu chunk; batas chunk sama di sumber/restore dan di dua run |
| MF-14 (v7) | **kelompok > 10 000:** 25 000 baris identik + baris lain, N = 10 000 | satu entri `c = 25000`; tidak ada chunk > N entri; memori klien tidak bergantung pada ukuran kelompok |
| MF-15 (v7) | generator sumber vs restore | `generator_sql_sha256` identik; berbeda → perbandingan ditolak |
| MF-16 (v7) | tabel tanpa PK, permutasi acak urutan INSERT 20× | digest identik di semua permutasi |
| MF-05 | PK teks dengan collation cluster restore berbeda (mis. `en_US` vs `C`) | digest identik (`COLLATE "C"`) |
| MF-06 | satu nilai di satu baris diubah (termasuk kolom sensitif, float `-0` vs `0`, numeric `1.5` vs `1.50`) | digest berbeda; chunk yang berbeda teridentifikasi tanpa nilai |
| MF-07 | `CP-3` gagal (fungsi `sha256` disimulasikan tidak ada lewat role/skema uji tanpa akses) / KAT salah (fungsi bayangan `public.sha256` dengan `search_path` default) | generator berhenti `fingerprint_capability_failed`; tidak ada manifest; bayangan `public.sha256` **tidak** dipakai |
| MF-08 | `server_version_num` < 160000 (disimulasikan) atau major sumber ≠ restore | gagal tertutup |
| MF-09 | manifest `v1` dibandingkan dengan `v2` | ditolak |
| MF-10 | output manifest dan log | tidak memuat nilai kolom sensitif maupun `K` |
- `numeric` dibandingkan beserta skalanya. `json` dibandingkan sebagai teks asli (spasi berarti).
- Biaya: pemindaian penuh setiap tabel (dua kali di M2: sebelum dan sesudah, ditambah sekali di restore). M1 menahan snapshot selama perhitungan (xmin tertahan → bloat sementara); M2 memperpanjang jendela henti-tulis. Durasi dicatat.
- `K` bocor → digest tabel kecil berentropi rendah dapat ditebak. Karena itu K tidak disimpan, dan manifest diperlakukan sebagai artefak internal.

#### 9.3.3 Inventaris writer untuk M2 (v5, Q3)

**Prinsip:**
- M2 sah **hanya** bila inventaris writer **lengkap** dan **setiap** writer terbukti berhenti selama jendela backup.
- Inventaris tidak lengkap, atau ada item yang tidak dapat diklasifikasi → **backup tidak sah**, proses berhenti di S1.
- Semua pemeriksaan read-only. Tidak ada `ALTER ROLE`, perubahan `pg_hba.conf`, atau perubahan konfigurasi DB.

**Inventaris (disusun sebelum jendela, dilampirkan ke laporan S1):**

| # | Kategori | Sumber (read-only) | Klasifikasi wajib |
|---|---|---|---|
| I1 | Role DB | `pg_roles` (semua role, `rolcanlogin`, `rolsuper`, `rolbypassrls`, `rolreplication`); keanggotaan `pg_write_all_data` (PG14+) / `pg_database_owner`; `has_table_privilege(role, tbl, 'INSERT,UPDATE,DELETE,TRUNCATE')` untuk setiap tabel; pemilik tabel/skema | setiap role LOGIN: *writer-capable* atau *read-only*. Role writer-capable yang tidak dikenal → inventaris tidak lengkap |
| I2 | Sesi | `pg_stat_activity` **semua** backend (bukan hanya role aplikasi): `pid`, `usename`, `application_name`, `client_addr`, `backend_type`, `state`, `backend_xid`, `backend_xmin`, `xact_start` | setiap backend diklasifikasi (lihat W2) |
| I3 | Transaksi prepared | `pg_prepared_xacts` | harus kosong |
| I4 | Replikasi logis | `pg_subscription` (DB ini sebagai subscriber), worker `logical replication apply` di `pg_stat_activity` | subscriber aktif = writer → wajib tidak ada / dinonaktifkan sebelum jendela (di luar G2, butuh persetujuan) |
| I5 | Akses jaringan | `pg_hba_file_rules` (host/user/db yang diizinkan), `listen_addresses` | setiap host jarak jauh yang diizinkan untuk role writer-capable dicatat sebagai calon writer eksternal |
| I6 | Unit systemd | `systemctl list-units --all`, `list-unit-files`; unit yang memuat DSN DB / menjalankan backend / tools (`transcendence*`, worker, helper) | writer / non-writer / dihentikan |
| I7 | Timer | `systemctl list-timers --all` | setiap timer yang memicu writer → dihentikan selama jendela |
| I8 | Cron / at | `/etc/crontab`, `/etc/cron.d/*`, `/etc/cron.{hourly,daily,weekly,monthly}/*`, `crontab -l` untuk **semua** user di `/etc/passwd` (termasuk `root`, `postgres`, user deploy), `atq` | setiap entri diklasifikasi (mis. `backup_daily.sh` = pembaca, tetapi jadwal tidak boleh tumpang tindih) |
| I9 | Integrasi eksternal | workflow deploy (GitHub Actions: run yang sedang berjalan / terjadwal), skrip admin operator (`tools/*.py`), agen monitoring dengan kredensial DB, host lain dari I5 | writer / non-writer; deploy **dibekukan** selama jendela (tidak ada run berjalan di awal dan akhir jendela) |
| I10 | Writer non-SQL | proses lokal yang menulis ke direktori data PG (seharusnya tidak ada selain postgres) | tidak ada |

**Pengecualian operator (eksplisit dan terbatas):**
- Sesi yang dikecualikan hanya:
  - (a) sesi manifest operator;
  - (b) backend `pg_dump` yang dijalankan `backup_db.sh`.
- Setiap sesi dikecualikan dicatat dengan `pid`, `usename`, `application_name` (manifest: `trx-backup-manifest-<STAMP>`; dump: `pg_dump`), `client_addr` (socket lokal), dan `backend_start` (≥ awal jendela).
- Syarat selama hidupnya: **`backend_xid IS NULL`** (tidak pernah memperoleh XID tulis) dan transaksi `READ ONLY`.
- Sesi operator lain (psql interaktif, dll.) **tidak** dikecualikan, dan harus tidak ada.

**Pemeriksaan bukti tanpa writer:**

| # | Pemeriksaan | Kapan | Lulus bila |
|---|---|---|---|
| W1 | Inventaris I1–I10 lengkap, setiap item terklasifikasi | sebelum jendela | tidak ada item "tidak dikenal" |
| W2 | `pg_stat_activity`: tidak ada `client backend` selain sesi pengecualian; worker latar hanya tipe yang diizinkan (`autovacuum launcher/worker`, `checkpointer`, `background writer`, `walwriter`, `archiver`, `logical replication launcher` tanpa apply worker) | awal, berkala, akhir | ya |
| W3 | tidak ada backend (selain autovacuum) dengan `backend_xid IS NOT NULL` | awal, berkala, akhir | ya |
| W4 | `pg_prepared_xacts` kosong; tidak ada subscription aktif | awal, akhir | ya |
| W5 | `pg_stat_user_tables`: `n_tup_ins + n_tup_upd + n_tup_del` per tabel sama di awal dan akhir, dan `pg_stat_database.stats_reset` tidak berubah | awal, akhir | ya (statistik kumulatif; pelengkap, bukan bukti tunggal) |
| W6 | Unit/timer/cron writer (I6–I8) dalam keadaan berhenti / tidak terjadwal di jendela; tidak ada run deploy (I9) | awal, akhir | ya |
| W7 | Pemantauan selama `backup_db.sh`: W2 + W3 diulang tiap ≤ 5 detik sampai pg_dump selesai | selama jendela | tidak ada pelanggaran satu pun |
| W8 | Manifest §9.3.2 sebelum == sesudah | akhir | identik |
| W9 (v6) | **Deteksi perubahan inventaris** (rincian di bawah): fingerprint I1/I5/I6–I9 sama di awal, setiap sampel ≤ 5 detik, dan akhir; penghitung sesi kumulatif hanya bertambah sebanyak sesi operator yang tercatat; tidak ada reload konfigurasi; jurnal host tanpa eksekusi writer | awal, berkala, akhir | tidak ada perubahan / kejadian tak terjelaskan |
| W10 (v6) | **Ketersediaan pemantauan:** setiap mekanisme W9 dapat dijalankan di awal, berkala, dan akhir (statistik tidak di-reset, jurnal persisten dan terbaca, API workflow terjangkau, sampel tanpa celah > 5 detik) | awal, berkala, akhir | semua tersedia |

**Deteksi perubahan inventaris selama jendela (v6, R5):**

| Kategori | Mekanisme deteksi (read-only) | Perubahan / kejadian yang membatalkan |
|---|---|---|
| I1 role & privilege | fingerprint sha256 (dihitung server, tanpa nilai rahasia) dari `pg_roles` (semua kolom kecuali `rolpassword`), `pg_auth_members`, `pg_class.relacl`/`relowner`, `pg_namespace.nspacl`/`nspowner`, `pg_default_acl`, `pg_database.datacl`/`datdba`, `pg_policy`; **(v7)** perubahan verifier password role dibandingkan **hanya di dalam DB** (lihat "Verifier password role" di bawah); yang keluar hanya `unchanged`/`changed` | fingerprint berbeda di sampel mana pun, atau verifier `changed` |
| I2 sesi singkat yang lolos sampling | penghitung kumulatif `pg_stat_database.sessions` (PG ≥ 14; minimum proyek 16) untuk **semua** database cluster, dibandingkan awal vs akhir; selisih harus = jumlah sesi operator yang tercatat (manifest, pg_dump, sesi sampling) | selisih tak terjelaskan (termasuk koneksi singkat ke database lain, mis. `postgres`, untuk `ALTER ROLE`) |
| I5 `pg_hba` & konfigurasi | fingerprint `pg_hba_file_rules` + `pg_ident_file_mappings`; `pg_conf_load_time()` dan `pg_postmaster_start_time()` tidak berubah; fingerprint `pg_settings` (nama, nilai, sumber) | berubah, reload (SIGHUP), atau restart |
| I3/I4 | `pg_prepared_xacts` kosong dan `pg_subscription` tidak berubah di setiap sampel | muncul / berubah |
| I6 unit systemd | fingerprint `systemctl list-units --all --plain --no-legend` (nama, load, active, sub) + `systemctl list-unit-files` + sha256 berkas unit writer yang terinventaris; jurnal `journalctl --since <awal> --until <akhir> -o json` untuk kejadian start/stop/reload unit | unit writer berubah status, berkas unit berubah, atau unit baru yang tidak terinventaris muncul |
| I7 timer | fingerprint `systemctl list-timers --all` (unit, NEXT, LAST); jurnal: tidak ada aktivasi timer writer | timer writer terpicu / jadwal berubah |
| I8 cron/at | sha256 setiap berkas `/etc/crontab`, `/etc/cron.d/*`, `/etc/cron.*/*`, `/var/spool/cron/crontabs/*`; daftar `atq`; jurnal unit `cron` (`CRON[...] CMD`) dalam jendela | berkas berubah; entri baru; eksekusi job writer; job tak terklasifikasi berjalan |
| I9 workflow & integrasi eksternal | daftar run GitHub Actions repositori (status `queued`/`in_progress`/`completed` dengan waktu di jendela) di awal dan akhir; skrip admin: tidak ada proses `tools/*.py` / `psql` selain sesi operator (daftar proses host); monitoring eksternal: hanya lewat I2 (sesi) | run deploy berjalan/dimulai di jendela; proses admin lain; sesi eksternal (I2) |
| I10 | daftar proses host yang membuka berkas di direktori data PG selain `postgres` | ada |

**Verifier password role (v7, T3):**
- **Masalah v6:** "hanya digest yang keluar" tetap mengekspor digest turunan `pg_authid.rolpassword` ke operator/manifest. Itu tidak diperlukan, dan memperluas jejak data verifier.
- **Mekanisme:**
  1. Satu **sesi monitor** khusus (superuser `postgres` lewat socket lokal, `application_name = trx-backup-monitor-<STAMP>`, tercatat di daftar pengecualian dan penghitung sesi W9) dibuka di awal jendela dan dipertahankan sampai akhir.
  2. Awal, di transaksi `READ ONLY`:
     ```sql
     SELECT pg_catalog.set_config('trx.authid_fp0',
            (SELECT pg_catalog.encode(pg_catalog.sha256(<framing STR(rolname) ‖ TLV_VALUE(rolpassword) ‖ TLV_VALUE(rolvaliduntil::text)
                    untuk setiap role, ORDER BY rolname COLLATE "C">), 'hex')
             FROM pg_catalog.pg_authid), false) IS NOT NULL AS stored;
     ```
     Hasil yang dikirim ke klien hanya boolean `stored`. Nilai digest disimpan di parameter sesi kustom (`set_config`, `is_local=false`) yang hanya hidup di memori backend sesi itu: tidak menulis katalog, tidak memperoleh XID, dan tidak terlihat sesi lain.
  3. Setiap sampel W7 dan pemeriksaan akhir, di sesi monitor yang **sama**:
     ```sql
     SELECT CASE WHEN pg_catalog.current_setting('trx.authid_fp0') = (<ekspresi digest yang sama>)
                 THEN 'unchanged' ELSE 'changed' END AS verifier_status;
     ```
     Yang keluar hanya `unchanged`/`changed`. Status per role **tidak** dikeluarkan.
- **Tidak ada yang diekspor:** digest verifier tidak masuk manifest, laporan, log operator, atau keluaran klien. Ekspresi digest dihitung di dalam query, sehingga teks statement yang mungkin tercatat di log server (`log_statement`) tidak memuat digest.
- **Tidak ada perubahan DB:** tanpa tabel (termasuk temp table, yang akan menulis katalog dan memberi XID), tanpa fungsi baru, tanpa `ALTER ROLE`, tanpa perubahan password. Parameter sesi hilang saat sesi monitor ditutup.
- **Fail-closed:**
  - sesi monitor putus / tersambung ulang (nilai awal hilang);
  - `current_setting` gagal;
  - `pg_authid` tidak terbaca;
  - status `changed`.

  Masing-masing → backup **tidak sah** (W9/W10).
- Pemeriksaan lain I1 tetap: semua role (`pg_roles`), keanggotaan, privilege/ACL tabel, skema, database, default ACL, policy RLS, dan kepemilikan. Fingerprint I1 non-rahasia tetap dihitung dan dibandingkan seperti v6.

**Tes verifier (v7; hanya di PostgreSQL ephemeral):**

| ID | Skenario | Lulus bila |
|---|---|---|
| PW-01 | seluruh keluaran sesi monitor (hasil query, NOTICE, error) dan manifest | tidak memuat digest/hex turunan `rolpassword`; hanya `stored`/`unchanged`/`changed` |
| PW-02 | tanpa perubahan selama jendela | `unchanged` di semua sampel |
| PW-03 | `ALTER ROLE ... PASSWORD` di DB ephemeral (sesi lain) di tengah jendela | sampel berikutnya `changed`; backup tidak sah |
| PW-04 | `ALTER ROLE ... VALID UNTIL` berubah | `changed` |
| PW-05 | sesi monitor diputus lalu tersambung ulang | perbandingan tidak mungkin → monitoring tidak tersedia → tidak sah |
| PW-06 | `backend_xid` sesi monitor selalu NULL; `pg_class` tidak bertambah (tanpa temp table) | ya |
| PW-07 | `log_statement = 'all'` di DB ephemeral: log server | tidak memuat digest verifier |

**Ketentuan:**
- Fingerprint **inventaris awal** (W1) disimpan. Setiap sampel W7 dan pemeriksaan akhir membandingkan dengan fingerprint itu. Inventaris dianggap **tetap berlaku** hanya bila semua fingerprint identik dari awal sampai akhir.
- Jendela yang lebih pendek dari interval sampling tetap tercakup oleh mekanisme kumulatif: penghitung `sessions`, `pg_conf_load_time`, jurnal host, dan riwayat run workflow.
- **Mekanisme tidak tersedia = tidak sah (W10).** Contoh:
  - `pg_stat_database.stats_reset` berubah atau `sessions` tidak tersedia;
  - jurnal tidak persisten atau tidak terbaca di jendela;
  - API workflow tidak terjangkau;
  - daftar cron user tidak terbaca;
  - ada celah sampling > 5 detik.

  Setiap contoh di atas membuat backup **tidak sah**; tidak ada pengecualian manual.
- Semua mekanisme read-only. Tidak ada `ALTER SYSTEM`, perubahan `log_*`, `ALTER ROLE`, atau perubahan berkas host. Bila pemantauan hanya mungkin dengan mengubah konfigurasi, perubahan itu di luar G2 dan butuh persetujuan terpisah; sampai itu dilakukan, M2 tidak sah dan M1 harus dipakai.

Hasil:
- **(v6)** Satu pelanggaran W1–W10 → backup **tidak sah** (W9/W10 menambah, tidak menggantikan W1–W8).
- **Satu** pelanggaran W1–W8 → backup **tidak sah**, ulangi S1 setelah sumber writer ditangani. API tetap boleh dinyalakan kembali, tetapi backup tidak dipakai.
- Laporan memuat tabel inventaris, daftar sesi pengecualian, seluruh sampel W2/W3 (tanpa teks query), dan waktu UTC setiap pemeriksaan.

**Larangan:**
- Tidak menjalankan S4 atau S9 tanpa persetujuan langkah sebelumnya.
- Tidak menjalankan 009 di produksi dalam rangkaian ini.
- Tidak menulis data uji ke DB staging (S5 read-only).
- Tidak mengubah `deploy/*.sh`, workflow CI, atau VPS dari implementasi G2. Langkah S0–S9 dilakukan operator di luar G2.

---

## 10. Rollback dan recovery

| Situasi | Tindakan |
|---|---|
| G2 perlu dibatalkan sebelum deploy | `git revert` commit G2 (per langkah G2.x bila dipisah). Tidak ada data yang bergantung, karena tabel 009 kosong |
| Migrasi 009 perlu dibatalkan | `009_rpc_migrations.down.sql`, hanya bila tidak ada baris non-final (ditolak otomatis bila ada). Di G2 tabel selalu kosong |
| Startup guard menolak boot produksi | Perbaiki konfigurasi (secret identik di `.env` API & `provisioner.json`, `production=true`, `strict`). **Tidak** ada bypass env, dan tidak ada penurunan ke `migration` di produksi |
| Sync berhenti untuk terminal classic setelah enforcement aktif | Perilaku yang dimaksud. Pulihkan dengan migrasi G3. Sebelum G3: jangan deploy `strict`/`migration` ke produksi (R1). Rollback **ke classic tidak disediakan** |
| Runtime menolak karena `migration_owner_orphan` | Ditangani admin lewat pemulihan G3 (takeover/blocked). G2 tidak menyediakan jalur pelepasan `migrate:*` |
| Lock akun macet (thread provider hang) | Provider memakai timeout (`sync_request_timeout`); `evict` menandai stale. Restart proses melepas semua lock in-memory; job/migrasi DB tetap konsisten (pemulihan DB-based) |
| (v2) Default helper `ro` perlu dibatalkan | `git revert` perubahan G2.0 saja. **Tidak** dengan mengembalikan default classic lewat konfigurasi: tidak ada knob konfigurasi untuk itu. Pemanggil yang butuh classic non-produksi wajib eksplisit |
| (v2) Guard menolak karena hash helper berubah setelah update paket/OS | Verifikasi berkas helper baru (review + sha256) → perbarui `PROVISIONER_HELPER_SHA256` → restart. Tidak ada bypass |
| (v2) Gerbang staging §9.2 gagal setelah enforcement `migration` aktif | Kembalikan staging ke `MT5_RPC_ENFORCEMENT=off` (non-produksi saja); terminal ro tetap ro; tidak ada terminal yang dikembalikan ke classic |
| (v2) Down 009 | Hanya di lingkungan tes/ephemeral selama G2. Penerapan dan rollback di staging/produksi = langkah deploy terpisah dengan persetujuan |
| (v3) Gagal di S0–S9 staging | Ikuti kolom "Gagal →" §9.3. Backup S1/S2 adalah titik pemulihan DB; down 009 hanya dengan persetujuan dan tabel tanpa baris non-final |
| (v4) Manifest S1 tidak konsisten (M2: sebelum ≠ sesudah; M1: sesi snapshot putus) | Backup dianggap tidak sah; ulangi S1 (M2 dengan penulis benar-benar dihentikan). Tidak lanjut ke S2/S3 |
| (v5/v6) Inventaris writer tidak lengkap / pelanggaran W1–W10 / fingerprint isi berbeda / versi PG berbeda / capability probe gagal | Backup tidak sah; berhenti di S1/S2. Tangani sumbernya (writer tak terinventaris, subscription, cron), lalu ulangi S1. Tidak ada pelonggaran ke hitungan baris saja |
| (v3) `ipv6_state=unproven` di staging | Readiness tetap merah. Perbaiki bukti (deklarasi `ipv6_expected` + kernel cmdline, atau aktifkan IPv6 dan audit tcp6). **Tidak** ada bypass, dan tidak ada pelonggaran ke "tcp6 opsional" |

Prinsip:
- Tidak ada rollback yang mengembalikan terminal ke classic.
- Tidak ada rollback yang menghapus kredensial.
- Rollback kode tidak memerlukan perubahan data.

---

## 11. Keputusan yang perlu persetujuan

| # | Keputusan | Rekomendasi |
|---|---|---|
| D1 | Migrasi skema 009 (`mt5_rpc_migrations`) masuk G2 | **Ya**. Gerbang membutuhkannya untuk fail-closed dan kontrak G3 tetap. Hanya menambah tabel; diterapkan di lingkungan tes saja sampai deploy disetujui |
| D2 | Default enforcement non-produksi `real` = `migration` (classic diblokir) | **Diputuskan (review v2): ya**, tetapi hanya setelah gerbang staging §9.2 lulus. Sebelumnya staging memakai `off` eksplisit |
| D3 | Unbind terminal classic (bukan migrasi) di enforcement ≠ off tetap diizinkan | **Ya**. Unbind adalah keputusan customer; tidak ada migrasi berjalan sehingga kredensial boleh dihapus sesuai produk #2 |
| D4 | Default helper non-produksi (`--rpc-mode` kosong) | **Diputuskan (review v2): `ro`**. Rekomendasi v1 ("tidak diubah") dibatalkan. Tes lama diperbarui sesuai aturan I-01; tes CLI tanpa `--rpc-mode` ditambahkan (CLI-01…07) |
| D6 (v2) | `PROVISIONER_HELPER_SHA256` wajib di produksi | **Ya**. Tanpa pin, SG-e tidak bisa membedakan helper sah dari salinan lain yang ditulis root |
| D7 (v2) | Format bukti selfcheck v3 (terikat `helper_sha256`) menggantikan v2 | **Ya**. Butuh deploy helper dan API bersamaan (R11) |
| D5 | Pembaruan `docs/RPYC-SECURITY.md` §6 di G2 | **Ya** (dokumentasi saja), atau tunda ke G3 bersama runbook migrasi |

---

## 12. Yang belum diverifikasi di audit ini

- **Dampak default `ro` pada tes lama:** jumlah tes yang patah **NOT MEASURED**, karena audit read-only tidak menjalankan tes dengan konfigurasi baru.
- **Kesimpulan E-03 bahwa pemulihan tidak menyentuh `migrate:*`:** berasal dari penelusuran SQL, belum dibuktikan tes (RC-01…05 direncanakan).
- **Pesan `last_error` nyata `ro_service` dari terminal Wine/MT5:** apakah ia mengandung marker auth broker? **NOT VERIFIED**, karena tidak ada terminal nyata di sandbox.
- **Header `/proc/net/tcp6` di host nyata:** tetap NOT VERIFIED (G1).
- **Perilaku RLS 009 di PostgreSQL produksi:** NOT VERIFIED. Akan diuji dengan pgserver lokal memakai login role sebenarnya (MIG-04…MIG-16).
- **(v2) Jumlah tes helper yang gagal karena default `ro`:** NOT MEASURED. Hanya estimasi grep: ±31 `op_install(` dan ≤34 baris argv `install`.
- **(v2) Kemampuan helper menghitung hash berkasnya sendiri saat dijalankan lewat sudo:** NOT VERIFIED di host nyata. Pemeriksaan pemilik root dan izin direktori SG-c tidak dapat dibuktikan di sandbox root; tes memakai `lstat` palsu, sama seperti tes izin G1.
- **(v2) Gerbang staging §9.2:** belum dijalankan. Membutuhkan staging dengan Wine/MT5 dan akun uji password investor.
- **(v3) Status IPv6 host staging:** NOT VERIFIED; sengaja tidak diasumsikan. Sandbox audit sendiri tidak memiliki `/proc/net/tcp6`; isi `cmdline` dan `if_inet6` di sandbox tidak dipakai sebagai bukti.
- **(v3) Perilaku `ipv6.disable=1` di kernel staging** (E1–E3 benar-benar absen): sesuai dokumentasi kernel, tetapi NOT VERIFIED pada host nyata.
- **(v4) Hak role operator untuk `pg_export_snapshot()` dan `pg_dump --snapshot` di staging:** NOT VERIFIED.
- **(v5) Perilaku probe `socket(AF_INET6)` → `EAFNOSUPPORT` pada kernel staging dengan `ipv6.disable=1`:** sesuai perilaku kernel yang terdokumentasi, tetapi NOT VERIFIED pada host nyata. Tes memakai probe yang diinjeksi.
- **(v5) Waktu eksekusi fingerprint isi pada ukuran data staging:** NOT MEASURED.
- **(v5) Versi major PostgreSQL staging dan cluster restore:** NOT VERIFIED; wajib sama (§9.3.2).
- **(v6) Versi PostgreSQL tempat `sha256(bytea)` pertama kali tersedia:** NOT VERIFIED dari sesi ini. `www.postgresql.org` tidak dapat dijangkau (host di luar allowlist jaringan sesi). Spesifikasi tidak bergantung padanya: minimum = 16 + capability probe.
- **(v6) Nilai inode namespace awal (`4026531836`, `4026531837`) pada kernel staging:** NOT VERIFIED (konstanta ABI kernel; diuji dengan symlink palsu).
- **(v6) Ketersediaan jurnal persisten, `pg_stat_database.sessions`, dan akses API workflow di staging:** NOT VERIFIED. Bila tidak tersedia, M2 tidak sah (W10).
- **(v7) Hasil KV-1…KV-5 di staging:** belum dijalankan. Ketersediaan `systemd-detect-virt` dan `lsns` di host staging NOT VERIFIED; bila tidak tersedia, KV tidak lulus dan `namespace_identity` tetap `unproven`.
- **(v7) Biaya sort/hash `GROUP BY (rh, row_bytes)` pada tabel tanpa PK berukuran staging:** NOT MEASURED. Di skema saat ini, tabel tanpa PK diperkirakan sedikit; daftar pastinya dihasilkan generator dari katalog.
- **(v5) Kelengkapan inventaris writer staging (I1–I10):** belum disusun; hanya dapat dilakukan di host staging oleh operator.

---

## 13. Daftar perubahan v1 → v2

| # | Koreksi review | Bagian | Perubahan |
|---|---|---|---|
| 1 | **D4** | §2.9 X11, §2.10 I-01 (baru), §4 baris 17 & 20, §5 G2.0 (baru), §7 R2/R10, §8 CLI-01…07, §9.1 #11, §10, §11 D4, §12 | Default helper non-produksi `--rpc-mode` kosong → `ro` (sebelumnya classic). `op_install(rpc_mode="ro")`. Helper tidak ro-capable → exit 4, tidak pernah classic. Aturan pembaruan tes lama (eksplisit classic atau konfigurasi ro-capable, tanpa skip/hapus); `test_h5_non_produksi_default_lama_classic` diganti. Rekomendasi v1 "tidak diubah" dibatalkan |
| 2 | **D2** | §7 R1, §9.2 (baru), §10, §11 D2, §12 | Enforcement `migration` di staging dipertahankan, tetapi diaktifkan hanya setelah gerbang ST-1…ST-10 lulus dan disetujui. Gerbang memuat bukti koneksi password investor (`trade_allowed is False`, identitas cocok), penolakan password master, sync nyata, tidak ada jalur order, probe token, `audit-all` lengkap, dan laporan tanpa secret. Sebelum lulus: `off` eksplisit + peringatan startup |
| 3 | **Account lock** | §3.4 (`session`), §3.5 (baru: objek lock, hierarki L0–L4, urutan per jalur, semantik `evict`, invarian provider), §7 R12, §8 CC-04…CC-13, §9.1 #12 | Urutan lock tegas untuk sync, verification (termasuk timeout), status, health non-deep/deep, scheduler, dan worker G3. `evict` tidak pernah mengganti lock; tidak memutus provider yang sedang dipakai (tandai `stale`, diputus pemegang). Provider hanya lewat `session()`; pemakaian tanpa lock → `RuntimeError`. Tes concurrency, stres, dan disconnect |
| 4 | **Migration 009** | §3.3.1 (baru: matriks hak akses), §3.3.2 (baru: aturan uji), §4 baris 21, §5 G2.2, §8 MIG-01…MIG-16, §9.1 #13, §10, §12 | Uji RLS dengan **login** `transcendence_app`/`transcendence_system`/migrator non-superuser yang sebenarnya (bukan superuser / `SET ROLE`); assert `rolsuper`/`rolbypassrls` false, FORCE RLS, owner. Indeks unik parsial (6 skenario), CHECK state = `MIGRATION_STATES`, privilege per role (DELETE ditolak untuk semua role runtime), down dengan/tanpa baris non-final + terapkan ulang. Fixture menolak DSN di luar cluster ephemeral. Larangan eksplisit menjalankan 009 di DB produksi/staging |
| 5 | **Startup guard** | §2.10 I-02 (baru), §3.6 (baru), §4 baris 1, 12, 17–19, §5 G2.7, §7 R11, §8 SG-07…SG-16 + SC-PAR, §9.1 #14, §10, §11 D6/D7 | Guard memakai **objek provisioner yang sama** dengan runtime. API memverifikasi berkas helper (path absolut non-symlink, root-owned, tidak writable, sha256, pin `PROVISIONER_HELPER_SHA256` di produksi). Helper melaporkan `helper_path`/`helper_sha256`/`euid`; bukti `selfcheck-v3` terikat `helper_sha256` hasil hitung API. Urutan startup: guard → `migrate` → pemulihan → scheduler → HTTP; gagal → exit 78 tanpa thread/socket (dibuktikan SG-07) |
| — | Umum | header, §1 | Status v2, tabel riwayat revisi. **Semua 40 temuan v1 (A-01…H-04) dan X1–X13 dipertahankan tanpa dihapus**; temuan baru ditambahkan sebagai I-01 dan I-02 (keduanya kritis) |
| — | Koreksi editorial | §1, §2, §4 baris 15 | Rujukan bagian diperbaiki: kontrak G3 ada di §3.4 (v1 menulis "4.4"); ID tes ada di bagian 8 (v1 menulis "bagian 9"). Ringkasan v1 di chat menyebut "37 temuan"; hitungan yang benar adalah **40** (18 kritis) |

---

## 14. Daftar perubahan v2 → v3

| # | Koreksi review | Bagian | Perubahan |
|---|---|---|---|
| 1 | **K1 Account lock** | header, §3.5.1, §3.5.6 (baru), §7 R4, §8 CC-14…CC-20, §9.1 #12 | Pengecualian v2 "lock dihapus bila akun dihapus" **dihapus**. Lock tidak pernah dihapus atau diganti selama proses hidup, termasuk saat penghapusan akun/customer; hanya hilang saat restart. Pembuatan lock atomik (`setdefault` di bawah L3); `account_lock` tidak membaca DB. Perilaku `session()` setelah akun dihapus, ID SQLite dipakai ulang, batas memori + `account_locks_count`. Tes race tiga arah `account_lock()`/`evict()`/hapus akun dan tes statis tidak ada penghapusan lock |
| 2 | **K2 TCP6** | header, §2.10 I-03 (baru), §3.7 (baru), §4 baris 17–18, §7 (rollback), §8 TCP6-01…TCP6-14, §9.1 #15, §9.2 ST-3, §10, §12 | Fail-closed dipertahankan. Deklarasi `ipv6_expected` (kunci hilang = `enabled`) + bukti kernel E1–E4. Keputusan `enabled` / `disabled_verified` / `unproven`: tcp6 ada → wajib diaudit (termasuk sysctl `disable_ipv6`); tcp6 absen diterima **hanya** dengan deklarasi `disabled` + token `ipv6.disable=1` persis + tanpa if_inet6/sys/net/ipv6; selain itu merah. Readiness mensyaratkan `ipv6_state` terbukti. Status IPv6 staging tidak diasumsikan |
| 3 | **K3 Staging 009** | header, §9.2, §9.3 (baru), §8 SG-17, §9.1 #14/#16, §10 | Urutan staging eksplisit S0–S9: prasyarat → backup → verifikasi restore → persetujuan 009 → penerapan oleh migrator non-superuser (API dihentikan) → verifikasi RLS/privilege/indeks read-only dengan login role sebenarnya → deploy helper+API selfcheck v3 (`off` eksplisit) → ST-1…ST-10 → persetujuan → `migration`. Kolom kegagalan per langkah. Penegasan: kode G2 tidak pernah menerapkan migrasi otomatis (SG-17) |
| — | Umum | header | Status v3, tabel riwayat. **Semua 40 temuan v1 (A-01…H-04), X1–X13, serta I-01/I-02 dipertahankan**; temuan baru I-03 (kritis) |

---

## 15. Daftar perubahan v3 → v4

| # | Penajaman | Bagian | Perubahan |
|---|---|---|---|
| 1 | **P1 Account lock** | header, §3.4, §3.5.1, §3.5.6, §3.5.7 (baru), §7 R4/R13, §8 LK-01…LK-09, §9.1 #12 | Tabel asal `account_id` per jalur: tidak pernah dari body/URL/header, hanya dari baris DB di bawah token + RLS, verifikasi password, job, atau koneksi sistem. `ensure_account_lock` membuat lock **hanya** untuk akun yang terbukti ada (`account_missing`) dan dimiliki customer pemanggil (`account_owner_mismatch`); ID tidak sah ditolak tanpa query. Batas keras `ACCOUNT_LOCKS_MAX`, fail-closed (`lock_registry_full`) tanpa menghapus lock. K1 dipertahankan: lock yang sudah ada tidak pernah dihapus/diganti; restart satu-satunya pembersihan. Tes ID tidak ada, 10 000 ID acak, kepemilikan, batas, race hapus akun, statis AST asal ID |
| 2 | **P2 TCP6** | header, §3.7.2, §3.7.3 catatan, §8 TCP6-15…TCP6-20, §9.1 #15, §12 | Tabel path absolut: E1 `/proc/net/tcp6`, E2 `/proc/net/if_inet6`, E3 `/proc/sys/net/ipv6`, E4 `/proc/cmdline`, E5 baru (informatif) `/proc/sys/net/ipv6/conf/{all,default}/disable_ipv6`. `/proc/sys` (sysctl procfs) dibedakan tegas dari `/sys` (sysfs), yang tidak pernah dibaca. E5 tidak mengubah `ipv6_state`, tetapi bila tidak terbaca → merah. Tes empat skenario (aktif, sysctl disabled, boot disabled, bukti tidak lengkap 7 varian), sysfs palsu tidak berpengaruh, statis tanpa akses `/sys`. Fail-closed dipertahankan |
| 3 | **P3 Backup** | header, §9.3 S1/S2, §9.3.1 (baru), §10, §12 | Hitungan pembanding harus dari snapshot yang sama dengan backup: M1 `pg_export_snapshot()` + `pg_dump --snapshot`, atau M2 (default, `backup_db.sh` tidak diubah) hentikan semua penulis + bukti `pg_stat_activity` + manifest sebelum == sesudah. Manifest per tabel (count + sidik kunci primer, tanpa data sensitif). Dicatat: identitas snapshot (id/`txid_current_snapshot`/LSN), waktu UTC tiap tahap, bukti restore (manifest identik, `schema_migrations`, drop). Restore ke cluster terpisah. DB aktif tidak diubah (hanya `SELECT` READ ONLY; M2 menghentikan layanan, bukan mengubah DB) |
| — | Umum | header | Status v4. **Semua 40 temuan v1, X1–X13, dan I-01…I-03 dipertahankan**; tidak ada temuan dihapus |

---

## 16. Daftar perubahan v4 → v5

| # | Penajaman | Bagian | Perubahan |
|---|---|---|---|
| 1 | **Q1 Bukti IPv6 disabled** | header, §3.7.2 (E6–E9), §3.7.2a (baru), §3.7.3 baris 3–4, §3.7.4, §8 TCP6-21…TCP6-30, §9.1 #15, §12 | Token `ipv6.disable=1` tidak lagi bukti tunggal. `disabled_verified` mensyaratkan V1–V6: token tunggal tanpa duplikat/penimpaan; `/proc/net/{tcp6,if_inet6,ipv6_route,snmp6}` tidak ada; `/proc/sys/net/ipv6` tidak ada; probe `socket(AF_INET6)` gagal **persis** `EAFNOSUPPORT`; netns helper = netns init; deklarasi `disabled`. Tidak terbukti → `unproven` + kode bukti → merah. Sysfs **tidak** dipakai; `/sys/module/ipv6/parameters/disable` dicatat sebagai opsi yang butuh review cakupan terpisah. Audit tcp6 tetap wajib bila berkas ada |
| 2 | **Q2 Integritas isi backup** | header, §9.3.1 (manifest v4 digantikan), §9.3.2 (baru), §10, §12 | Fingerprint isi seluruh baris: cakupan tabel dari katalog (fail-closed untuk tipe tak didukung), parameter sesi tetap, serialisasi TLV bertipe (NULL, teks, integer, numeric dengan skala, float `extra_float_digits=3`, boolean, date/timestamp/timestamptz UTC, json/jsonb, bytea, uuid). Kolom sensitif (daftar eksplisit + pola default) di-hash di server dengan kunci per-run `K` (tidak disimpan; `key_fp` saja). Hash baris → chunk N=10 000 (agregat ≤ 320 KB, keyset pagination, dua tingkat untuk tabel sangat besar) → tabel → manifest. Ditambah sequence dan fingerprint skema. Sumber vs restore wajib identik; selisih dilokalisasi per chunk tanpa nilai. Keterbatasan: collision SHA-256 ≈ 2⁻¹²⁸, bukan HMAC formal, versi PG harus sama, large objects/OID tidak dicakup, biaya pemindaian penuh. Koreksi nama tabel v4 (`mt5_sync_state`, `mt5_trades`) |
| 3 | **Q3 Inventaris writer M2** | header, §9.3.1 langkah M2, §9.3.3 (baru), §10, §12 | Inventaris I1–I10: semua role (hak tulis, superuser, `pg_write_all_data`), semua sesi, transaksi prepared, replikasi logis, aturan `pg_hba`, unit systemd, timer, cron/at semua user, integrasi eksternal (deploy workflow, skrip admin, monitoring), writer non-SQL. Pengecualian hanya sesi manifest dan backend `pg_dump`, diidentifikasi per pid dengan syarat `backend_xid IS NULL` + READ ONLY. Pemeriksaan W1–W8 (awal, berkala ≤ 5 s, akhir), termasuk statistik tuple dan manifest sebelum == sesudah. Inventaris tidak lengkap atau satu pelanggaran → backup tidak sah, berhenti. Semua read-only (tanpa `ALTER ROLE`/`pg_hba`) |
| — | Umum | header | Status v5. **Seluruh desain v1–v4, 40 temuan v1, X1–X13, I-01…I-03, dan keputusan D1–D7 dipertahankan**; tidak ada temuan dihapus |

---

## 17. Daftar perubahan v5 → v6

| # | Koreksi | Bagian | Perubahan |
|---|---|---|---|
| 1 | **R1 Network namespace** | header, §3.7.2 (E9), §3.7.2a (V5 + catatan presisi), §3.7.2b (baru), §8 TCP6-31…TCP6-37, §9.1 #15, §12 | Dijelaskan bahwa `/proc/1/ns/net` = netns PID 1 di PID namespace procfs yang terlihat helper, bukan otomatis host. Asumsi A1–A4 (helper di host, procfs milik namespace helper, unit tanpa direktif netns, kernel ≥ 3.8). V5 kini mensyaratkan N1 PID ns awal (`pid:[4026531836]`), N2 user ns awal (`user:[4026531837]`), N3 `/proc/self` == pid helper, N4 netns self == netns PID 1, N5 template unit tanpa `PrivateNetwork=` dan sejenisnya. Gagal → `unproven`, merah. Identitas namespace juga dicatat untuk seluruh audit-all (`namespace:tidak_terbukti` → `audit_complete=false`). V1–V6 dipertahankan |
| 2 | **R2 SHA-256 PostgreSQL** | header, §9.3.2 (kunci per-run), capability probe (baru), perbandingan, keterbatasan, §12 | Klaim "PG ≥ 11" dicabut sebagai dasar. Minimum = PostgreSQL 16 (versi yang didukung/diuji proyek), sumber dan restore sama major. Capability probe CP-1…CP-8: versi, `search_path = pg_catalog`, fungsi `pg_catalog.sha256(bytea)` tepat satu, KAT standar (kosong, `abc`, 448-bit, 1 MiB), fungsi framing tersedia. Gagal → generator berhenti, backup tidak sah, tanpa fallback dan tanpa memasang ekstensi. Versi awal `sha256` di dokumentasi dicatat NOT VERIFIED (dokumentasi tidak terjangkau) |
| 3 | **R3 Urutan tanpa PK** | §9.3.2 cakupan, algoritma langkah 3 & 7, §9.3.2a (`TABLE` memuat mode urutan), MF-03…MF-05 | Satu aturan kanonik: tabel tanpa PK → `ORDER BY rh` (bytewise). Aturan v5 "ORDER BY seluruh kolom" dihapus karena bertentangan dengan langkah algoritma. PK teks `COLLATE "C"`. Keyset tanpa PK + penanganan duplikat di batas chunk. Mode urutan dicatat dan wajib sama |
| 4 | **R4 Framing digest** | §9.3.2 (TLV nilai), §9.3.2a (baru), manifest versi, MF-01/02/09/10 | Format byte eksplisit `trx-manifest-v2`: `u8`, `u32be` (`int4send`), `u64be` (`int8send`), `STR` length-prefix untuk semua teks, label domain per jenis digest, `K` 32 byte tetap. Definisi `SENS`, `ROW`, `CHUNK`, `SUPER`, `COLSPEC`, `TABLE` (nama, mode urutan, kolom, jumlah baris, N, level, jumlah komponen), `PARAMS`, `SEQS`, `MANIFEST`, `key_fp`. Vektor emas Python vs SQL. Manifest v1 tidak sebanding dengan v2 |
| 5 | **R5 Perubahan inventaris writer** | §9.3.3 (W9, W10, tabel deteksi per kategori, ketentuan, hasil), §10, §12 | Deteksi perubahan selama jendela: fingerprint role/privilege/ACL/policy (+ digest internal password role), penghitung kumulatif `pg_stat_database.sessions` seluruh cluster, `pg_hba_file_rules`/`pg_ident_file_mappings`/`pg_settings` + `pg_conf_load_time()`/`pg_postmaster_start_time()`, fingerprint unit/timer/cron/at + jurnal host, run workflow di awal & akhir, proses admin host. Mekanisme tidak tersedia → tidak sah (W10). W1–W8 dan I1–I10 dipertahankan |
| — | Umum | header | Status v6. **40 temuan v1 (A-01…H-04), I-01…I-03, X1–X13, D1–D7, dan desain v1–v5 dipertahankan**. Satu-satunya isi lama yang diganti: (a) aturan urutan tanpa PK v5 yang bertentangan (R3), (b) klaim versi "PG ≥ 11" (R2), (c) notasi ASCII TLV v5 dan formula digest v5 yang diganti format byte v2 (R4). Ketiganya diganti dengan yang lebih ketat |

---

## 18. Daftar perubahan v6 → v7

| # | Koreksi | Bagian | Perubahan |
|---|---|---|---|
| 1 | **T1 Pembuktian namespace** | header, §3.7.2b (N6, prosedur KV-1…KV-5, keterbatasan), §8 TCP6-38…TCP6-42, §9.1 #15, §12 | Inode PID/user namespace awal tidak lagi dianggap terverifikasi otomatis. KV-1 host bukan container; KV-2 nilai init host + `lsns`; KV-3 helper lewat jalur sudo nyata dari user layanan API; KV-4 jalur dan hash helper = runtime; KV-5 `osrelease` dicatat dan `ns_verified_osrelease` ditulis setelah persetujuan. N6 mengikat bukti ke kernel yang berjalan. Tidak ada knob untuk mengganti konstanta. Tidak cocok / kernel berubah / tidak terbukti → `unproven`, merah. N1–N5 dan fail-closed v6 dipertahankan; V5 tidak dilonggarkan |
| 2 | **T2 Determinisme tabel tanpa PK** | header, §9.3.2 (urutan kanonik, langkah 3 & 7), §9.3.2a (`CHUNK_G`, `TABLE`, `PARAMS`, versi format), MF-04 (diperbarui), MF-11…MF-16 | Dievaluasi: `ORDER BY rh`, `row_number() OVER (ORDER BY rh)`, dan keyset `rh > :last_rh` tidak total / dapat membelah kelompok, dan aturan duplikat v6 membuat chunk tak berbatas. Pengganti: kelompok `(rh, row_bytes)` + `count`, urutan `ORDER BY rh, row_bytes` (bytewise), chunk ≤ N kelompok, entri `rh ‖ u64be(c)`, satu query di snapshot sama (spill ke disk), `row_bytes` tidak keluar server. Baris identik → digest sama; multiplisitas ikut di-hash; collision `rh` dibedakan `row_bytes`. `generator_sql_sha256` di `PARAMS`. Format naik `trx-manifest-v3` |
| 3 | **T3 Verifier password role** | header, §9.3.3 (I1, bagian "Verifier password role", PW-01…PW-07), §12 | Digest `pg_authid.rolpassword` tidak diekspor. Nilai awal disimpan via `set_config` di memori sesi monitor khusus (tanpa tabel/temp table, tanpa XID); setiap sampel hanya mengeluarkan `unchanged`/`changed`, tanpa status per role. Sesi monitor putus atau `changed` → tidak sah. Pemeriksaan semua role, privilege, ACL, policy, dan perubahan password dipertahankan. Tanpa `ALTER ROLE` atau perubahan password |
| — | Umum | header | Status v7. **40 temuan v1 (A-01…H-04), I-01…I-03, X1–X13, D1–D7, dan koreksi v1–v6 dipertahankan**. Isi lama yang diganti hanya bagian yang bertentangan dengan T1–T3: urutan `ORDER BY rh` + keyset `rh` v6 (T2), versi format `v2` → `v3` (T2), dan kalimat "hanya digest yang keluar" untuk `rolpassword` (T3). Semuanya diganti dengan aturan yang lebih ketat |

