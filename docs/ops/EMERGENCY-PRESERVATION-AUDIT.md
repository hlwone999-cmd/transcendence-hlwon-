# Emergency Preservation — Transcendence MT5 Journal (Fase 1, READ-ONLY)

Tanggal: 2026-11-16 · Status: **audit belum dapat dijalankan langsung di VPS**

## 0. Batas akses (fakta)

Sesi ini **tidak dapat menjangkau VPS** `<VPS_IP>` (SSH port <SSH_PORT>):
- tidak ada klien `ssh`/`scp` di container;
- koneksi TCP langsung ditolak (`Connection refused`);
- jaringan sesi hanya HTTPS lewat proxy, dan allowlist tidak menerima alamat IP.

Akibatnya:
- **Setiap fakta tentang server di bawah ini NOT VERIFIED** sampai keluaran skrip audit tersedia.
- Yang tertulis di laporan ini berasal dari **repo** (commit `273c774`): kode, skrip deploy, workflow, dan dokumentasi. Itu menggambarkan apa yang *seharusnya* ada di VPS, bukan apa yang *ada*.

Alat yang disiapkan:
- **`emergency_preservation_audit.sh`** (read-only, keluaran ke stdout saja), dijalankan di VPS sebagai root.
- Sudah diuji sintaks dan dijalankan di sandbox tanpa galat.
- Masking secret diuji: `.env` → hanya nama kunci; DSN, `KEY=`, `--password` → `***`.
- Pemindaian statis memastikan skrip tidak memuat operasi tulis (`rm`, `mv`, `chmod`, `systemctl start/stop/restart`, DDL/DML, `pg_dump`).

## A. Kondisi server — NOT VERIFIED

| Item | Dari repo / riwayat percakapan | Status |
|---|---|---|
| OS | Ubuntu 26.04.1 (disebut pengguna sebelumnya) | NOT VERIFIED |
| Akses | SSH port <SSH_PORT>; user `deploy` dipakai workflow (`backend-deploy.yml:218`) | NOT VERIFIED |
| CPU/RAM/swap/disk/inode/timezone | tidak diketahui | NOT VERIFIED — skrip §1 |

## B. Inventaris data dan service (yang diharapkan dari repo)

| Komponen | Lokasi yang diharapkan | Sumber repo | Wajib diselamatkan |
|---|---|---|---|
| Backend API + scheduler + worker job (satu proses `run.py`, waitress) | `/opt/transcendence-hlwon-/backend`, venv `.venv`, unit `transcendence-backend.service` | `backend-deploy.yml:117-122`, `docs/MULTI-CUSTOMER-INSTALASI-PROVISIONING.md:218-221` | kode = git (commit); **perubahan lokal belum di-commit** dicek skrip §4 |
| Konfigurasi backend | `/opt/transcendence-hlwon-/backend/.env`, kemungkinan `/etc/default/transcendence-backend` | `backend-deploy.yml:10,122` | **YA (kritis)** |
| PostgreSQL | DB `transcendence` (+ `transcendence_restore` sementara saat deploy) | `backend-deploy.yml:126-127` | **YA** |
| Role DB | `transcendence_app`, `transcendence_system`, migrator, owner | migrasi 002/008, B12.4 | **YA** (`pg_dumpall --globals-only`) |
| Helper provisioning | `/usr/local/sbin/transcendence-mt5-provision`, config `/etc/transcendence/provisioner.json`, sudoers `/etc/sudoers.d/transcendence-mt5` | `config.py:128`, helper `CONFIG_PATH` | **YA** |
| Terminal per customer | `/var/lib/transcendence/terminals/<CODE>/{wine-prefix,terminal,data}`, unit `transcendence-mt5-<code>.service`, `.provision_id`, `.install_complete`, `rpc.token` (mode ro) | `config.py:155`, helper | **YA** (arsip); lihat F-5 |
| Template MT5 | `/opt/transcendence/mt5-template` | dokumen | YA |
| Layout lama (single-akun) | `/opt/jurnal-mt5/backend` (SQLite `data/journal.db`), `/home/jurnal/.wine-mt5`, `/home/jurnal/mt5/mt5server.exe`, unit `jurnal-mt5.service`, `mt5server.service` | `docs/MT5-UBUNTU-NO-DOCKER.md:211-240` | **YA bila masih ada** (DB SQLite terpisah) |
| Nginx + TLS | `/etc/nginx/sites-available/jurnal`, `/etc/letsencrypt` | dokumen | YA |
| Cron backup | `/etc/cron.d/transcendence-backup` (02:15 harian, user `deploy`) | `docs/MULTI-CUSTOMER-BACKUP-ROLLBACK.md:15-17` | YA (konfigurasi) |
| Logrotate | `/etc/logrotate.d/transcendence-backup` | repo `deploy/logrotate-transcendence-backup` | YA (konfigurasi) |
| Redis | **tidak dipakai** backend (rate limiter in-memory; `PR_DESCRIPTION.md:67`) | repo | hanya bila ternyata terpasang & dipakai pihak lain (skrip §3) |

## C. Lokasi database dan data MT5

- **PostgreSQL:** `data_directory` dan ukuran per DB/tabel dicetak skrip §2.
  - Repo memakai PostgreSQL 16 (`docs/MULTI-CUSTOMER-ARSITEKTUR.md:30`); versi VPS **NOT VERIFIED**.
- **SQLite lama:** `/opt/jurnal-mt5/backend/data/journal.db` bila layout lama masih ada.
- **Data MT5 per terminal:**
  - `/var/lib/transcendence/terminals/<CODE>/terminal` (portable): `config/` (termasuk `accounts.dat`, yang bisa berisi login tersimpan → **sensitif**), `bases/` (cache riwayat, dapat diunduh ulang dari broker), `logs/`, `MQL5/`;
  - `wine-prefix/` per customer;
  - **riwayat jurnal yang sah ada di PostgreSQL** (`mt5_deals`, `mt5_orders`, `mt5_trades`, …), bukan di terminal.

## D. Status backup

| Backup | Status | Alasan |
|---|---|---|
| Harian `daily-*.sql.gz` di `/var/backups/transcendence` | **UNVERIFIED** | belum diamati. Skrip repo hanya memvalidasi gzip + tidak kosong + header; **tidak** memeriksa footer `dump complete` dan **tidak pernah** mencoba restore |
| Pra-deploy `pre-deploy-*.sql.gz` | **UNVERIFIED** | sama |
| Manual `manual-*.sql.gz` | **UNVERIFIED** | sama |
| Role / globals (`pg_dumpall -g`) | **NOT FOUND (di repo)** | tidak ada skrip yang membuatnya |
| `.env` / `MT5_CRED_KEY` / `MT5_RPC_SECRET` / `provisioner.json` | **NOT FOUND (di repo)** | tidak ada mekanisme backup |
| Terminal / Wine prefix / template / helper | **NOT FOUND (di repo)** | tidak ada mekanisme backup |
| Nginx / TLS / systemd / sudoers / cron | **NOT FOUND (di repo)** | tidak ada mekanisme backup |
| Salinan offsite | **NOT FOUND (di repo)** | semua backup ditulis ke **disk VPS yang sama** |

Skrip §7 memeriksa dump terbaru secara read-only: `gzip -t`, header, **footer**, dan sha256. Hasil itu pun hanya menaikkan status ke "terbaca". Sebuah dump baru **VERIFIED** setelah restore berhasil di host lain dan hasilnya dibandingkan (bagian G).

## E. Estimasi ukuran

Angka pasti menunggu skrip §10. Rumus yang dipakai:

```
DB        = pg_dump -Fc (≈ 15–40% ukuran pg_database_size untuk data teks/angka)
GLOBALS   = < 1 MB
FILES     = /var/lib/transcendence/terminals + /opt/transcendence/mt5-template
            + /home/jurnal (bila ada) + /etc/{transcendence,nginx,letsencrypt,systemd,sudoers.d,cron.d}
            + /opt/*/backend/.env + kode yang belum di-commit
            (per terminal MT5+Wine biasanya 1–3 GB; bases/ bisa beberapa GB)
EXISTING  = /var/backups/transcendence (salin apa adanya sebagai cadangan kedua)
PAKET     = DB + GLOBALS + FILES(terkompresi ≈ 50–70%) + EXISTING
Storage tujuan arsip ≥ 2 × PAKET (dua salinan) + ruang restore uji ≥ 3 × DB_size
```

Perkiraan kasar untuk 4 terminal (tanpa angka nyata, **NOT VERIFIED**):
- FILES 5–15 GB;
- DB kemungkinan < 1 GB;
- paket total sekitar 10–20 GB.

## F. Risiko kehilangan data dan keamanan

| # | Risiko | Tingkat |
|---|---|---|
| F-1 | **Semua backup (bila ada) berada di disk VPS yang akan kedaluwarsa** → hilang bersama VPS | Kritis |
| F-2 | **`MT5_CRED_KEY` (Fernet) hanya ada di `.env`.** Tanpa kunci itu, seluruh `mt5_credentials` tidak bisa didekripsi; semua customer harus menghubungkan ulang (riwayat tetap aman) | Kritis |
| F-3 | Dump tanpa globals: restore di server baru gagal pada GRANT/OWNER ke role yang belum ada | Tinggi |
| F-4 | Validasi dump hanya format (tanpa footer, tanpa restore) → dump terpotong secara logis bisa terlihat "valid" | Tinggi |
| F-5 | Terminal mode **classic** (RPyC tanpa autentikasi; default lama `MT5_RPC_MODE_DEFAULT=classic`): proses lokal mana pun di VPS bisa mengeksekusi kode di terminal. Wine prefix dari host berisiko harus diperlakukan **tidak tepercaya**; jangan dijalankan ulang apa adanya di VPS baru | Tinggi |
| F-6 | `accounts.dat` / `rpc.token` / `.env` / `provisioner.json` / dump globals (hash password role) adalah materi sensitif → arsip wajib dienkripsi; transfer wajib terenkripsi | Tinggi |
| F-7 | Layout lama (`/opt/jurnal-mt5`, SQLite) bisa masih menyimpan data yang tidak ada di PostgreSQL | Sedang (cek §2/§4) |
| F-8 | Perubahan kode lokal yang belum di-commit di `/opt/transcendence-hlwon-` | Sedang (cek §4) |
| F-9 | Sertifikat TLS & DNS: bisa diterbitkan ulang di VPS baru; tidak kritis untuk data | Rendah |
| F-10 | Kunci penandatangan APK Android **tidak** di VPS (CI/lokal). Pastikan terpisah tersimpan; di luar cakupan VPS | Perlu konfirmasi |
| F-11 | Waktu tersisa 1–2 minggu: kegagalan satu langkah tanpa salinan offsite = kehilangan total | Kritis |

## G. Rencana backup dan restore (USULAN — butuh persetujuan per langkah)

**Prinsip:**
- tidak menghentikan service kecuali disetujui;
- tidak mengubah DB;
- semua arsip terenkripsi;
- minimal dua salinan di luar VPS;
- dianggap sah hanya setelah restore uji di host lain.

1. **G0 Audit** — jalankan `emergency_preservation_audit.sh`, kirim keluarannya ke saya. Saya menyusun angka E dan daftar final.
2. **G1 Dump DB (konsisten, tanpa downtime).**
   - Dump: `pg_dump -Fc` per DB (snapshot konsisten bawaan pg_dump) + `pg_dumpall --globals-only`.
   - Manifest hitungan diambil di snapshot yang sama, metode M1 spesifikasi G2 §9.3.1 (`pg_export_snapshot` + `pg_dump --snapshot`).
   - Fingerprint isi lengkap (§9.3.2) **opsional** di keadaan darurat. Bila waktu sempit, cukup hitungan eksak + restore uji.
   - Ditulis ke direktori baru di disk VPS hanya bila ruang cukup (dari §1/§10); bila tidak, di-stream langsung ke host tujuan lewat SSH.
3. **G2 Arsip berkas** (`tar` + `zstd`/`gzip`, tanpa mengikuti symlink keluar):
   - `/opt/transcendence-hlwon-`, termasuk `.env` (tanpa `.venv` yang bisa dibangun ulang);
   - `/etc/transcendence`, `/usr/local/sbin/transcendence-mt5-provision`, `/opt/transcendence/mt5-template`;
   - `/var/lib/transcendence/terminals`, termasuk `.tombstones`/`.fence`;
   - layout lama `/opt/jurnal-mt5` + `/home/jurnal` (bila ada);
   - `/etc/systemd/system/{transcendence*,jurnal*,mt5*}`, `/etc/sudoers.d/transcendence*`, `/etc/cron.d/transcendence*`, `/etc/logrotate.d/transcendence*`, `/etc/nginx`, `/etc/letsencrypt`;
   - salinan apa adanya `/var/backups/transcendence`.

   Terminal yang **sedang berjalan** diarsipkan "crash-consistent". Itu cukup sebagai arsip, karena data jurnal yang sah ada di PostgreSQL.
4. **G3 Enkripsi + checksum** — enkripsi ke **kunci publik `age`/GPG milik Anda**. Kunci privat **tidak** pernah ada di VPS. Buat `SHA256SUMS` untuk setiap arsip terenkripsi.
5. **G4 Transfer keluar VPS** — **ditarik** dari mesin Anda (`rsync -e ssh`/`scp`) ke dua tujuan, mis. laptop + object storage. Lalu verifikasi `sha256sum -c` di setiap tujuan.
6. **G5 Verifikasi restore** di host terpisah (VPS baru atau mesin lokal) dengan **PostgreSQL major yang sama** (atau lebih baru):
   - restore globals → `pg_restore` → bandingkan hitungan eksak tabel inti dengan manifest G1;
   - `schema_migrations` harus sama;
   - uji dekripsi satu kredensial dengan `MT5_CRED_KEY` hasil salinan (tanpa menampilkan nilainya) untuk membuktikan kunci cocok.

   Baru setelah ini status backup = **VERIFIED**.
7. **G6 Restore di VPS baru:**
   - OS + PostgreSQL + role;
   - restore DB;
   - kode dari git (commit yang sama);
   - `.env` dan `provisioner.json`;
   - helper + template;
   - terminal **dibangun ulang sebagai `ro`** lewat jalur provisioning normal. Wine prefix lama hanya arsip (F-5);
   - backend dengan `MT5_ENABLED=0` dulu, lalu diaktifkan bertahap;
   - DNS/TLS terakhir.

   Tidak ada jalur trading yang dipakai di langkah apa pun.

Rollback:
- VPS lama tetap utuh sampai G5 lulus dan VPS baru berjalan;
- tidak ada langkah yang menghapus data di VPS lama.

## H. Rekomendasi VPS pengganti

Angka final menunggu audit:
- **vCPU 4** (MT5 di Wine cukup berat saat sinkronisasi);
- **RAM 8 GB minimal untuk 4 terminal** (± 1–1,5 GB per terminal + PostgreSQL + API); 16 GB bila target > 4 customer;
- **Disk SSD/NVMe ≥ 100 GB**: OS 20 GB + terminal 4 × 3–5 GB + DB × 3 (restore uji) + backup lokal 30 hari + cadangan 30%;
- **OS:** LTS yang sama atau sejenis dengan VPS lama, agar paket Wine/PostgreSQL setara. **PostgreSQL major ≥ versi lama** (pg_restore ke versi lebih baru didukung, sebaliknya tidak);
- snapshot/backup penyedia diaktifkan, dan backup offsite otomatis (bukan disk yang sama);
- region dekat server broker untuk latensi sinkronisasi (opsional).

## I. Tindakan yang membutuhkan persetujuan eksplisit

1. Anda menjalankan `emergency_preservation_audit.sh` di VPS (read-only) dan membagikan keluarannya. Periksa dulu bahwa tidak ada nilai rahasia yang terlihat.
2. Menulis berkas backup di disk VPS (G1/G2), atau memilih streaming langsung ke host tujuan.
3. Menjalankan `pg_dump`/`pg_dumpall` di DB produksi (beban baca; tanpa perubahan data).
4. Pilihan metode konsistensi: M1 snapshot ekspor (tanpa downtime, direkomendasikan) atau M2 hentikan writer (butuh stop service).
5. Membaca dan mengarsipkan berkas secret (`.env`, `provisioner.json`, `accounts.dat`, `rpc.token`, kunci privat TLS) ke arsip **terenkripsi**.
6. Kunci publik enkripsi dan dua tujuan penyimpanan offsite.
7. Transfer arsip keluar VPS.
8. Restore uji di host terpisah.
9. Pembuatan VPS baru dan langkah G6 (setiap langkah dengan persetujuan terpisah).
10. Akses sesi ini ke VPS, bila Anda ingin saya yang menjalankan langkah-langkahnya. Saat ini tidak mungkin dari sini: tidak ada ssh dan IP tidak dapat di-allowlist. Alternatifnya, jalankan Claude Code di mesin yang bisa SSH ke VPS.
