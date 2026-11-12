"""F2/B2 — CHILD PROCESS TERMINATION di helper.

Runner.run() menjalankan tiap perintah di process group sendiri dan
mematikan SELURUH grup (plus turunan yang lolos lewat setsid, di proses
helper) saat selesai, batas waktu, atau sinyal. Diuji dengan proses helper
main() SUNGGUHAN + systemctl palsu (butuh euid 0; dilewati di CI non-root)
dan uji Runner in-process (tanpa root). Sandbox lokal, bukan staging."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from provisioner import mt5_provision_helper as H

PA = "a" * 32
ROOT = str(Path(H.__file__).parent.parent)
needs_root = pytest.mark.skipif(os.geteuid() != 0,
                                reason="main() helper mewajibkan euid 0")

SCRIPTS = {
    # satu proses (exec) — seperti binary systemctl
    "single": '[ "$1" = daemon-reload ] && exec sleep 30\n',
    # shell + cucu yang menulis berkas belakangan
    "grandchild": 'if [ "$1" = daemon-reload ]; then'
                  ' (sleep 3; echo x > {fs}) & echo $! >> {pids}; wait; fi\n',
    # anak & cucu mengabaikan SIGTERM
    "ignore_term": 'trap "" TERM\nif [ "$1" = daemon-reload ]; then'
                   ' (trap "" TERM; sleep 3; echo x > {fs}) &'
                   ' echo $! >> {pids}; wait; fi\n',
    # cucu keluar dari process group (setsid) lalu menulis berkas
    "setsid": 'if [ "$1" = daemon-reload ]; then'
              ' setsid sh -c "echo \\$\\$ >> {pids}; sleep 3; echo x > {fs}" &'
              ' sleep 30; fi\n',
    # selesai cepat
    "fast": "",
}


def _alive(pid):
    try:
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1] \
            .split()[0] != "Z"
    except OSError:
        return False


def _holds(pid, lock):
    try:
        return any(os.path.realpath(f"/proc/{pid}/fd/{f}") == str(lock)
                   for f in os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        return False


class Env:
    def __init__(self, tmp_path, kind, op_timeout=60):
        self.d = tmp_path
        tpl = tmp_path / "template"
        tpl.mkdir()
        (tpl / "terminal64.exe").write_text("bin")
        for n in ("terminals", "units"):
            (tmp_path / n).mkdir()
        self.pids, self.fs = tmp_path / "child.pids", tmp_path / "fs-after"
        sc = tmp_path / "fake-systemctl"
        sc.write_text("#!/bin/sh\necho $$ >> {pids}\n".format(pids=self.pids)
                      + SCRIPTS[kind].format(pids=self.pids, fs=self.fs)
                      + '[ "$1" = is-active ] && exit 3\nexit 0\n')
        sc.chmod(0o755)
        self.cfgf = tmp_path / "prov.json"
        self.cfgf.write_text(json.dumps({
            "base_dir": str(tmp_path / "terminals"),
            "template_dir": str(tpl), "unit_dir": str(tmp_path / "units"),
            "service_user": "root", "allow_shared_service_user": True, "port_base": 18812, "max_slots": 4,
            "systemctl": str(sc), "lock_dir": str(tmp_path / "locks"),
            "op_timeout_seconds": op_timeout, "lock_wait_seconds": 0.2}))
        self.cfgf.chmod(0o644)
        self.cfg = H.HelperConfig.load(str(self.cfgf))
        self.lock = tmp_path / "locks" / "C001.lock"

    def start(self):
        code = (f"import sys; sys.path.insert(0, {ROOT!r})\n"
                "from provisioner import mt5_provision_helper as H\n"
                "sys.exit(H.main(['install','--code','C001','--port','18812',"
                f"'--provision-id','{PA}'], config_path={str(self.cfgf)!r}))")
        return subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE, text=True)

    def wait_children(self, n=1, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.pids.exists() and len(self.pids.read_text().split()) >= n:
                time.sleep(0.2)
                return [int(x) for x in self.pids.read_text().split()]
            time.sleep(0.05)
        raise AssertionError("anak tidak pernah mulai")

    def inspect(self):
        return H.op_inspect(self.cfg, "C001", PA)


def _assert_all_stopped(env, kids, pr, out):
    assert pr.returncode == H.EXIT_INTERRUPTED, out
    assert json.loads(out.strip().splitlines()[-1])["code"] == 5
    for k in kids:
        assert not _alive(k), f"turunan {k} masih hidup"
        assert not _holds(k, env.lock)
    ins = env.inspect()
    assert ins["busy"] is False and ins["phase"] == "incomplete"
    time.sleep(3.5)                     # melewati waktu tulis cucu
    assert not env.fs.exists(), "turunan menulis setelah helper berhenti"
    assert not (env.d / "terminals" / "C001" / H.COMPLETE).exists()


@needs_root
@pytest.mark.parametrize("kind,nkids", [("single", 1), ("grandchild", 2),
                                        ("ignore_term", 2), ("setsid", 2)])
def test_sigterm_saat_anak_aktif(tmp_path, kind, nkids):
    env = Env(tmp_path, kind)
    pr = env.start()
    kids = env.wait_children(nkids)
    during = env.inspect()
    assert during["busy"] is True and during["phase"] == "incomplete"
    assert any(_holds(k, env.lock) for k in kids)   # anak memegang fd lock
    pr.send_signal(signal.SIGTERM)
    out, _ = pr.communicate(timeout=20)
    _assert_all_stopped(env, kids, pr, out)


@needs_root
@pytest.mark.parametrize("kind,nkids", [("single", 1), ("grandchild", 2),
                                        ("ignore_term", 2), ("setsid", 2)])
def test_sigalrm_batas_waktu_saat_anak_aktif(tmp_path, kind, nkids):
    env = Env(tmp_path, kind, op_timeout=2)
    pr = env.start()
    kids = env.wait_children(nkids)
    assert env.inspect()["busy"] is True
    t0 = time.monotonic()
    out, _ = pr.communicate(timeout=20)             # berhenti sendiri (SIGALRM)
    assert time.monotonic() - t0 < 6
    _assert_all_stopped(env, kids, pr, out)


@needs_root
def test_anak_sudah_selesai_sebelum_sinyal(tmp_path):
    env = Env(tmp_path, "fast")
    pr = env.start()
    out, _ = pr.communicate(timeout=20)
    assert pr.returncode == 0, out
    kids = [int(x) for x in env.pids.read_text().split()]
    assert kids and not any(_alive(k) for k in kids)
    ins = env.inspect()
    assert ins["busy"] is False and ins["phase"] == "complete"
    # sinyal setelah selesai tidak mengubah hasil
    with pytest.raises(ProcessLookupError):
        os.kill(pr.pid, signal.SIGTERM)
    assert env.inspect()["phase"] == "complete"


@needs_root
def test_lock_tertahan_sampai_seluruh_proses_berhenti(tmp_path):
    """Helper di-SIGKILL (tidak sempat membersihkan): turunan masih hidup ->
    lock TETAP tertahan (inspect busy, install lain SIBUK, API =
    helper_running). Setelah turunan berhenti -> lock bebas."""
    env = Env(tmp_path, "single")
    pr = env.start()
    kids = env.wait_children(1)
    pr.kill()
    pr.wait()
    assert _alive(kids[0]) and _holds(kids[0], env.lock)
    assert env.inspect()["busy"] is True
    with pytest.raises(H.HelperError) as ei:
        H.dispatch(env.cfg, ["remove", "--code", "C001", "--provision-id", PA])
    assert ei.value.code == H.EXIT_BUSY
    from services import provisioner as P

    class C:
        rpyc_port_base = 18812
        max_customers = 4
        provisioner_helper = "/x"
        provisioner_sudo = "/usr/bin/sudo"
        provisioner_timeout_seconds = 1
    hp = P.HelperProvisioner(C())
    hp._spawn = lambda argv, t: (0, json.dumps(env.inspect()))
    assert hp._inspect_state(P.TerminalSpec("C001", 18812, PA)) == \
        "helper_running"                             # API: BUKAN selesai
    os.kill(kids[0], signal.SIGKILL)
    end = time.monotonic() + 5
    while _alive(kids[0]) and time.monotonic() < end:
        time.sleep(0.05)
    assert env.inspect()["busy"] is False


@needs_root
def test_proses_lain_tidak_ikut_dimatikan(tmp_path):
    """Proses yang BUKAN turunan helper (mis. terminal customer lain) tetap
    hidup walau helper mematikan seluruh turunannya."""
    lain = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        env = Env(tmp_path, "setsid")
        pr = env.start()
        kids = env.wait_children(2)
        pr.send_signal(signal.SIGTERM)
        pr.communicate(timeout=20)
        assert not any(_alive(k) for k in kids)
        assert lain.poll() is None                   # tidak tersentuh
    finally:
        lain.kill()
        lain.wait()


# ------------------------------------------------------------- Runner in-process
def test_runner_mewariskan_fd_lock_dan_tanpa_shell(tmp_path):
    lock = tmp_path / "l.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    out = tmp_path / "fds"
    sh = tmp_path / "cek.sh"
    sh.write_text(f"#!/bin/sh\nls -l /proc/$$/fd > {out}\n")
    sh.chmod(0o755)
    r = H.Runner()
    r.pass_fds = (fd,)
    assert r.run([str(sh)]) == 0
    assert str(lock) in out.read_text()              # anak mewarisi fd lock
    os.close(fd)
    with pytest.raises(H.HelperError):
        H.Runner().run(["true"])                     # wajib path absolut


def test_runner_batas_waktu_mematikan_seluruh_grup(tmp_path):
    pids = tmp_path / "pids"
    fs = tmp_path / "fs"
    sh = tmp_path / "lama.sh"
    sh.write_text(f"#!/bin/sh\n(sleep 2; echo x > {fs}) & echo $! > {pids}\n"
                  "sleep 30\n")
    sh.chmod(0o755)
    r = H.Runner()
    r.timeout = 0.5
    with pytest.raises(H.HelperError) as ei:
        r.run([str(sh)])
    assert ei.value.code == H.EXIT_FAIL and "batas waktu" in ei.value.msg
    cucu = int(pids.read_text())
    end = time.monotonic() + 2          # SIGKILL sudah dikirim; tunggu efeknya
    while _alive(cucu) and time.monotonic() < end:
        time.sleep(0.02)
    assert not _alive(cucu)
    time.sleep(2.5)
    assert not fs.exists()


def test_runner_selesai_normal_mematikan_sisa_grup(tmp_path):
    pids = tmp_path / "pids"
    fs = tmp_path / "fs"
    sh = tmp_path / "sisa.sh"
    sh.write_text(f"#!/bin/sh\n(sleep 2; echo x > {fs}) & echo $! > {pids}\n"
                  "exit 0\n")
    sh.chmod(0o755)
    assert H.Runner().run([str(sh)]) == 0
    cucu = int(pids.read_text())
    end = time.monotonic() + 2          # SIGKILL sudah dikirim; tunggu efeknya
    while _alive(cucu) and time.monotonic() < end:
        time.sleep(0.02)
    assert not _alive(cucu)
    time.sleep(2.5)
    assert not fs.exists()


def test_sapuan_turunan_tidak_aktif_di_luar_proses_helper():
    """In-process (uji/pustaka): anak proses pemanggil yang tidak terkait
    TIDAK boleh dimatikan oleh Runner."""
    assert H._HELPER_PROCESS is False
    lain = subprocess.Popen(["/bin/sleep", "30"])
    try:
        assert H.Runner().run(["/bin/true"]) == 0
        assert lain.poll() is None
    finally:
        lain.kill()
        lain.wait()
