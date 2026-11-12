"""Remediasi review keamanan B2+B3: H1 (SIGCHLD), M1 (subreaper gagal),
M2 (turunan tersisa), M3 (pemimpin tidak dapat dituai).

Uji 'main()' memakai proses helper SUNGGUHAN (butuh euid 0 — DILEWATI di CI
non-root, tidak boleh diklaim lulus di sana); uji in-process berjalan tanpa
root. Sandbox lokal, bukan staging."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from provisioner import mt5_provision_helper as H
from services import provisioner as P

PA = "a" * 32
ROOT = str(Path(H.__file__).parent.parent)
needs_root = pytest.mark.skipif(os.geteuid() != 0,
                                reason="main() helper mewajibkan euid 0")

ESCAPE = ('if [ "$1" = daemon-reload ]; then setsid sh -c "echo \\$\\$ >'
          ' {d}/esc.pid; sleep 3; echo x > {d}/fs-after" & fi\n')
DAEMON = ('if [ "$1" = daemon-reload ]; then setsid python3 -c "import os,time\n'
          'os.closerange(3,256)\n'
          'open(\'{d}/esc.pid\',\'w\').write(str(os.getpid()))\n'
          'time.sleep(3)\nopen(\'{d}/fs-after\',\'w\').write(\'x\')" &'
          ' sleep 0.5; fi\n')


def _mk(tmp_path, script=""):
    d = tmp_path
    tpl = d / "template"
    tpl.mkdir()
    (tpl / "terminal64.exe").write_text("bin")
    for n in ("terminals", "units"):
        (d / n).mkdir()
    sc = d / "fake-systemctl"
    sc.write_text("#!/bin/sh\necho $1 >> {d}/sc.calls\n".format(d=d)
                  + script.format(d=d)
                  + '[ "$1" = is-active ] && exit 3\nexit 0\n')
    sc.chmod(0o755)
    cfgf = d / "prov.json"
    cfgf.write_text(json.dumps({
        "base_dir": str(d / "terminals"), "template_dir": str(tpl),
        "unit_dir": str(d / "units"), "service_user": "root", "allow_shared_service_user": True,
        "port_base": 18812, "max_slots": 4, "systemctl": str(sc),
        "lock_dir": str(d / "locks"), "op_timeout_seconds": 30,
        "lock_wait_seconds": 0.2}))
    cfgf.chmod(0o644)
    return cfgf, H.HelperConfig.load(str(cfgf))


def _helper(cfgf, pre="", op="install"):
    args = {"install": ["install", "--code", "C001", "--port", "18812",
                        "--provision-id", PA],
            "inspect": ["inspect", "--code", "C001"]}[op]
    code = (f"import os, signal, sys, time; sys.path.insert(0, {ROOT!r})\n"
            "from provisioner import mt5_provision_helper as H\n" + pre +
            f"\nsys.exit(H.main({args!r}, config_path={str(cfgf)!r}))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=60)
    return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])


def _alive(pid):
    return H._proc_start(pid) is not None


# ================================================================ H1
@needs_root
@pytest.mark.parametrize("pre", ["", "signal.signal(signal.SIGCHLD,"
                                     " signal.SIG_IGN)"])
def test_h1_main_sigchld_default_dan_diabaikan_sama_sama_benar(tmp_path, pre):
    """SIGCHLD diwarisi SIG_IGN -> main() mengembalikannya ke SIG_DFL sebelum
    membuat anak; hasil sama dengan SIGCHLD default (dulu: exit 4)."""
    cfgf, cfg = _mk(tmp_path)
    rc, out = _helper(cfgf, pre=pre)
    assert rc == 0 and out["ok"] is True, out
    assert H.op_inspect(cfg, "C001", PA)["phase"] == "complete"


def test_h1_runner_menolak_sigchld_diabaikan_sebelum_membuat_anak(tmp_path):
    mark = tmp_path / "jalan"
    sh = tmp_path / "x.sh"
    sh.write_text(f"#!/bin/sh\necho x > {mark}\n")
    sh.chmod(0o755)
    old = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    try:
        with pytest.raises(H.HelperError) as ei:
            H.Runner().run([str(sh)])
    finally:
        signal.signal(signal.SIGCHLD, old)
    assert ei.value.code == H.EXIT_FAIL and "SIGCHLD" in ei.value.msg
    time.sleep(0.2)
    assert not mark.exists()                  # tidak ada anak yang dibuat


def test_h1_anak_hilang_tidak_pernah_killpg(monkeypatch):
    """waitid() melaporkan anak sudah dituai pihak lain (ChildProcessError):
    identitas PID/PGID tidak terjamin -> killpg TIDAK BOLEH dipanggil dan
    hasil TIDAK sukses."""
    calls = []
    monkeypatch.setattr(H.os, "killpg", lambda *a: calls.append(a))

    def gone(*a, **k):
        raise ChildProcessError()
    monkeypatch.setattr(H.os, "waitid", gone)
    with pytest.raises(H.HelperError) as ei:
        H.Runner().run(["/bin/true"])
    assert calls == []
    assert ei.value.code == H.EXIT_FAIL
    assert "tidak dapat dipastikan" in ei.value.msg


def test_h1_killpg_hanya_saat_pemimpin_belum_dituai(monkeypatch):
    """Bukti urutan: saat killpg dipanggil, pemimpin MASIH ada di tabel
    proses (berjalan/zombie milik kita) -> PID/PGID tidak mungkin sudah
    dipakai ulang proses lain."""
    seen = []
    real = os.killpg

    def spy(pgid, sig):
        st = open(f"/proc/{pgid}/stat").read().rsplit(")", 1)[1].split()
        seen.append((st[0], int(st[1]), int(st[2])))   # state, ppid, pgrp
        return real(pgid, sig)
    monkeypatch.setattr(H.os, "killpg", spy)
    assert H.Runner().run(["/bin/true"]) == 0
    state, ppid, pgrp = seen[0]
    assert ppid == os.getpid()                 # masih anak kita, belum dituai
    assert state in ("Z", "R", "S")


# ================================================================ M1
@needs_root
def test_m1_subreaper_gagal_operasi_ditolak_sebelum_perubahan(tmp_path):
    cfgf, cfg = _mk(tmp_path, ESCAPE)
    rc, out = _helper(cfgf, pre="H._set_subreaper = lambda: False")
    assert rc == H.EXIT_FAIL and out["ok"] is False
    assert "subreaper" in out["error"]
    assert not (tmp_path / "terminals" / "C001").exists()   # tidak disentuh
    assert not (tmp_path / "sc.calls").exists()             # tanpa anak
    assert not (tmp_path / "locks" / "C001.lock").exists()  # sebelum lock
    # operasi baca tetap tersedia
    rc, out = _helper(cfgf, pre="H._set_subreaper = lambda: False",
                      op="inspect")
    assert rc == 0 and out["phase"] == "absent"


@needs_root
@pytest.mark.parametrize("script", [ESCAPE, DAEMON])
def test_m1_turunan_setsid_dan_daemon_tutup_fd_dimatikan(tmp_path, script):
    """Subreaper aktif: turunan yang keluar dari process group — termasuk
    daemon yang MENUTUP fd lock warisan — mati sebelum helper menjawab ok."""
    cfgf, cfg = _mk(tmp_path, script)
    rc, out = _helper(cfgf)
    assert rc == 0 and out["ok"] is True, out
    pid = int((tmp_path / "esc.pid").read_text())
    assert not _alive(pid)
    assert H.op_inspect(cfg, "C001", PA)["busy"] is False
    time.sleep(3.5)
    assert not (tmp_path / "fs-after").exists()


# ================================================================ M2
@needs_root
def test_m2_turunan_tersisa_exit_7_bukan_sukses_dan_tetap_sibuk(tmp_path):
    """Sapuan melaporkan turunan yang TIDAK dapat dimatikan (disimulasikan
    dengan proses hidup nyata): helper exit 7, TIDAK {"ok": true}; customer
    tetap 'sibuk' selama proses itu ada; bebas setelah proses mati."""
    cfgf, cfg = _mk(tmp_path)
    stray = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        ident = (stray.pid, H._proc_start(stray.pid))
        rc, out = _helper(cfgf, pre=f"H.kill_descendants = lambda *a: "
                                    f"[{ident!r}]")
        assert rc == H.EXIT_UNCLEAN and out["ok"] is False, out
        assert (tmp_path / "locks" / "C001.unclean").exists()
        assert H.op_inspect(cfg, "C001", PA)["busy"] is True
        with pytest.raises(H.HelperError) as ei:
            H.dispatch(cfg, ["remove", "--code", "C001", "--provision-id",
                             PA])
        assert ei.value.code == H.EXIT_BUSY          # fail-closed
    finally:
        stray.kill()
        stray.wait()
    assert H.op_inspect(cfg, "C001", PA)["busy"] is False
    assert not (tmp_path / "locks" / "C001.unclean").exists()


def test_m2_catatan_unclean_pid_daur_ulang_tidak_dihitung(tmp_path):
    cfg = H.HelperConfig(base_dir=tmp_path / "b", template_dir=tmp_path,
                         unit_dir=tmp_path, service_user="x", port_base=1,
                         max_slots=1, lock_dir=tmp_path / "l")
    me = os.getpid()
    H.record_unclean(cfg, "C001", [(me, "starttime-lain")])   # PID sama, proses beda
    assert H.unclean_alive(cfg, "C001") is False
    H.record_unclean(cfg, "C001", [(me, H._proc_start(me))])
    assert H.unclean_alive(cfg, "C001") is True
    (tmp_path / "l" / "C001.unclean").write_text("rusak")
    assert H.unclean_alive(cfg, "C001") is True               # fail-closed


def test_m2_api_exit_7_selalu_tidak_pasti(monkeypatch):
    class C:
        rpyc_port_base = 18812
        max_customers = 4
        provisioner_helper = "/x"
        provisioner_sudo = "/usr/bin/sudo"
        provisioner_timeout_seconds = 1
    hp = P.HelperProvisioner(C())
    calls = []

    def spawn(argv, t):
        calls.append(argv[3])
        return 7, '{"ok": false, "code": 7, "error": "x"}'
    monkeypatch.setattr(hp, "_spawn", spawn)
    for op in ("install", "remove"):
        with pytest.raises(P.ProvisionTimeout) as ei:
            getattr(hp, op)(P.TerminalSpec("C001", 18812, PA))
        assert ei.value.state == "helper_running" and ei.value.uncertain
    assert "inspect" not in calls           # tidak bergantung hasil inspect


# ================================================================ M3
def test_m3_pemimpin_tidak_dapat_dituai_exit_7_dan_dicatat(monkeypatch):
    """p.wait() melewati batas (pemimpin tak dapat dituai): bukan sukses,
    identitas pemimpin dilaporkan, penantian DIBATASI."""
    real_wait = subprocess.Popen.wait
    seen = {}

    def stuck(self, timeout=None):
        seen["timeout"] = timeout
        raise subprocess.TimeoutExpired("x", timeout)
    got = []
    r = H.Runner()
    r.timeout = 0.3                 # pemimpin masih hidup saat pembersihan
    r.on_unclean = got.extend
    monkeypatch.setattr(subprocess.Popen, "wait", stuck)
    t0 = time.monotonic()
    with pytest.raises(H.HelperError) as ei:
        r.run(["/bin/sleep", "30"])
    monkeypatch.setattr(subprocess.Popen, "wait", real_wait)
    assert ei.value.code == H.EXIT_UNCLEAN
    assert seen["timeout"] == H.KILL_WAIT_SECONDS      # batas eksplisit
    assert time.monotonic() - t0 < 5
    assert len(got) == 1 and got[0][0] > 0
    try:
        os.waitpid(got[0][0], 0)                       # bersihkan anak uji
    except ChildProcessError:
        pass


@needs_root
def test_m3_main_pemimpin_macet_exit_7_dan_lock_tetap_tertutup(tmp_path):
    """Di proses helper: pemimpin 'tak dapat dituai' -> exit 7, bukan ok;
    customer tetap sibuk selama proses tercatat hidup."""
    cfgf, cfg = _mk(tmp_path, '[ "$1" = daemon-reload ] && exec sleep 20\n')
    pre = ("import subprocess\n"
           "def _stuck(self, timeout=None):\n"
           "    raise subprocess.TimeoutExpired('x', timeout)\n"
           "_real_kill = os.killpg\n"
           "H.os.killpg = lambda *a: None\n"          # SIGKILL 'tidak mempan'
           "subprocess.Popen.wait = _stuck\n"
           "H.Runner.timeout = 0.5\n"
           "H.kill_descendants = lambda *a: []\n")
    rc, out = _helper(cfgf, pre=pre)
    assert rc == H.EXIT_UNCLEAN and out["ok"] is False, out
    rec = json.loads((tmp_path / "locks" / "C001.unclean").read_text())
    pid = rec[0][0]
    try:
        assert _alive(pid)
        assert H.op_inspect(cfg, "C001", PA)["busy"] is True
        assert H.op_inspect(cfg, "C001", PA)["phase"] != "complete"
    finally:
        os.kill(pid, signal.SIGKILL)
    end = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < end:
        time.sleep(0.05)
    assert H.op_inspect(cfg, "C001", PA)["busy"] is False


# ================================================================ B5
# Temuan 1: kegagalan mencatat .unclean tidak boleh ditelan.
# Temuan 2: identitas TIDAK diketahui tetap UNCLEAN & sibuk; tidak dihapus.
def _b5cfg(tmp_path):
    return H.HelperConfig(base_dir=tmp_path / "b", template_dir=tmp_path,
                          unit_dir=tmp_path, service_user="x", port_base=1,
                          max_slots=1, lock_dir=tmp_path / "l",
                          lock_wait_seconds=0.1)


def test_b5_identitas_tidak_diketahui_sibuk_dan_tidak_dihapus(tmp_path):
    cfg = _b5cfg(tmp_path)
    H.record_unclean(cfg, "C001", [H.UNKNOWN_IDENT])
    marker = tmp_path / "l" / "C001.unclean"
    assert json.loads(marker.read_text()) == [[0, "?"]]
    for _ in range(3):                                   # tidak hilang sendiri
        assert H.unclean_alive(cfg, "C001") is True
    assert marker.exists()
    assert H.lock_busy(cfg, "C001") is True
    with pytest.raises(H.HelperError) as ei:             # mutasi ditolak
        H.acquire_lock(cfg, "C001")
    assert ei.value.code == H.EXIT_BUSY
    # entri baru digabung; ketidakpastian lama DIPERTAHANKAN
    H.record_unclean(cfg, "C001", [(os.getpid(), "starttime-lain")])
    assert [0, "?"] in json.loads(marker.read_text())
    assert H.unclean_alive(cfg, "C001") is True
    marker.unlink()                                      # pemulihan admin
    assert H.unclean_alive(cfg, "C001") is False


def test_b5_catatan_tidak_pernah_kosong(tmp_path):
    cfg = _b5cfg(tmp_path)
    H.record_unclean(cfg, "C001", [])                    # tanpa identitas
    marker = tmp_path / "l" / "C001.unclean"
    assert json.loads(marker.read_text()) == [[0, "?"]]
    marker.write_text("[]")                              # kosong = tak pasti
    assert H.unclean_alive(cfg, "C001") is True and marker.exists()
    marker.write_text('[[123, "?"]]')                    # starttime tak diketahui
    assert H.unclean_alive(cfg, "C001") is True and marker.exists()


def test_b5_runner_callback_gagal_tidak_ditelan(monkeypatch):
    guarded = []
    monkeypatch.setattr(H, "_guard", lambda surv: guarded.append(list(surv)))
    monkeypatch.setattr(subprocess.Popen, "wait", lambda self, timeout=None:
                        (_ for _ in ()).throw(
                            subprocess.TimeoutExpired("x", timeout)))
    r = H.Runner()
    r.timeout = 0.3

    def gagal(surv):
        raise OSError("disk penuh")
    r.on_unclean = gagal
    with pytest.raises(H.HelperError) as ei:
        r.run(["/bin/sleep", "30"])
    monkeypatch.undo()
    assert ei.value.code == H.EXIT_UNCLEAN               # tetap non-sukses
    assert len(guarded) == 1 and guarded[0][0][0] > 0    # pengaman dipanggil
    try:
        os.waitpid(guarded[0][0][0], 0)
    except ChildProcessError:
        pass


def test_b5_persist_gagal_tulis_menahan_dengan_lock(tmp_path, monkeypatch):
    """Marker tidak dapat ditulis & lock belum dipegang (jalur sapuan akhir):
    lock DIAMBIL lalu proses ditahan selama proses tersisa hidup."""
    import threading
    cfg = _b5cfg(tmp_path)
    stray = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    ident = (stray.pid, H._proc_start(stray.pid))
    monkeypatch.setattr(H, "record_unclean", lambda *a: (_ for _ in ())
                        .throw(OSError("read-only")))
    th = threading.Thread(target=H.persist_unclean,
                          args=(cfg, "C001", [ident], False), daemon=True)
    try:
        th.start()
        time.sleep(0.8)
        assert th.is_alive()                             # ditahan
        assert not (tmp_path / "l" / "C001.unclean").exists()
        assert H.lock_busy(cfg, "C001") is True          # lock dipegang
    finally:
        stray.kill()
        stray.wait()
    th.join(5)
    assert not th.is_alive()                             # pulih sendiri


@needs_root
def test_b5_main_marker_gagal_dan_turunan_tutup_fd_tetap_sibuk(tmp_path):
    """Helper SUNGGUHAN: turunan tersisa TIDAK memegang fd lock (seperti
    daemon yang menutupnya) dan .unclean tidak dapat ditulis -> helper tidak
    keluar & tidak menjawab ok; customer tetap sibuk; mutasi ditolak; pulih
    (exit 7) setelah proses tersisa mati."""
    cfgf, cfg = _mk(tmp_path)
    stray = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    ident = (stray.pid, H._proc_start(stray.pid))
    pre = (f"H.kill_descendants = lambda *a: [{ident!r}]\n"
           "def _gagal(*a):\n    raise OSError('tidak dapat menulis')\n"
           "H.record_unclean = _gagal\n")
    code = (f"import os, signal, sys, time; sys.path.insert(0, {ROOT!r})\n"
            "from provisioner import mt5_provision_helper as H\n" + pre +
            "sys.exit(H.main(['install','--code','C001','--port','18812',"
            f"'--provision-id','{PA}'], config_path={str(cfgf)!r}))")
    pr = subprocess.Popen([sys.executable, "-c", code],
                          stdout=subprocess.PIPE, text=True)
    try:
        time.sleep(2.0)
        assert pr.poll() is None                         # helper DITAHAN
        assert not (tmp_path / "locks" / "C001.unclean").exists()
        assert H.op_inspect(cfg, "C001", PA)["busy"] is True
        with pytest.raises(H.HelperError) as ei:
            H.dispatch(cfg, ["remove", "--code", "C001", "--provision-id",
                             PA])
        assert ei.value.code == H.EXIT_BUSY
        pr.send_signal(signal.SIGTERM)                   # tidak melepas pengaman
        time.sleep(0.5)
        assert pr.poll() is None
    finally:
        stray.kill()
        stray.wait()
    out, _ = pr.communicate(timeout=15)
    res = json.loads(out.strip().splitlines()[-1])
    assert pr.returncode == H.EXIT_UNCLEAN and res["ok"] is False
    assert H.op_inspect(cfg, "C001", PA)["busy"] is False


@needs_root
def test_b5_main_sapuan_gagal_marker_tidak_kosong_dan_tetap_sibuk(tmp_path):
    """kill_descendants GAGAL (pengecualian): status tidak diketahui ->
    exit 7, marker berisi identitas tak diketahui (tidak kosong), inspect
    sibuk terus, operasi mutasi berikutnya ditolak, marker tidak hilang."""
    cfgf, cfg = _mk(tmp_path)
    pre = ("def _rusak(*a):\n    raise RuntimeError('proc tidak terbaca')\n"
           "H.kill_descendants = _rusak\n")
    rc, out = _helper(cfgf, pre=pre)
    assert rc == H.EXIT_UNCLEAN and out["ok"] is False, out
    marker = tmp_path / "locks" / "C001.unclean"
    assert [0, "?"] in json.loads(marker.read_text())
    for _ in range(3):
        assert H.op_inspect(cfg, "C001", PA)["busy"] is True
    with pytest.raises(H.HelperError) as ei:
        H.dispatch(cfg, ["remove", "--code", "C001", "--provision-id", PA])
    assert ei.value.code == H.EXIT_BUSY
    rc2, out2 = _helper(cfgf)                            # helper berikutnya
    assert rc2 == H.EXIT_BUSY and out2["ok"] is False
    assert marker.exists()


# ================================================================ B6
# _proc_start() dulu menyamakan "tidak ada" dengan "tidak dapat dibaca".
# Kini: ALIVE / DEAD / ZOMBIE / UNKNOWN; UNKNOWN tidak pernah dianggap mati.
import errno  # noqa: E402


def _raises(exc):
    def f(pid):
        raise exc
    return f


def _ident_self():
    return (os.getpid(), H._proc_start(os.getpid()))


@pytest.mark.parametrize("exc,expect", [
    (FileNotFoundError(errno.ENOENT, "x"), "dead"),
    (ProcessLookupError(errno.ESRCH, "x"), "dead"),
    (PermissionError(errno.EACCES, "x"), "unknown"),
    (OSError(errno.EIO, "x"), "unknown"),
])
def test_b6_status_dari_galat_baca_proc(monkeypatch, exc, expect):
    monkeypatch.setattr(H, "_read_proc_stat", _raises(exc))
    assert H.proc_state(4242, "100") == expect
    assert H._survivors_pending([(4242, "100")]) is (expect == "unknown")


@pytest.mark.parametrize("raw", ["", "sampah", "1 (x) S 1",
                                 "1 (x) S " + "a " * 30,
                                 "1 (x) SS " + "1 " * 30])
def test_b6_format_stat_rusak_adalah_unknown(monkeypatch, raw):
    monkeypatch.setattr(H, "_read_proc_stat", lambda pid: raw)
    assert H.proc_state(4242, "100") == "unknown"
    assert H._survivors_pending([(4242, "100")]) is True


def test_b6_hidup_cocok_dan_pid_dipakai_ulang():
    pid, start = _ident_self()
    assert start and H.proc_state(pid, start) == "alive"
    assert H._survivors_pending([(pid, start)]) is True
    # PID sama, starttime berbeda = proses asli sudah mati (PID didaur ulang)
    assert H.proc_state(pid, str(int(start) + 1)) == "dead"
    assert H._survivors_pending([(pid, str(int(start) + 1))]) is False


def test_b6_proses_zombie_dan_proses_hilang():
    z = subprocess.Popen(["/bin/true"])
    try:
        end = time.monotonic() + 5
        while H._proc_probe(z.pid)[0] != "zombie" and time.monotonic() < end:
            time.sleep(0.02)
        st, start = H._proc_probe(z.pid)
        assert st == "zombie"
        assert H.proc_state(z.pid, start) == "zombie"
        assert H._survivors_pending([(z.pid, start)]) is False
        assert H._proc_start(z.pid) is None
    finally:
        z.wait()
    assert H.proc_state(z.pid, start) == "dead"            # ENOENT nyata
    assert H._survivors_pending([(z.pid, start)]) is False


@pytest.mark.parametrize("exc", [PermissionError(errno.EACCES, "x"),
                                 OSError(errno.EIO, "x")])
def test_b6_proc_tak_terbaca_marker_tetap_dan_mutasi_ditolak(tmp_path,
                                                             monkeypatch,
                                                             exc):
    """Kegagalan membaca /proc TIDAK membuat inspect bebas, TIDAK menghapus
    marker, dan TIDAK mengizinkan operasi mutasi."""
    cfg = _b5cfg(tmp_path)
    (tmp_path / "b").mkdir()
    H.record_unclean(cfg, "C001", [(4242, "100")])
    marker = tmp_path / "l" / "C001.unclean"
    monkeypatch.setattr(H, "_read_proc_stat", _raises(exc))
    for _ in range(3):
        assert H.unclean_alive(cfg, "C001") is True
        assert H.op_inspect(cfg, "C001", PA)["busy"] is True
    assert json.loads(marker.read_text()) == [[4242, "100"]]
    with pytest.raises(H.HelperError) as ei:
        H.dispatch(cfg, ["remove", "--code", "C001", "--provision-id", PA])
    assert ei.value.code == H.EXIT_BUSY
    # catatan baru TIDAK membuang entri lama yang statusnya tak pasti
    H.record_unclean(cfg, "C001", [(4343, "200")])
    assert [4242, "100"] in json.loads(marker.read_text())
    assert marker.exists()
    # setelah /proc terbaca & proses memang tidak ada -> bebas, marker hilang
    monkeypatch.setattr(H, "_read_proc_stat",
                        _raises(FileNotFoundError(errno.ENOENT, "x")))
    assert H.unclean_alive(cfg, "C001") is False and not marker.exists()
    assert H.op_inspect(cfg, "C001", PA)["busy"] is False


def test_b6_marker_unknown_tidak_pernah_terhapus_otomatis(tmp_path,
                                                          monkeypatch):
    cfg = _b5cfg(tmp_path)
    H.record_unclean(cfg, "C001", [H.UNKNOWN_IDENT, (4242, "100")])
    marker = tmp_path / "l" / "C001.unclean"
    monkeypatch.setattr(H, "_read_proc_stat",
                        _raises(FileNotFoundError(errno.ENOENT, "x")))
    for _ in range(5):                 # proses bernama sudah mati, tetapi
        assert H.unclean_alive(cfg, "C001") is True      # ada yg tak diketahui
    assert marker.exists()


def test_b6_guard_tidak_lepas_saat_proc_tak_terbaca(monkeypatch):
    """_guard() (pengaman terakhir) tetap menahan selama status UNKNOWN."""
    import threading
    monkeypatch.setattr(H, "_read_proc_stat",
                        _raises(OSError(errno.EIO, "x")))
    th = threading.Thread(target=H._guard, args=([(4242, "100")],),
                          daemon=True)
    th.start()
    time.sleep(1.2)
    assert th.is_alive()
    monkeypatch.setattr(H, "_read_proc_stat",
                        _raises(FileNotFoundError(errno.ENOENT, "x")))
    th.join(5)
    assert not th.is_alive()


# ================================================================ B7
# kill_descendants(): galat pemeriksaan proses tidak boleh diabaikan diam-
# diam. Dijalankan di PROSES TERPISAH (fungsi ini mematikan semua anak
# pemanggilnya) — tidak butuh root.
_B7 = """
import errno, json, os, signal, subprocess, sys, time
sys.path.insert(0, {root!r})
from provisioner import mt5_provision_helper as H
me = os.getpid()
kid = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
other = int(sys.argv[1])                     # proses yang BUKAN turunan
real_read, real_waitid = H._read_proc_stat, os.waitid
def alive(pid):
    return H._proc_probe(pid)[0] == "alive"
{patch}
t0 = time.monotonic()
left = H.kill_descendants({secs})
print(json.dumps({{"left": [list(x) for x in left], "kid": kid.pid,
                  "kid_alive": alive(kid.pid) if {probe_real} else None,
                  "other_alive": real_read(other).rsplit(")", 1)[1].split()[0] != "Z",
                  "took": round(time.monotonic() - t0, 2)}}))
try:
    kid.kill()
except Exception:
    pass
"""


def _b7(patch, secs=1.0, probe_real=True):
    other = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        code = _B7.format(root=ROOT, patch=patch, secs=secs,
                          probe_real=probe_real)
        r = subprocess.run([sys.executable, "-c", code, str(other.pid)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-800:]
        out = json.loads(r.stdout.strip().splitlines()[-1])
        out["other_poll"] = other.poll()
        return out
    finally:
        other.kill()
        other.wait()


_UNREADABLE_KID = """
def read(pid):
    if int(pid) == kid.pid:
        {raise_}
    return real_read(pid)
H._read_proc_stat = read
def alive(pid):
    try:
        os.kill(pid, 0); return real_read(pid).rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False
"""


@pytest.mark.parametrize("raise_", [
    "raise PermissionError(errno.EACCES, 'x')",
    "raise OSError(errno.EIO, 'x')",
    "return 'sampah tanpa kurung'",
    "return '1 (x) S ' + 'a ' * 30",
])
def test_b7_turunan_stat_tak_terbaca_tetap_dimatikan(raise_):
    """stat turunan tidak terbaca (EACCES/EIO/format rusak): TIDAK dianggap
    'bukan turunan' — diverifikasi lewat waitid(pidfd) lalu dimatikan."""
    out = _b7(_UNREADABLE_KID.format(raise_=raise_))
    assert out["kid_alive"] is False                 # turunan mati
    assert out["left"] == []                         # bersih setelah mati
    assert out["other_poll"] is None                 # bukan turunan: utuh


def test_b7_turunan_tak_dapat_diperiksa_tidak_pernah_daftar_kosong():
    """stat tak terbaca DAN waitid(pidfd) gagal: identitas tidak dapat
    dipastikan -> hasil TIDAK kosong (UNKNOWN_IDENT); proses tidak dimatikan
    secara membabi buta."""
    patch = _UNREADABLE_KID.format(
        raise_="raise OSError(errno.EIO, 'x')") + """
def wid(idtype, id_, opts):
    if idtype == os.P_PIDFD:
        raise OSError(errno.EIO, 'x')
    return real_waitid(idtype, id_, opts)
os.waitid = wid
"""
    out = _b7(patch, secs=0.6)
    assert [0, "?"] in out["left"]
    assert out["kid_alive"] is True                  # tidak ditebak-tebak
    assert out["other_poll"] is None


def test_b7_bukan_turunan_stat_tak_terbaca_tidak_dimatikan():
    """Proses LAIN (bukan turunan) yang stat-nya tidak terbaca: waitid(pidfd)
    -> ECHILD -> dilewati, TIDAK dimatikan, tidak dicatat."""
    patch = """
def read(pid):
    if int(pid) == other:
        raise PermissionError(errno.EACCES, 'x')
    return real_read(pid)
H._read_proc_stat = read
"""
    out = _b7(patch)
    assert out["other_poll"] is None and out["other_alive"] is True
    assert out["left"] == []
    assert out["kid_alive"] is False                 # turunan asli tetap mati


def test_b7_pid_dipakai_ulang_tidak_dimatikan():
    """Bacaan pertama: PID tampak anak helper; setelah pidfd dibuka identitas
    berbeda (PID dipakai ulang proses lain) -> TIDAK dimatikan."""
    patch = """
seen = {}
def read(pid):
    raw = real_read(pid)
    if int(pid) == other:
        seen[pid] = seen.get(pid, 0) + 1
        if seen[pid] % 2 == 1:                       # bacaan ke-1: palsu
            f = raw.rsplit(")", 1)
            rest = f[1].split()
            rest[1] = str(me); rest[19] = "1"        # PPid=helper, start lain
            return f[0] + ") " + " ".join(rest)
    return raw
H._read_proc_stat = read
"""
    out = _b7(patch)
    assert out["other_poll"] is None and out["other_alive"] is True
    assert out["left"] == []


def test_b7_pidfd_gagal_dibuka_turunan_tetap_tercatat():
    patch = """
real_open = os.pidfd_open
def po(pid, flags=0):
    if pid == kid.pid:
        raise OSError(errno.EMFILE, 'x')
    return real_open(pid, flags)
os.pidfd_open = po
"""
    out = _b7(patch, secs=0.6)
    assert len(out["left"]) == 1
    pid, start = out["left"][0]
    assert pid == out["kid"] and start.isdigit()     # identitas TERCATAT
    assert out["kid_alive"] is True


def test_b7_sinyal_gagal_terkirim_turunan_tetap_tercatat():
    patch = """
def send(fd, sig, *a):
    raise PermissionError(errno.EPERM, 'x')
signal.pidfd_send_signal = send
"""
    out = _b7(patch, secs=0.6)
    assert out["left"] and out["left"][0][0] == out["kid"]
    assert out["kid_alive"] is True


def test_b7_kontrol_positif_tanpa_galat():
    out = _b7("")
    assert out["left"] == [] and out["kid_alive"] is False
    assert out["other_poll"] is None


@needs_root
def test_b7_main_turunan_tak_terverifikasi_exit_7_bukan_sukses(tmp_path):
    """Helper SUNGGUHAN: pemeriksaan /proc gagal & waitid(pidfd) gagal ->
    kill_descendants tidak kosong -> exit 7 (bukan {"ok": true}), marker
    berisi identitas tak diketahui, inspect sibuk, mutasi berikutnya
    ditolak."""
    cfgf, cfg = _mk(tmp_path, ESCAPE)
    pre = ("import errno\n_rr, _rw = H._read_proc_stat, os.waitid\n"
           "def _read(pid):\n"
           "    if int(pid) != os.getpid():\n"
           "        raise OSError(errno.EIO, 'x')\n"
           "    return _rr(pid)\n"
           "def _wid(t, i, o):\n"
           "    if t == os.P_PIDFD:\n"
           "        raise OSError(errno.EIO, 'x')\n"
           "    return _rw(t, i, o)\n"
           "_kd = H.kill_descendants\n"
           "def _sweep(*a):\n"
           "    H._read_proc_stat, os.waitid = _read, _wid\n"
           "    try:\n        return _kd(0.5)\n"
           "    finally:\n        H._read_proc_stat, os.waitid = _rr, _rw\n"
           "H.kill_descendants = _sweep\n")
    rc, out = _helper(cfgf, pre=pre)
    assert rc == H.EXIT_UNCLEAN and out["ok"] is False, out
    marker = tmp_path / "locks" / "C001.unclean"
    assert [0, "?"] in json.loads(marker.read_text())
    assert H.op_inspect(cfg, "C001", PA)["busy"] is True
    with pytest.raises(H.HelperError) as ei:
        H.dispatch(cfg, ["remove", "--code", "C001", "--provision-id", PA])
    assert ei.value.code == H.EXIT_BUSY
    ep = tmp_path / "esc.pid"
    if ep.exists():                                   # bersihkan turunan uji
        try:
            os.kill(int(ep.read_text()), signal.SIGKILL)
        except OSError:
            pass
