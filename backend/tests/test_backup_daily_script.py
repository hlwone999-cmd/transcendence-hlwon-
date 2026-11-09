"""TEMUAN REVIEW N7 — backup harian (cron) tervalidasi & owner konsisten.

Masalah: runbook cron lama menjalankan backup harian sebagai user
`postgres` yang MENULIS langsung ke /var/backups/transcendence
(milik deploy:deploy 750) — penulisan GAGAL SENYAP; pipeline
`pg_dump | gzip > final` tanpa pipefail, tanpa validasi isi, tanpa
penanganan kegagalan — pola persis yang audit round-4 #2 tutup untuk
jalur pra-deploy.

Perbaikan: skrip baru `backend/deploy/backup_daily.sh` berjalan sebagai
user DEPLOY (satu model owner dengan jalur pra-deploy):
`set -euo pipefail`, dump ke `.part` (umask 077), validasi tiga lapis
(gzip -t + tidak kosong + marker dump PG), publikasi atomik `mv`,
`chmod 600`, `LAST_DAILY` hanya setelah tervalidasi, retensi HANYA
untuk `daily-*.sql.gz`, tanpa sudo selain `sudo -u postgres pg_dump`,
dan preflight izin direktori (owner deploy + mode 750) TANPA sudo.
Dokumen runbook diselaraskan: cron user deploy (BACKUP-ROLLBACK §1),
sudoers minimal (PROVISIONING §10.4).

Tiga lapis pengujian:
1. STATIK       — skema skrip: pipefail, .part, validasi, urutan
                  mv < LAST_DAILY, retensi terbatas daily-*, tanpa
                  kredensial, TIDAK menyentuh kontrak stamp pra-deploy.
2. DOKUMEN      — cron memakai user deploy + skrip; pola lama
                  `postgres ... | gzip > final` hilang; provisioning
                  §10.4 memuat sudoers pg_dump; larangan 777.
3. SIMULASI     — bash backup_daily.sh dengan stub sudo/pg_dump/gzip:
                  sukses (final 600 + LAST_DAILY + kontrak pra-deploy
                  tak tersentuh + retensi selektif), pg_dump gagal,
                  dump kosong, bukan-PG, gzip korup, dir mode salah,
                  dir tidak ada, RETENTION_DAYS non-numerik.

Hardening pre-push (sesudah review sprint):
4. ATOMISITAS   — LAST_DAILY wajib ditulis ATOMIK: printf ke
                  .LAST_DAILY.part di direktori yang sama lalu mv
                  (rename POSIX) — bukan redirect in-place yang
                  truncate-then-write; bukti simulasi: inode pointer
                  BERUBAH setelah sukses (rename), tidak ada .part
                  tersisa, isi selalu stamp valid terbaru.
5. LOGROTATE    — kebijakan logrotate daily-cron.log wajib ada
                  (weekly + maxsize 5M, rotate 12, compress,
                  copytruncate, su deploy deploy) dan runbook wajib
                  menunjuk file kebijakan + path instalasinya.
6. FLOCK         — hardening konkurensi: hanya SATU instance
                  backup_daily.sh per BACKUP_DIR — flock non-blocking
                  pada fd `exec` (tetap terbuka, dilepas otomatis
                  kernel saat exit/gagal/kill), posisi setelah
                  preflight dan sebelum STAMP; instance kedua ditolak
                  dengan pesan jelas, tidak menyentuh pg_dump, dan run
                  berikutnya sukses setelah lock lepas.
"""
import fcntl
import gzip
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DAILY_SH = REPO / "backend" / "deploy" / "backup_daily.sh"
LOGROTATE_CONF = REPO / "backend" / "deploy" / "logrotate-transcendence-backup"
BACKUP_DOC = REPO / "docs" / "MULTI-CUSTOMER-BACKUP-ROLLBACK.md"
PROV_DOC = REPO / "docs" / "MULTI-CUSTOMER-INSTALASI-PROVISIONING.md"

DUMP_OK = (b"--\n-- PostgreSQL database dump\n--\n"
           b"CREATE TABLE customers (id int);\n")


# ---------------------------------------------------------------- 1. statik
def test_daily_pipefail_dan_validasi_tiga_lapis():
    raw = DAILY_SH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in raw, "wajib pipefail (temuan inti N7)"
    assert "umask 077" in raw, "file dump wajib 600 sejak dibuat"
    assert ".part" in raw, "dump wajib ke file sementara (.part)"
    assert "gzip -t" in raw, "wajib validasi gzip -t"
    assert "wc -c" in raw, "wajib cek dump tidak kosong"
    assert "PostgreSQL database dump" in raw, "wajib marker dump PG"
    assert "mv" in raw, "publikasi atomik mv .part -> final"
    assert "trap" in raw and "rm -f" in raw, ".part wajib dibersihkan trap"
    assert "chmod 600" in raw, "dump berisi data transaksi — wajib 600"


def test_daily_tanpa_kredensial_database():
    raw = DAILY_SH.read_text(encoding="utf-8")
    assert "sudo -u postgres pg_dump" in raw, \
        "dump wajib via peer-auth sudo (tanpa kredensial di cron)"
    for forbidden in ("PGPASSWORD", "DATABASE_URL", "password"):
        assert forbidden not in raw, \
            f"skrip cron tidak boleh memuat {forbidden}"


def test_daily_tidak_menyentuh_kontrak_pra_deploy():
    """Daily punya pointer sendiri (LAST_DAILY); kontrak stamp pra-deploy
    tidak boleh dibaca/ditulis/dihapus oleh jalur harian."""
    raw = DAILY_SH.read_text(encoding="utf-8")
    assert "LAST_DAILY" in raw, "pointer harian wajib ada"
    assert "LAST_PRE_DEPLOY" not in raw, \
        "jalur harian tidak boleh menyentuh LAST_PRE_DEPLOY"
    lines = raw.splitlines()
    idx_mv = [i for i, ln in enumerate(lines) if re.search(r"mv .*TARGET", ln)]
    # publikasi pointer final = mv .LAST_DAILY.part ke LAST_DAILY
    idx_last = [i for i, ln in enumerate(lines)
                if re.search(r"mv \"\$PART_POINTER\" \"\$BACKUP_DIR/LAST_DAILY\"", ln)]
    assert idx_mv and idx_last, "mv/LAST_DAILY tidak ditemukan"
    assert min(idx_mv) < min(idx_last), \
        "LAST_DAILY wajib ditulis SETELAH backup tervalidasi (mv)"


def test_daily_retensi_hanya_daily_sql_gz():
    raw = DAILY_SH.read_text(encoding="utf-8")
    m = re.search(r"find \"?\$BACKUP_DIR\"?.*-name 'daily-\*\.sql\.gz'", raw)
    assert m, "retensi wajib cocokkan pola sempit daily-*.sql.gz"
    assert "-maxdepth 1" in raw, "retensi tidak boleh turun ke subdirektori"
    assert "-delete" not in raw, \
        "hapus eksplisit satu per satu, bukan find -delete buta"
    assert "rm -rf" not in raw, "rm -rf dilarang di skrip backup"
    # preflight numerik untuk RETENTION_DAYS (masuk argumen find)
    assert "RETENTION_DAYS tidak valid" in raw
    assert not re.search(r"find\s+.*-delete", raw), \
        "hapus eksplisit satu per satu, bukan find -delete buta"


def test_daily_preflight_izin_tanpa_sudo():
    raw = DAILY_SH.read_text(encoding="utf-8")
    assert "stat -c" in raw, "preflight wajib cek owner/mode via stat"
    assert '"750"' in raw, "preflight wajib menuntut mode 750"
    assert "install -d" in raw, \
        "pesan gagal wajib menunjuk perintah perbaikan provisioning"
    # satu-satunya sudo INVOKE di skrip = pg_dump (permukaan sudo cron
    # minimal) — baris eksekusi = baris yang memulai `sudo ` (bukan
    # komentar maupun string pesan gagal).
    exec_lines = [ln for ln in raw.splitlines()
                  if ln.strip().startswith("sudo ")]
    assert len(exec_lines) == 1 and "pg_dump" in exec_lines[0], \
        f"cron hanya boleh invoke sudo pg_dump, ditemukan: {exec_lines}"


# ---------------------------------------------------------------- 2. dokumen
def test_runbook_cron_memakai_user_deploy_dan_skrip():
    raw = BACKUP_DOC.read_text(encoding="utf-8")
    assert "backup_daily.sh" in raw, "runbook wajib menunjuk skrip baru"
    m = re.search(r"^\s*15 2 \* \* \* (\S+) ", raw, re.M)
    assert m, "baris cron tidak ditemukan di runbook"
    assert m.group(1) == "deploy", \
        f"cron wajib berjalan sebagai deploy, bukan {m.group(1)}"
    # pola lama yang gagal senyap wajib hilang total
    assert not re.search(r"\* \* \* postgres pg_dump", raw), \
        "cron lama sebagai postgres masih ada di runbook"
    assert "RETENTION_DAYS" in raw and "LAST_DAILY" in raw, \
        "runbook wajib menjelaskan retensi & pointer harian"


def test_provisioning_sudoers_minimal_dan_larangan_777():
    raw = PROV_DOC.read_text(encoding="utf-8")
    assert "10.4" in raw, "provisioning wajib punya §10.4 sudoers"
    assert "sudoers.d/transcendence-deploy" in raw
    assert re.search(r"NOPASSWD:\s*/usr/bin/pg_dump transcendence", raw), \
        "sudoers minimal wajib memuat pg_dump transcendence"
    assert "777" in raw, "larangan 777 wajib tertulis"
    # §10.1 tetap menuntut deploy:deploy 750
    assert re.search(r"-o deploy -g deploy -m 750 /var/backups/transcendence",
                     raw), "model ownership deploy:deploy 750 wajib tetap ada"


# --------------------------------------------------- 4. hardening atomik
def test_last_daily_ditulis_atomik_tmp_lalu_mv():
    """REGRESI hardening pre-push: penulisan LAST_DAILY wajib ATOMIK —
    printf ke .LAST_DAILY.part di DIREKTORI YANG SAMA (syarat rename
    POSIX: satu filesystem) lalu mv ke tempat final. Redirect in-place
    (truncate-then-write) DILARANG: kill/crash/habisnya disk di tengah
    penulisan meninggalkan pointer kosong yang bisa disalahbaca
    operasional sebagai 'belum pernah backup'."""
    raw = DAILY_SH.read_text(encoding="utf-8")
    assert ".LAST_DAILY.part" in raw, "wajib file sementara .LAST_DAILY.part"
    # tidak ada lagi redirect in-place langsung ke pointer final
    assert not re.search(r">\s*\"\$BACKUP_DIR/LAST_DAILY\"", raw), \
        "redirect in-place ke LAST_DAILY dilarang — wajib tmp + mv"
    # file sementara wajib di direktori yang SAMA (satu filesystem)
    m = re.search(r'PART_POINTER="(\$BACKUP_DIR/[^"]+)"', raw)
    assert m, "PART_POINTER wajib didefinisikan di bawah $BACKUP_DIR"
    # urutan wajib: mv dump -> printf ke part -> mv pointer final
    lines = raw.splitlines()
    idx_mv_dump = [i for i, ln in enumerate(lines) if re.search(r"mv .*TARGET", ln)]
    idx_print = [i for i, ln in enumerate(lines)
                 if re.search(r"printf .*\$STAMP.*PART_POINTER", ln)]
    idx_mv_ptr = [i for i, ln in enumerate(lines)
                  if re.search(r"mv \"\$PART_POINTER\"", ln)]
    assert idx_mv_dump and idx_print and idx_mv_ptr, "urutan atomik tidak lengkap"
    assert min(idx_mv_dump) < min(idx_print) < min(idx_mv_ptr), \
        "urutan wajib: mv dump -> printf .LAST_DAILY.part -> mv pointer"
    # trap EXIT wajib membersihkan .LAST_DAILY.part juga (bukan cuma dump .part)
    m_trap = re.search(r"^trap '(.+)' EXIT$", raw, re.M)
    assert m_trap and "PART_POINTER" in m_trap.group(1), \
        "trap EXIT wajib membersihkan .LAST_DAILY.part"
    # pointer sementara wajib 600 (umask 077 + chmod eksplisit)
    assert 'chmod 600 "$PART_POINTER"' in raw


def test_last_daily_pointer_diganti_atomik_bukan_truncate_inplace(tmp_path):
    """Simulasi atomisitas: LAST_DAILY LAMA wajib DIGANTI lewat rename —
    inode pointer BERUBAH setelah run sukses (bukti bukan
    truncate-then-write di tempat yang mempertahankan inode), isi baru
    utuh, mode tetap 600, dan tidak ada file sementara tersisa."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub_pg_dump_ok(bin_dir, log)
    pointer = bk / "LAST_DAILY"
    pointer.write_text("daily-20260101-020000\n")
    pointer.chmod(0o600)
    ino_lama = os.stat(pointer).st_ino

    r = _run(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    stamp_baru = _final(bk).name[:-len(".sql.gz")]
    assert pointer.read_text().strip() == stamp_baru, \
        "isi pointer wajib stamp valid terbaru (terganti, bukan rusak)"
    assert oct(os.stat(pointer).st_mode)[-3:] == "600", "pointer wajib 600"
    assert os.stat(pointer).st_ino != ino_lama, \
        "inode wajib berubah (rename tmp+mv) — bukan truncate in-place"
    assert not list(bk.glob(".*part")), \
        "file sementara (dump .part / .LAST_DAILY.part) wajib bersih"


# ------------------------------------------------- 5. hardening logrotate
def test_logrotate_kebijakan_daily_cron_log():
    """REGRESI hardening pre-push: log cron daily-cron.log tumbuh tiap
    run — TANPA rotasi ia menghabiskan disk direktori backup 750 dan
    ikut mematikan backup berikutnya. Kebijakan wajib ada di repo,
    memuat opsi pengaman, dan runbook wajib menunjuk file + path
    pemasangannya (/etc/logrotate.d)."""
    assert LOGROTATE_CONF.is_file(), "file kebijakan logrotate wajib ada"
    raw = LOGROTATE_CONF.read_text(encoding="utf-8")
    assert re.search(r"^/var/backups/transcendence/daily-cron\.log\s*\{$", raw, re.M), \
        "blok logrotate wajib menyasar daily-cron.log"
    for opt in ("weekly", "maxsize", "rotate", "dateext", "compress",
                "copytruncate", "missingok", "notifempty", "su deploy deploy"):
        assert re.search(rf"^\s*{re.escape(opt)}\b", raw, re.M), \
            f"kebijakan wajib memuat opsi '{opt}'"
    # bila 'create' dipakai, modenya wajib ketat (tanpa bit group/other)
    m_create = re.search(r"^\s*create\s+(\d{3})", raw, re.M)
    if m_create:
        assert int(m_create.group(1), 8) & 0o077 == 0, \
            "create mode logrotate wajib ketat (tanpa akses group/other)"
    # copytruncate WAJIB: cron menulis via '>>' tiap run tanpa daemon
    # pemegang fd — tanpa copytruncate butuh postrotate yang tidak ada
    # jalurnya memutar log yang sedang ditulis cron berikutnya.
    assert re.search(r"^\s*copytruncate\s*$", raw, re.M)
    # konsistensi runbook: menunjuk file kebijakan + path instalasi
    doc = BACKUP_DOC.read_text(encoding="utf-8")
    assert "logrotate-transcendence-backup" in doc, \
        "runbook wajib menunjuk file kebijakan logrotate"
    assert "/etc/logrotate.d/transcendence-backup" in doc, \
        "runbook wajib memuat path pemasangan /etc/logrotate.d"
    assert "daily-cron.log" in doc, "runbook wajib membahas log cron"


# -------------------------------------------------- 6. hardening flock
def test_flock_guard_satu_instance():
    """REGRESI hardening konkurensi: hanya SATU instance backup_daily.sh
    boleh berjalan — flock NON-blocking pada fd yang dibuka `exec`
    (tetap terbuka sepanjang proses; dilepas otomatis kernel saat proses
    keluar). Posisi wajib: SETELAH preflight izin (dir tervalidasi) dan
    SEBELUM STAMP/dump — instance yang ditolak tidak boleh sampai
    menyentuh pg_dump."""
    raw = DAILY_SH.read_text(encoding="utf-8")
    lines = raw.splitlines()
    # kode eksekusi = baris non-komentar (larangan berlaku untuk perintah,
    # bukan dokumentasi di komentar)
    kode = "\n".join(ln for ln in lines if not ln.strip().startswith("#"))
    assert "exec 9>" in raw, "wajib buka fd lock dengan exec (tetap terbuka)"
    assert "flock -n 9" in kode, "wajib flock NON-blocking"
    assert "flock -u" not in kode, \
        "lepas lock otomatis oleh kernel saat exit — tanpa flock -u di kode"
    assert "$$" not in kode, \
        "nama file sementara tetap ber-stamp tanpa PID — guard flock " \
        "sudah menutup race antar-instance"
    assert re.search(r'exec 9>"\$BACKUP_DIR/\.backup_daily\.lock"', raw), \
        "lock file wajib .backup_daily.lock di dalam BACKUP_DIR"
    assert "sedang berjalan" in raw, "penolakan wajib dengan pesan jelas"
    # urutan wajib: preflight mode 750 -> flock -> STAMP
    idx_preflight = [i for i, ln in enumerate(lines)
                     if '_dir_mode" = "750"' in ln]
    idx_flock = [i for i, ln in enumerate(lines)
                 if ln.strip().startswith("flock -n 9")]
    idx_stamp = [i for i, ln in enumerate(lines) if ln.startswith("STAMP=")]
    assert idx_preflight and idx_flock and idx_stamp, "urutan tidak lengkap"
    assert min(idx_preflight) < min(idx_flock) < min(idx_stamp), \
        "flock wajib SETELAH preflight dan SEBELUM STAMP/dump"


def test_flock_instance_kedua_ditolak_lalu_berhasil_setelah_lock_lepas(tmp_path):
    """Simulasi dua proses bersamaan: lock dipegang 'instance pertama'
    (fd python pada lock file yang sama) — instance kedua WAJIB ditolak
    dengan pesan jelas, tanpa dump terbit, pointer tidak berubah, dan
    pg_dump tidak pernah dijalankan; setelah lock dilepas, run
    berikutnya sukses dan LAST_DAILY menunjuk dump yang benar-benar
    sudah terbit (tidak korup)."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub_pg_dump_ok(bin_dir, log)
    lockfile = bk / ".backup_daily.lock"
    fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # 'instance pertama'
    (bk / "LAST_DAILY").write_text("daily-20260101-020000\n")

    r1 = _run(env, tmp_path)                          # instance kedua
    assert r1.returncode != 0, "instance kedua wajib ditolak flock"
    assert "sedang berjalan" in r1.stdout, "pesan penolakan wajib jelas"
    assert not list(bk.glob("daily-*.sql.gz")), \
        "tidak boleh ada dump terbit saat ditolak"
    assert (bk / "LAST_DAILY").read_text().strip() == "daily-20260101-020000", \
        "pointer wajib tidak berubah saat ditolak"
    calls = log.read_text() if log.exists() else ""
    assert "pg_dump" not in calls, \
        "dump wajib TIDAK dijalankan setelah ditolak lock"

    os.close(fd)                                      # lock dilepas
    r2 = _run(env, tmp_path)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    final = _final(bk)
    assert (bk / "LAST_DAILY").read_text().strip() == final.name[:-len(".sql.gz")], \
        "pointer wajib menunjuk dump yang BENAR-BENAR sudah terbit"
    assert gzip.decompress(final.read_bytes()).startswith(b"--\n-- PostgreSQL"), \
        "dump yang terbit wajib valid (tidak korup)"
    assert not list(bk.glob(".*part")), "tanpa file sementara tersisa"


def test_flock_lepas_otomatis_setelah_gagal_dan_setelah_proses_dibunuh(tmp_path):
    """Lock wajib TERLEPAS OTOMATIS: (a) proses gagal exit — tanpa
    flock -u; (b) proses DIBUNUH kill -9 — kernel melepas lock pemiliknya
    (trap tidak berjalan pada SIGKILL, lock tetap lepas karena fd tertutup
    kernel). Setelah itu run berikutnya wajib dapat berjalan dan
    menerbitkan dump valid; pointer tidak pernah menunjuk file yang
    belum terbit."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    lockfile = bk / ".backup_daily.lock"

    # (a) proses gagal (pg_dump exit 1) — lock wajib lepas saat exit
    _stub(bin_dir, "pg_dump", log, "exit 1")
    r1 = _run(env, tmp_path)
    assert r1.returncode != 0
    fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)    # sukses = lock lepas
    os.close(fd)

    # (b) proses nyata di latar, dibunuh SAAT MEMEGANG lock.
    # Marker dari stub pg_dump = bukti deterministik lock sudah terpegang:
    # stub hanya berjalan SETELAH flock sukses (guard ada sebelum STAMP).
    marker = tmp_path / "pg_dump_mulai"
    _stub(bin_dir, "pg_dump", log, f"touch {marker}; sleep 30")
    proc = subprocess.Popen(["bash", str(DAILY_SH)],
                            env={**os.environ, **env},
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            cwd=str(tmp_path),
                            start_new_session=True)  # grup proses sendiri
    deadline = time.time() + 10
    while time.time() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists(), \
        "stub pg_dump wajib sempat mulai (skrip memegang lock)"

    (bk / "LAST_DAILY").write_text("daily-20260101-020000\n")  # kondisi awal
    # Bunuh SELURUH grup proses (bash + anak pipeline pewaris fd 9) —
    # cara operator mematikan job yang kabur; SIGKILL = trap tak berjalan.
    pgid = os.getpgid(proc.pid)
    os.killpg(pgid, signal.SIGKILL)
    proc.wait()
    # proc.wait() hanya menunggu bash INDUK. Anak dalam grup yang sama
    # (subshell/pipeline pewaris fd 9) bisa masih memproses SIGKILL —
    # kernel baru menutup fd (melepas lock) saat proses itu benar-benar
    # keluar. Tunggu sampai TIDAK ada anggota grup yang hidup, lalu cek
    # lock secara KETAT (LOCK_NB, tanpa retry).
    assert _tunggu_grup_mati(pgid), "anggota grup proses tidak mati"
    fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)    # sukses = kernel lepas
    os.close(fd)
    # kill terjadi sebelum publikasi: pointer tidak boleh menunjuk dump baru
    assert (bk / "LAST_DAILY").read_text().strip() == "daily-20260101-020000"
    assert not list(bk.glob("daily-*.sql.gz")), \
        "tidak boleh ada dump terbit dari run yang dibunuh"

    # (c) run berikutnya berjalan normal setelah lock lepas
    _stub_pg_dump_ok(bin_dir, log)
    r2 = _run(env, tmp_path)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    final = _final(bk)          # tepat 1 dump harian final — tak ada korup
    assert gzip.decompress(final.read_bytes()).startswith(b"--\n-- PostgreSQL")
    assert (bk / "LAST_DAILY").read_text().strip() == final.name[:-len(".sql.gz")]


def _tunggu_grup_mati(pgid: int, timeout: float = 10.0) -> bool:
    """True bila semua anggota grup proses [pgid] sudah keluar (tidak
    ada lagi, atau tinggal zombie — fd zombie sudah ditutup kernel)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        hidup = False
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                f = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(f[2]) == pgid and f[0] != "Z":
                hidup = True
                break
        if not hidup:
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------- 3. simulasi
def _dunia(tmp_path: Path):
    """Dunia palsu utk menjalankan backup_daily.sh sungguhan.
    Direktori backup dibuat sesuai provisioning §10.1: owner user
    sekarang (mewakili deploy), mode 750."""
    bk = tmp_path / "var" / "backups" / "transcendence"
    bk.mkdir(parents=True)
    os.chmod(bk, 0o750)
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    env = {
        "BACKUP_DIR": str(bk),
        "DB_NAME": "transcendence",
        "RETENTION_DAYS": "30",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    return bk, bin_dir, log, env


def _stub(bin_dir: Path, name: str, log: Path, body: str):
    p = bin_dir / name
    p.write_text(f'#!/bin/bash\necho "{name} $*" >> {log}\n{body}\n')
    p.chmod(0o755)


def _stub_sudo(bin_dir: Path, log: Path):
    """sudo: catat, lewati -u <user>, eksekusi target (stub pg_dump)."""
    p = bin_dir / "sudo"
    p.write_text('#!/bin/bash\n'
                 f'echo "sudo $*" >> {log}\n'
                 'if [ "$1" = "-u" ]; then shift 2; fi\n'
                 'exec "$@"\n')
    p.chmod(0o755)


def _stub_pg_dump_ok(bin_dir: Path, log: Path):
    _stub(bin_dir, "pg_dump", log, f"cat << 'EOF'\n{DUMP_OK.decode()}EOF")


def _run(env, tmp_path):
    return subprocess.run(["bash", str(DAILY_SH)], env={**os.environ, **env},
                          capture_output=True, text=True, cwd=str(tmp_path))


def _final(bk: Path):
    f = sorted(bk.glob("daily-*.sql.gz"))
    assert len(f) == 1, f"harus tepat 1 file daily final, ada: {f}"
    return f[0]


def _tua(p: Path, hari: int):
    lama = time.time() - hari * 86400
    os.utime(p, (lama, lama))


def test_daily_sukses_publikasi_600_pointer_retensi_selektif(tmp_path):
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub_pg_dump_ok(bin_dir, log)
    # arsip lama: daily 40 hari (wajib terhapus), pra-deploy 40 hari
    # (wajib SELAMAT — kontrak rollback tidak boleh disentuh retensi),
    # dan pointer kontrak pra-deploy milik workflow.
    old_daily = bk / "daily-20260101-020000.sql.gz"
    old_daily.write_bytes(gzip.compress(DUMP_OK))
    _tua(old_daily, 40)
    old_pre = bk / "pre-deploy-run-999-attempt-1.sql.gz"
    old_pre.write_bytes(gzip.compress(DUMP_OK))
    _tua(old_pre, 40)
    (bk / "LAST_PRE_DEPLOY").write_text("run-999-attempt-1\n")

    r = _run(env, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    final = _final(bk)
    assert final.name.startswith("daily-") and final != old_daily
    assert gzip.decompress(final.read_bytes()).startswith(
        b"--\n-- PostgreSQL"), "isi final wajib dump PG yang tervalidasi"
    assert oct(os.stat(final).st_mode)[-3:] == "600", "dump wajib 600"
    stamp = final.name[:-len(".sql.gz")]   # stamp penuh: daily-YYYYmmdd-HHMMSS
    assert (bk / "LAST_DAILY").read_text().strip() == stamp
    assert oct(os.stat(bk / "LAST_DAILY").st_mode)[-3:] == "600"
    assert not list(bk.glob(".*part")), ".part wajib sudah berpindah"
    assert not old_daily.exists(), "daily tua >30 hari wajib dihapus"
    assert old_pre.exists(), \
        "pre-deploy-* TIDAK boleh dihapus retensi harian"
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == \
        "run-999-attempt-1", "kontrak pra-deploy wajib utuh"
    assert "OK:" in r.stdout and "retensi: 1" in r.stdout


def test_pg_dump_gagal_tidak_ada_publikasi_pointer_tak_berubah(tmp_path):
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    (bk / "LAST_DAILY").write_text("daily-20260101-020000\n")
    # pg_dump: tulis sebagian SQL lalu MATI (exit 1) — koneksi putus.
    _stub(bin_dir, "pg_dump", log,
          "echo '-- PostgreSQL database dump'; echo 'CREATE ...'; exit 1")
    r = _run(env, tmp_path)
    assert r.returncode != 0, "pipefail wajib menggagalkan step"
    assert not list(bk.glob("daily-*.sql.gz")), \
        "tidak boleh ada file final dari backup gagal"
    assert (bk / "LAST_DAILY").read_text().strip() == \
        "daily-20260101-020000", "pointer tidak boleh berubah saat gagal"
    assert not list(bk.glob(".*part")), ".part wajib dibersihkan trap"


def test_dump_kosong_ditolak_walau_gzip_valid(tmp_path):
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub(bin_dir, "pg_dump", log, "exit 0")     # stdout kosong
    r = _run(env, tmp_path)
    assert r.returncode != 0
    assert "KOSONG" in r.stdout
    assert not list(bk.glob("daily-*.sql.gz"))
    assert not (bk / "LAST_DAILY").exists()


def test_dump_bukan_pg_ditolak_marker(tmp_path):
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub(bin_dir, "pg_dump", log,
          "echo 'pg_dump: error: koneksi ditolak server'; exit 0")
    r = _run(env, tmp_path)
    assert r.returncode != 0
    assert "bukan dump PostgreSQL" in r.stdout
    assert not list(bk.glob("daily-*.sql.gz"))


def test_gzip_korup_ditolak_gzip_t(tmp_path):
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    # gzip: pada pipeline (tanpa -t) tulis sampah + exit 0;
    # pada gzip -t jalankan gzip ASLI (menolak sampah).
    _stub(bin_dir, "gzip", log,
          'if [ "$1" = "-t" ]; then exec /bin/gzip "$@"; fi; '
          "cat > /dev/null; "
          'printf "BUKAN-GZIP\\x00\\x01\\x02" > "$0-out"; '
          'cat "$0-out"')
    _stub_pg_dump_ok(bin_dir, log)
    r = _run(env, tmp_path)
    assert r.returncode != 0, "hasil gzip korup wajib ditolak"
    assert not list(bk.glob("daily-*.sql.gz"))


def test_dir_mode_salah_ditolak_tanpa_sama_sekali_dump(tmp_path):
    """Preflight izin: mode selain 750 -> gagal SEBELUM sudo/pg_dump."""
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub_pg_dump_ok(bin_dir, log)
    os.chmod(bk, 0o770)
    r = _run(env, tmp_path)
    assert r.returncode != 0
    assert "wajib 750" in r.stdout
    calls = log.read_text() if log.exists() else ""
    assert "pg_dump" not in calls, \
        "preflight gagal — dump tidak boleh sempat dijalankan"


def test_dir_tidak_ada_gagal_dengan_pesan_provisioning(tmp_path):
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub_pg_dump_ok(bin_dir, log)
    env["BACKUP_DIR"] = str(bk.parent / "tidak-ada")
    r = _run(env, tmp_path)
    assert r.returncode != 0
    assert "install -d" in r.stdout, "wajib menunjuk perintah provisioning"
    calls = log.read_text() if log.exists() else ""
    assert "pg_dump" not in calls


def test_retention_days_non_numerik_ditolak(tmp_path):
    bk, bin_dir, log, env = _dunia(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub_pg_dump_ok(bin_dir, log)
    env["RETENTION_DAYS"] = "30; rm -rf /"
    r = _run(env, tmp_path)
    assert r.returncode != 0
    assert "RETENTION_DAYS tidak valid" in r.stdout
    calls = log.read_text() if log.exists() else ""
    assert "pg_dump" not in calls
