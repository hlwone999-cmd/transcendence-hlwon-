"""TEMUAN AUDIT ROUND-5 — serialisasi deployment production + stamp unik.

Dua temuan tambahan pada workflow Backend Deploy:

1. CONCURRENCY: workflow belum punya concurrency group — dua run
   workflow_dispatch bisa berjalan PARALEL dan keduanya memodifikasi
   VPS/database produksi yang sama (backup, migrasi, restart service,
   dan file stamp saling menginjak).

2. STAMP BACKUP: `STAMP=$(date +%Y%m%d-%H%M%S)` = timestamp resolusi
   DETIK — dua run (atau re-run) yang jatuh pada detik yang sama
   menghasilkan stamp IDENTIK: file final `pre-deploy-<stamp>.sql.gz`
   saling menimpa dan backup lama bisa terkecoh dianggap milik run ini.

Perbaikan (semuanya dites di file ini):
1. Workflow-level `concurrency: group: backend-deploy-production` +
   `cancel-in-progress: false` — satu antrian produksi: tepat satu
   deployment berjalan pada satu waktu, run berikutnya MENUNGGU, dan
   deployment berjalan TIDAK PERNAH dibatalkan run baru. Nama group
   statis + monopoli workflow ini (workflow lain tidak boleh memakai
   nama yang sama — group GitHub Actions bersifat global lintas
   workflow).
2. Stamp = identifier unik yang MEMUAT github.run_id dan
   github.run_attempt: "run-<run_id>-attempt-<run_attempt>" — dibuat
   backend/deploy/make_backup_stamp.sh dan dijalankan sungguhan di sini
   (deterministik, unik antar run/attempt, filename-safe, menolak input
   tidak valid). backup_db.sh & rollback.sh tetap memvalidasi charset
   [0-9A-Za-z_-] (gate lama tetap, dites round-4).
3. Kontrak round-4 TETAP: rollback HANYA memakai PRE_DEPLOY_STAMP milik
   run yang sama — dump run/attempt SEBELUMNYA (atau LAST_PRE_DEPLOY)
   tidak pernah dianggap backup run terbaru.

Empat lapis pengujian:
  1. STATIK WORKFLOW — concurrency block di level workflow (kolom 0),
     group statis khusus production tanpa ${{ }}, cancel-in-progress:
     false, monopoli nama group, skema timestamp detik HILANG, wiring
     github.run_id/github.run_attempt -> make_backup_stamp.sh ->
     PRE_DEPLOY_STAMP -> rollback.sh (urutan ekspor sebelum backup).
  2. STATIK SKRIP — make_backup_stamp.sh: pipefail, wajib RUN_ID/
     RUN_ATTEMPT, format run-<id>-attempt-<n>, gate charset filename-
     safe konsisten di ketiga skrip deploy.
  3. PERILAKU STAMP — skrip dijalankan sungguhan: deterministik, unik
     (banyak pasangan, sebagian KONKUREN pada detik yang sama — skema
     timestamp lama BISA bertabrakan di sini), menolak input kosong/
     non-numerik/karakter aneh, dan diterima penuh oleh backup_db.sh.
  4. PERILAKU BACKUP/ROLLBACK — dua backup KONKUREN stamp berbeda tidak
     saling menimpa (defense-in-depth); skenario penuh attempt-1 sukses
     -> attempt-2 (re-run) backup GAGAL -> rollback stamp attempt-2
     MENOLAK dump attempt-1/LAST_PRE_DEPLOY (backup lama bukan run
     terbaru) -> rollback stamp attempt-1 me-restore PERSIS dump-nya.
"""
import gzip
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
WF = WORKFLOWS / "backend-deploy.yml"
STAMP_SH = REPO / "backend" / "deploy" / "make_backup_stamp.sh"
BACKUP_SH = REPO / "backend" / "deploy" / "backup_db.sh"
ROLLBACK_SH = REPO / "backend" / "deploy" / "rollback.sh"

GROUP = "backend-deploy-production"

DUMP_HEAD = "--\n-- PostgreSQL database dump\n--\n"


# ======================================================== 1. STATIK WORKFLOW
def test_concurrency_group_khusus_production_di_level_workflow():
    """TEMUAN #1: block concurrency wajib ADA di level workflow (kolom 0
    — berlaku untuk seluruh run), group STATIS khusus production, dan
    cancel-in-progress: false."""
    lines = WF.read_text(encoding="utf-8").splitlines()
    idx = [i for i, ln in enumerate(lines) if re.match(r"^concurrency:\s*$", ln)]
    assert len(idx) == 1, "block concurrency wajib ada TEPAT SATU di level workflow"
    i = idx[0]
    m = re.match(r"^\s+group:\s*(\S+)\s*$", lines[i + 1])
    assert m, f"baris setelah 'concurrency:' wajib 'group:' (dapat {lines[i+1]!r})"
    group = m.group(1)
    assert group == GROUP, f"group wajib '{GROUP}' (dapat '{group}')"
    assert "production" in group, "group wajib menandai production"
    assert "${{" not in group, \
        "group wajib STATIS — group dinamis per-ref memungkinkan dua run " \
        "dari ref berbeda jalan paralel di VPS yang sama"
    m = re.match(r"^\s+cancel-in-progress:\s*false\s*$", lines[i + 2])
    assert m, \
        "cancel-in-progress wajib eksplisit FALSE — run berikutnya MENUNGGU, " \
        "deployment berjalan tidak pernah dibatalkan di tengah backup/migrasi"
    raw = WF.read_text(encoding="utf-8")
    assert not re.search(r"cancel-in-progress:\s*true", raw), \
        "tidak boleh ada cancel-in-progress: true di workflow ini"


def test_group_production_monopoli_backend_deploy():
    """Nama concurrency group GitHub Actions berlaku GLOBAL lintas
    workflow. Bila workflow lain (mis. CI backend-tests) memakai group
    sama, run CI bisa mengantre di depan/membatalkan deployment
    produksi. Group ini wajib monopoli Backend Deploy."""
    for f in WORKFLOWS.glob("*.yml"):
        if f.name == "backend-deploy.yml":
            continue
        assert GROUP not in f.read_text(encoding="utf-8"), \
            f"group produksi tidak boleh dipakai workflow lain: {f.name}"


def test_skema_stamp_timestamp_detik_dihapus():
    """TEMUAN #2: `date +%Y%m%d-%H%M%S` (resolusi detik) wajib HILANG —
    dua run pada detik sama menghasilkan stamp identik."""
    raw = WF.read_text(encoding="utf-8")
    assert "STAMP=$(date" not in raw, \
        "stamp dari timestamp detik masih ada di workflow"
    assert "%Y%m%d-%H%M%S" not in raw, \
        "pola timestamp detik masih ada di workflow"


def test_wiring_run_id_run_attempt_ke_stamp_ke_rollback():
    """Stamp wajib MEMUAT github.run_id dan github.run_attempt (via
    make_backup_stamp.sh), diekspor SEBAGAI PRE_DEPLOY_STAMP sebelum
    backup dijalankan, dan diteruskan ke rollback.sh."""
    raw = WF.read_text(encoding="utf-8")
    assert "backend/deploy/make_backup_stamp.sh" in raw, \
        "stamp wajib dibuat make_backup_stamp.sh (dites pytest)"
    assert "${{ github.run_id }}" in raw, "stamp wajib memuat github.run_id"
    assert "${{ github.run_attempt }}" in raw, \
        "stamp wajib memuat github.run_attempt"
    assert "RUN_ID='${{ github.run_id }}'" in raw
    assert "RUN_ATTEMPT='${{ github.run_attempt }}'" in raw
    # dan diteruskan ke rollback.sh — di-anchor pada step backup agar
    # tidak tertukar dengan penyebutan di komentar header
    assert re.search(r'echo "PRE_DEPLOY_STAMP=\$STAMP" >> "\$GITHUB_ENV"', raw)
    idx_step = raw.index("Backup database pra-deploy")
    idx_export = raw.index("PRE_DEPLOY_STAMP=$STAMP", idx_step)
    idx_backup = raw.index("backend/deploy/backup_db.sh", idx_export)
    assert idx_export < idx_backup, \
        "PRE_DEPLOY_STAMP wajib diekspor SEBELUM backup dijalankan " \
        "(step rollback if: failure() tetap tahu stamp run ini walau backup gagal)"
    # dan diteruskan ke rollback.sh
    assert "PRE_DEPLOY_STAMP='$PRE_DEPLOY_STAMP'" in raw
    # environment production tetap (lapis kedua proteksi deployment)
    assert re.search(r"environment:\s*\n\s+name:\s*production", raw)


def test_skrip_stamp_pipefail_kontrak_dan_format():
    raw = STAMP_SH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in raw
    assert "RUN_ID:?RUN_ID wajib" in raw and "RUN_ATTEMPT:?RUN_ATTEMPT wajib" in raw
    assert "run-${RUN_ID}-attempt-${RUN_ATTEMPT}" in raw, \
        "format stamp wajib memuat run_id dan run_attempt"
    assert re.search(r"grep -Eq '\^\[0-9\]\+\$'", raw), \
        "RUN_ID/RUN_ATTEMPT wajib divalidasi numerik"


def test_charset_gate_filename_safe_konsisten_ketiga_skrip():
    """Gate filename-safe yang sama (charset [0-9A-Za-z_-]) wajib ada di
    generator stamp, backup_db.sh, dan rollback.sh — stamp tidak mungkin
    membawa karakter liar ke nama file walau satu lapis bolong."""
    for sh in (STAMP_SH, BACKUP_SH, ROLLBACK_SH):
        raw = sh.read_text(encoding="utf-8")
        assert "*[!0-9A-Za-z_-]*" in raw, \
            f"{sh.name} wajib memvalidasi charset filename-safe stamp"


# ======================================================= 2. PERILAKU STAMP
def _gen_stamp(run_id, attempt):
    return subprocess.run(
        ["bash", str(STAMP_SH)],
        env={**os.environ, "RUN_ID": str(run_id), "RUN_ATTEMPT": str(attempt)},
        capture_output=True, text=True)


def test_stamp_deterministik_dan_unik_antar_run_attempt():
    r1 = _gen_stamp(4211, 1)
    assert r1.returncode == 0, r1.stderr
    assert r1.stdout.strip() == "run-4211-attempt-1"
    # deterministik: (run_id, attempt) sama -> stamp sama
    assert _gen_stamp(4211, 1).stdout.strip() == r1.stdout.strip()
    # re-run (attempt naik) -> stamp BARU: backup lama tidak tertimpa
    r2 = _gen_stamp(4211, 2)
    assert r2.returncode == 0
    assert r2.stdout.strip() == "run-4211-attempt-2"
    assert r2.stdout.strip() != r1.stdout.strip()
    # run lain -> stamp lain
    rx = _gen_stamp(9999, 1)
    assert rx.stdout.strip() == "run-9999-attempt-1"
    assert rx.stdout.strip() != r1.stdout.strip()


def test_stamp_unik_dan_deterministik_konkuren_60_pasangan():
    """40 pasangan (run_id, attempt) + 5 pasangan ULANG — dijalankan
    KONKUREN (12 worker) sehingga semuanya jatuh pada detik-detik yang
    SAMA (persis kondisi yang membuat skema timestamp detik lama bisa
    bertabrakan). Hasil wajib: stamp unik untuk pasangan berbeda,
    deterministik untuk pasangan sama, semua filename-safe."""
    pairs = [(1000 + i, (i % 3) + 1) for i in range(40)]
    dup = pairs[:5]
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(lambda p: _gen_stamp(*p), pairs + dup))
    assert all(r.returncode == 0 for r in results), \
        [r.stderr for r in results if r.returncode != 0]
    stamps = [r.stdout.strip() for r in results]
    first, again = stamps[:len(pairs)], stamps[len(pairs):]
    # pasangan BERBEDA -> stamp BERBEDA (unik)
    assert len(set(first)) == len(pairs), \
        "stamp wajib unik per (run_id, attempt) — tidak boleh ada tabrakan"
    # pasangan SAMA -> stamp SAMA (deterministik)
    for p, s in zip(dup, again):
        assert dict(zip(pairs, first))[p] == s, f"stamp {p} tidak deterministik"
    for s in stamps:
        assert re.match(r"^run-\d+-attempt-\d+$", s), s
        assert re.match(r"^[0-9A-Za-z_-]+$", s), f"tidak filename-safe: {s}"
        assert "/" not in s and ".." not in s, s


def test_stamp_menolak_input_kosong_non_numerik_dan_karakter_aneh():
    for rid, att in (("", "1"), ("42", ""), ("abc", "1"), ("42", "x"),
                     ("42 2", "1"), ("42;", "1"), ("$(id)", "1")):
        r = _gen_stamp(rid, att)
        assert r.returncode != 0, f"RUN_ID={rid!r}/RUN_ATTEMPT={att!r} wajib ditolak"
        # pesan error boleh ke stdout (konvensi ::error:: GitHub) — yang
        # WAJIB: tidak ada stamp valid yang keluar
        assert not re.match(r"^run-\d+-attempt-\d+$", r.stdout.strip()), \
            f"stamp tidak boleh keluar saat input tidak valid ({rid!r},{att!r})"


def test_stamp_generator_diterima_penuh_backup_db_sh(tmp_path):
    """Stamp hasil generator langsung dipakai backup_db.sh: diterima,
    nama file final memuat run_id+attempt, LAST_PRE_DEPLOY = stamp itu."""
    bk, bin_dir, log, env = _dunia_backup(tmp_path)
    _stub_sudo(bin_dir, log)
    _stub(bin_dir, "pg_dump", log,
          f"cat << 'EOF'\n{DUMP_HEAD}CREATE TABLE customers (id int);\nEOF")
    stamp = _gen_stamp(42, 1).stdout.strip()
    assert stamp == "run-42-attempt-1"
    r = _backup_run(bk, env, stamp, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    final = bk / f"pre-deploy-{stamp}.sql.gz"
    assert final.exists(), "file final wajib memuat stamp run_id+attempt"
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == stamp


# =========================================== 3. PERILAKU BACKUP (konkuren)
def _dunia_backup(tmp_path: Path):
    bk = tmp_path / "var" / "backups" / "transcendence"
    bk.mkdir(parents=True)
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    env = {
        "BACKUP_DIR": str(bk),
        "BACKUP_OWNER": str(os.getuid()),   # install -d -o <uid-sendiri> OK
        "DB_NAME": "transcendence",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }
    return bk, bin_dir, log, env


def _stub(bin_dir: Path, name: str, log: Path, body: str):
    p = bin_dir / name
    p.write_text(f'#!/bin/bash\necho "{name} $*" >> {log}\n{body}\n')
    p.chmod(0o755)


def _stub_sudo(bin_dir: Path, log: Path):
    p = bin_dir / "sudo"
    p.write_text('#!/bin/bash\n'
                 f'echo "sudo $*" >> {log}\n'
                 'if [ "$1" = "-u" ]; then shift 2; fi\n'
                 'exec "$@"\n')
    p.chmod(0o755)


def _backup_run(bk, env, stamp, tmp_path, extra=None):
    e = {**os.environ, **env, "STAMP": stamp, **(extra or {})}
    return subprocess.run(["bash", str(BACKUP_SH)], env=e,
                          capture_output=True, text=True, cwd=str(tmp_path))


def test_dua_backup_konkuren_stamp_beda_tidak_saling_menimpa(tmp_path):
    """Defense-in-depth: BAHKAN bila serialisasi antrian concurrency
    dilanggar, stamp unik per run membuat backup dua run TIDAK mungkin
    menimpa satu sama lain (skema timestamp detik lama bisa: dua run
    pada detik sama -> satu nama file -> isi saling menginjak)."""
    bk, bin_dir, log, env = _dunia_backup(tmp_path)
    _stub_sudo(bin_dir, log)
    # pg_dump: header dump PG + marker identitas run (via env)
    _stub(bin_dir, "pg_dump", log,
          "cat << EOF\n" + DUMP_HEAD + "$PGDUMP_MARKER\nEOF")
    s1 = _gen_stamp(111, 1).stdout.strip()
    s2 = _gen_stamp(222, 1).stdout.strip()
    assert s1 != s2

    def run_one(args):
        stamp, marker = args
        return _backup_run(bk, env, stamp, tmp_path,
                           extra={"PGDUMP_MARKER": marker})

    with ThreadPoolExecutor(max_workers=2) as ex:
        rs = list(ex.map(run_one, [(s1, "MARKER-RUN-111"),
                                   (s2, "MARKER-RUN-222")]))
    assert all(r.returncode == 0 for r in rs), \
        [r.stdout + r.stderr for r in rs]
    # kedua dump final ADA, masing-masing VALID, isi = milik run-nya
    for stamp, marker in ((s1, "MARKER-RUN-111"), (s2, "MARKER-RUN-222")):
        final = bk / f"pre-deploy-{stamp}.sql.gz"
        assert final.exists()
        isi = gzip.decompress(final.read_bytes()).decode()
        assert "PostgreSQL database dump" in isi
        assert marker in isi, \
            f"isi dump {stamp} bukan milik run-nya — backup tertimpa run lain"
    isi1 = gzip.decompress((bk / f"pre-deploy-{s1}.sql.gz").read_bytes())
    isi2 = gzip.decompress((bk / f"pre-deploy-{s2}.sql.gz").read_bytes())
    assert isi1 != isi2, "dua run berbeda tidak boleh menghasilkan dump identik"
    # tidak ada .part tersisa dari kedua run
    assert not list(bk.glob("*part"))
    # LAST_PRE_DEPLOY = salah satu stamp, dan dump stamp itu ADA
    last = (bk / "LAST_PRE_DEPLOY").read_text().strip()
    assert last in (s1, s2), f"LAST_PRE_DEPLOY '{last}' bukan stamp kedua run"
    assert (bk / f"pre-deploy-{last}.sql.gz").exists()


# ============================================ 4. PERILAKU ROLLBACK (stamp)
def test_scenario_attempt1_ok_attempt2_gagal_rollback_hanya_run_sama(tmp_path):
    """INTI ROUND-5 (kontrak round-4 pada stamp baru):
    attempt-1 backup OK -> attempt-2 (re-run run yang sama) backup GAGAL
    -> rollback dengan stamp attempt-2 WAJIB MENOLAK walau dump
    attempt-1 + LAST_PRE_DEPLOY valid tersedia (backup run SEBELUMNYA
    bukan backup run terbaru) -> rollback dengan stamp attempt-1 (milik
    run yang backup-nya sukses) me-restore PERSIS dump attempt-1."""
    bk = tmp_path / "var" / "backups" / "transcendence"
    bk.mkdir(parents=True)
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir()
    blog = tmp_path / "backup-calls.log"
    rlog = tmp_path / "rollback-calls.log"

    _stub_sudo(bin_dir, blog)
    pd = bin_dir / "pg_dump"
    pd.write_text(f'#!/bin/bash\necho "pg_dump $*" >> {blog}\n'
                  "cat << EOF\n" + DUMP_HEAD + "$PGDUMP_MARKER\nEOF")
    pd.chmod(0o755)
    env_bk = {"BACKUP_DIR": str(bk), "BACKUP_OWNER": str(os.getuid()),
              "DB_NAME": "transcendence",
              "PATH": f"{bin_dir}:/usr/bin:/bin"}

    # --- attempt-1: backup SUKSES (dump valid terpublikasi)
    s1 = _gen_stamp(77, 1).stdout.strip()
    r1 = _backup_run(bk, env_bk, s1, tmp_path,
                     extra={"PGDUMP_MARKER": "attempt-satu"})
    assert r1.returncode == 0, r1.stdout + r1.stderr
    dump1 = bk / f"pre-deploy-{s1}.sql.gz"
    assert dump1.exists()
    assert "attempt-satu" in gzip.decompress(dump1.read_bytes()).decode()
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == s1

    # --- attempt-2 (re-run): backup GAGAL (pg_dump keluar KOSONG)
    s2 = _gen_stamp(77, 2).stdout.strip()
    assert s2 != s1
    pd.write_text(f'#!/bin/bash\necho "pg_dump $*" >> {blog}\nexit 0\n')
    pd.chmod(0o755)
    r2 = _backup_run(bk, env_bk, s2, tmp_path,
                     extra={"PGDUMP_MARKER": "attempt-dua"})
    assert r2.returncode != 0, "attempt-2 wajib gagal (dump kosong ditolak)"
    assert not (bk / f"pre-deploy-{s2}.sql.gz").exists()
    assert (bk / "LAST_PRE_DEPLOY").read_text().strip() == s1, \
        "LAST_PRE_DEPLOY tetap attempt-1 — backup lama tidak dianggap run terbaru"

    # --- dunia rollback (stub systemctl/git/psql/dst.)
    # PREVIOUS_COMMIT tercatat (deployment normal, BUKAN first-deploy)
    (bk / "PREVIOUS_COMMIT").write_text("abc1234\n")
    app = tmp_path / "opt" / "transcendence-hlwon-"
    (app / "backend").mkdir(parents=True, exist_ok=True)
    (app / "backend" / "requirements.txt").write_text("flask\n")
    vbin = app / "backend" / ".venv" / "bin"
    vbin.mkdir(parents=True, exist_ok=True)
    (vbin / "activate").write_text("# venv stub\n")
    for name, body in {"systemctl": "", "dropdb": "", "createdb": "",
                       "psql": "cat > /dev/null", "git": "", "pip": "",
                       "curl": "echo 200"}.items():
        p = bin_dir / name
        p.write_text(f'#!/bin/bash\necho "{name} $*" >> {rlog}\n{body}\n')
        p.chmod(0o755)
    g = bin_dir / "gunzip"
    g.write_text(f'#!/bin/bash\necho "gunzip $*" >> {rlog}\n'
                 'exec /bin/gunzip "$@"\n')
    g.chmod(0o755)
    envf = tmp_path / "backend.env"
    envf.write_text("DATABASE_URL=postgresql://app:pw@127.0.0.1/transcendence\n")
    env_rb = {"APP_DIR": str(app), "VENV_DIR": str(vbin.parent),
              "SERVICE": "transcendence-backend", "ENV_FILE": str(envf),
              "BACKUP_DIR": str(bk), "DB_NAME": "transcendence",
              "RESTORE_DB_NAME": "transcendence_restore",
              "HEALTH_URL": "http://127.0.0.1:8000/api/v1/health",
              "PATH": f"{bin_dir}:/usr/bin:/bin"}

    # --- rollback dengan stamp attempt-2 (run yang GAGAL ini):
    #     dump attempt-1 ADA + valid, tetapi WAJIB DITOLAK
    rr = subprocess.run(["bash", str(ROLLBACK_SH)],
                        env={**os.environ, **env_rb, "PRE_DEPLOY_STAMP": s2},
                        capture_output=True, text=True, cwd=str(tmp_path))
    assert rr.returncode == 1, "rollback attempt-2 wajib menolak"
    calls = rlog.read_text() if rlog.exists() else ""
    assert "gunzip" not in calls and "psql" not in calls \
        and "createdb" not in calls, "dump attempt-1 tidak boleh di-restore"
    assert "systemctl" not in calls, "service tidak boleh disentuh"
    assert "git checkout" not in calls, "kode tidak boleh di-checkout"
    assert envf.read_text() == \
        "DATABASE_URL=postgresql://app:pw@127.0.0.1/transcendence\n", \
        ".env tidak boleh berubah"
    assert "backup LAMA tidak boleh dipakai" in rr.stdout
    assert "intervensi manual" in rr.stdout.lower()

    # --- rollback dengan stamp attempt-1 (backup-nya SUKSES):
    #     me-restore PERSIS dump milik attempt-1
    rlog.unlink(missing_ok=True)
    rok = subprocess.run(["bash", str(ROLLBACK_SH)],
                         env={**os.environ, **env_rb, "PRE_DEPLOY_STAMP": s1},
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert rok.returncode == 0, rok.stdout + rok.stderr
    calls = rlog.read_text()
    assert f"gunzip -c {bk}/pre-deploy-{s1}.sql.gz" in calls, \
        "rollback wajib memakai dump milik attempt-1"
    assert f"pre-deploy-{s2}.sql.gz" not in calls
    assert "psql -q transcendence_restore" in calls
    assert "ROLLBACK OK" in rok.stdout
