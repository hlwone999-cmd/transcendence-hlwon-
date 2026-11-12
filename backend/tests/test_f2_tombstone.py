"""F2/T2 — penghapusan berbasis TOMBSTONE yang mempertahankan bukti
kepemilikan bila remove terputus (SIGTERM sebelum/sesudah rename, SIGKILL
saat rmtree, helper restart), tanpa pernah menyentuh direktori lain.

Helper ASLI; lock flock nyata. Uji sinyal memakai proses helper main()
sungguhan (butuh euid 0 — dilewati di CI non-root); varian in-process
(tanpa root) menguji logika yang sama. Bukan staging (tanpa systemd)."""
import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from provisioner import mt5_provision_helper as H
from services import provisioner as P
from tests.test_provision_helper import FakeRunner

PA, PB, PC = "a" * 32, "b" * 32, "c" * 32
ROOT = str(Path(H.__file__).parent.parent)
needs_root = pytest.mark.skipif(os.geteuid() != 0,
                                reason="main() helper mewajibkan euid 0")


@pytest.fixture()
def hcfg(tmp_path):
    tpl = tmp_path / "template"
    (tpl / "sub").mkdir(parents=True)
    for i in range(30):
        (tpl / f"f{i:02d}.bin").write_text("x" * (i + 1))
    (tpl / "sub" / "d.dat").write_text("data")
    (tmp_path / "terminals").mkdir()
    (tmp_path / "units").mkdir()
    return H.HelperConfig(base_dir=tmp_path / "terminals", template_dir=tpl,
                          unit_dir=tmp_path / "units", service_user="root",
                          port_base=18812, max_slots=4,
                          proc_root=tmp_path / "proc",
                          lock_dir=tmp_path / "locks", lock_wait_seconds=5)


def _inst(hcfg, code="C001", pid=PA, port=18812):
    return H.op_install(hcfg, code, port, pid, FakeRunner(), chown=False)


def _tree(*paths):
    h = hashlib.sha256()
    for base in paths:
        base = Path(base)
        if base.is_file():
            h.update(base.read_bytes())
            continue
        for root, dirs, files in sorted(os.walk(base)):
            dirs.sort()
            for f in sorted(files):
                fp = Path(root) / f
                h.update(str(fp).encode() + fp.read_bytes())
    return h.hexdigest()


@pytest.fixture()
def other(hcfg):
    """Customer LAIN (C002) terpasang lengkap; hash dipantau."""
    _inst(hcfg, "C002", PB, 18813)
    paths = (hcfg.base_dir / "C002",
             hcfg.unit_dir / "transcendence-mt5-c002.service")
    return lambda: _tree(*paths)


def _clean(hcfg, code="C001", pid=PA):
    tomb, manifest = H.tomb_paths(hcfg, code, pid)
    return (not (hcfg.base_dir / code).exists() and not tomb.exists()
            and not manifest.exists() and not (
                hcfg.unit_dir / f"transcendence-mt5-{code.lower()}.service")
            .exists())


def _remove(hcfg, pid=PA, code="C001"):
    return H.dispatch(hcfg, ["remove", "--code", code, "--provision-id", pid],
                      runner=FakeRunner())


# ------------------------------------------------------------- proses nyata
def _cfgfile(hcfg, tmp_path):
    sc = tmp_path / "fake-systemctl"           # unit TIDAK aktif setelah stop
    sc.write_text("#!/bin/sh\ncase \"$1\" in is-active) exit 3;; esac\n"
                  "exit 0\n")
    os.chmod(sc, 0o755)
    f = tmp_path / "prov.json"
    f.write_text(json.dumps({
        "base_dir": str(hcfg.base_dir), "template_dir": str(hcfg.template_dir),
        "unit_dir": str(hcfg.unit_dir), "service_user": "root", "allow_shared_service_user": True,
        "port_base": 18812, "max_slots": 4, "systemctl": str(sc),
        "lock_dir": str(hcfg.lock_dir), "op_timeout_seconds": 60,
        "lock_wait_seconds": 5}))
    os.chmod(f, 0o644)
    return f


def _helper_proc(hcfg, tmp_path, hook, pid=PA):
    """Proses helper main() SUNGGUHAN; hook mencetak READY lalu menunggu."""
    code = textwrap.dedent(f"""
        import os, shutil, sys, time
        sys.path.insert(0, {ROOT!r})
        from provisioner import mt5_provision_helper as H
        hook = {hook!r}
        _rename, _rmtree = os.rename, shutil.rmtree
        def rename(a, b):
            if hook == "before_rename":
                print("READY", flush=True); time.sleep(60)
            return _rename(a, b)
        def rmtree(path, *a, **k):
            if hook == "after_rename":
                print("READY", flush=True); time.sleep(60)
            if hook == "kill_mid_rmtree":
                p = str(path)
                os.unlink(os.path.join(p, ".provision_id"))
                for n in sorted(os.listdir(os.path.join(p, "terminal")))[:10]:
                    os.unlink(os.path.join(p, "terminal", n))
                print("READY", flush=True); time.sleep(60)
            if hook == "slow_rmtree":
                time.sleep(1.0)
            return _rmtree(path, *a, **k)
        H.os.rename = rename
        H.shutil.rmtree = rmtree
        sys.exit(H.main(["remove", "--code", "C001", "--provision-id",
                         {pid!r}], config_path={str(_cfgfile(hcfg, tmp_path))!r}))
    """)
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)


def _ready(pr):
    line = pr.stdout.readline().strip()
    assert line == "READY", line


@needs_root
def test_sigterm_sebelum_rename_lalu_restart_melanjutkan(hcfg, tmp_path,
                                                         other):
    _inst(hcfg)
    o0 = other()
    pr = _helper_proc(hcfg, tmp_path, "before_rename")
    _ready(pr)
    pr.send_signal(signal.SIGTERM)
    out, _ = pr.communicate(timeout=20)
    assert pr.returncode == H.EXIT_INTERRUPTED, out
    p = H.paths(hcfg, "C001")
    assert p["base"].is_dir() and H.read_marker(p) == PA   # bukti utuh
    assert H.op_inspect(hcfg, "C001", PA)["busy"] is False
    res = _remove(hcfg)                                    # helper "restart"
    assert res["ok"] and res["resumed"] is True
    assert _clean(hcfg) and other() == o0


@needs_root
def test_sigterm_setelah_rename_lalu_restart_melanjutkan(hcfg, tmp_path,
                                                         other):
    _inst(hcfg)
    o0 = other()
    pr = _helper_proc(hcfg, tmp_path, "after_rename")
    _ready(pr)
    pr.send_signal(signal.SIGTERM)
    out, _ = pr.communicate(timeout=20)
    assert pr.returncode == H.EXIT_INTERRUPTED, out
    tomb, manifest = H.tomb_paths(hcfg, "C001", PA)
    assert not (hcfg.base_dir / "C001").exists() and tomb.is_dir()
    assert H.read_manifest(manifest, "C001", PA)
    ins = H.op_inspect(hcfg, "C001", PA)
    assert ins["phase"] == "removing" and ins["owner_match"] is True
    assert ins["busy"] is False
    res = _remove(hcfg)
    assert res["ok"] and res["resumed"] is True
    assert _clean(hcfg) and other() == o0


@needs_root
def test_sigkill_saat_rmtree_penanda_hilang_tetap_dapat_dilanjutkan(
        hcfg, tmp_path, other):
    _inst(hcfg)
    o0 = other()
    pr = _helper_proc(hcfg, tmp_path, "kill_mid_rmtree")
    _ready(pr)
    pr.kill()
    pr.wait()
    tomb, manifest = H.tomb_paths(hcfg, "C001", PA)
    assert tomb.is_dir() and not (tomb / H.MARKER).exists()   # penanda hilang
    assert H.lock_busy(hcfg, "C001") is False                 # kernel lepas lock
    assert H.read_manifest(manifest, "C001", PA)              # bukti bertahan
    res = _remove(hcfg)
    assert res["ok"] and res["resumed"] is True
    assert _clean(hcfg) and other() == o0


@needs_root
def test_dua_remove_bersamaan_serial_dan_idempoten(hcfg, tmp_path, other):
    _inst(hcfg)
    o0 = other()
    a = _helper_proc(hcfg, tmp_path, "slow_rmtree")
    time.sleep(0.3)
    b = _helper_proc(hcfg, tmp_path, "slow_rmtree")
    oa, _ = a.communicate(timeout=30)
    ob, _ = b.communicate(timeout=30)
    ra = json.loads(oa.strip().splitlines()[-1])
    rb = json.loads(ob.strip().splitlines()[-1])
    assert a.returncode == 0 and b.returncode == 0, (oa, ob)
    assert sorted([ra["already_absent"], rb["already_absent"]]) == \
        [False, True]
    assert _clean(hcfg) and other() == o0


# ------------------------------------------------------------- in-process
def _interrupt_at(monkeypatch, target, when="before"):
    """Galat (setara SIGTERM -> HelperError exit 5) pada titik tertentu."""
    obj, name = target
    real = getattr(obj, name)

    def wrapped(*a, **k):
        if when == "after":
            real(*a, **k)
        raise H.HelperError(H.EXIT_INTERRUPTED, "dihentikan (uji)")
    monkeypatch.setattr(obj, name, wrapped)


@pytest.mark.parametrize("target,when", [
    ((H, "_atomic_write"), "before"),        # sebelum manifest
    ((H.os, "rename"), "before"),            # sebelum rename
    ((H.os, "rename"), "after"),             # tepat setelah rename
    ((H.shutil, "rmtree"), "before"),        # sebelum rmtree tombstone
])
def test_terputus_di_setiap_titik_lalu_dilanjutkan(hcfg, other, monkeypatch,
                                                   target, when):
    _inst(hcfg)
    o0 = other()
    _interrupt_at(monkeypatch, target, when)
    with pytest.raises(H.HelperError):
        _remove(hcfg)
    monkeypatch.undo()
    assert other() == o0
    res = _remove(hcfg)
    assert res["ok"] is True and _clean(hcfg) and other() == o0


def test_tombstone_tanpa_manifest_tidak_disentuh(hcfg, other):
    o0 = other()
    tomb, manifest = H.tomb_paths(hcfg, "C001", PA)
    tomb.mkdir()
    (tomb / "berkas").write_text("milik siapa?")
    with pytest.raises(H.HelperError) as ei:
        _remove(hcfg)
    assert ei.value.code == H.EXIT_OWNERSHIP
    assert (tomb / "berkas").exists() and other() == o0


def test_tombstone_penanda_berbeda_tidak_disentuh(hcfg):
    tomb, manifest = H.tomb_paths(hcfg, "C001", PA)
    tomb.mkdir()
    (tomb / H.MARKER).write_text(PC)
    manifest.write_text(json.dumps({"code": "C001", "provision_id": PA}))
    os.chmod(manifest, 0o600)
    with pytest.raises(H.HelperError) as ei:
        _remove(hcfg)
    assert ei.value.code == H.EXIT_OWNERSHIP and tomb.is_dir()


def test_manifest_pid_lain_tidak_disentuh_oleh_remove_pid_ini(hcfg):
    """Tombstone milik provision_id B tidak dihapus oleh remove ber-pid A."""
    tombB, manB = H.tomb_paths(hcfg, "C001", PB)
    tombB.mkdir()
    (tombB / H.MARKER).write_text(PB)
    manB.write_text(json.dumps({"code": "C001", "provision_id": PB}))
    os.chmod(manB, 0o600)
    out = _remove(hcfg, pid=PA)
    assert out["already_absent"] is True and tombB.is_dir() and manB.exists()


def test_manifest_palsu_atau_symlink_ditolak(hcfg, tmp_path):
    tomb, manifest = H.tomb_paths(hcfg, "C001", PA)
    real = tmp_path / "palsu.json"
    real.write_text(json.dumps({"code": "C001", "provision_id": PA}))
    os.symlink(real, manifest)
    tomb.mkdir()
    with pytest.raises(H.HelperError) as ei:
        _remove(hcfg)
    assert ei.value.code == H.EXIT_OWNERSHIP and tomb.is_dir()


def test_install_ditolak_selama_penghapusan_belum_selesai(hcfg, monkeypatch):
    _inst(hcfg)
    _interrupt_at(monkeypatch, (H.shutil, "rmtree"))
    with pytest.raises(H.HelperError):
        _remove(hcfg)
    monkeypatch.undo()
    with pytest.raises(H.HelperError) as ei:
        _inst(hcfg, pid=PC)
    assert ei.value.code == H.EXIT_OWNERSHIP
    assert H.op_inspect(hcfg, "C001", PA)["phase"] == "removing"
    assert _remove(hcfg)["ok"] is True
    assert _inst(hcfg, pid=PC)["ok"] is True       # setelah selesai: boleh


def test_direktori_tombstone_tidak_aman_ditolak(hcfg):
    d = hcfg.base_dir / H.TOMB_DIR
    d.mkdir(mode=0o777)
    os.chmod(d, 0o777)
    _ = d
    with pytest.raises(H.HelperError) as ei:
        _remove(hcfg)
    assert ei.value.code == H.EXIT_FAIL


def test_api_fase_removing_bukan_uncertain(monkeypatch):
    class C:
        rpyc_port_base = 18812
        max_customers = 4
        provisioner_helper = "/x"
        provisioner_sudo = "/usr/bin/sudo"
        provisioner_timeout_seconds = 1
    hp = P.HelperProvisioner(C())
    monkeypatch.setattr(hp, "_spawn", lambda argv, t: (0, json.dumps(
        {"ok": True, "busy": False, "phase": "removing",
         "owner_match": True})))
    assert hp._inspect_state(P.TerminalSpec("C001", 18812, PA)) == \
        "stopped_incomplete"
