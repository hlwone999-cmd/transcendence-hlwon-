"""K1 — fence helper diuji dengan DUA (atau lebih) PROSES OS NYATA.

Setiap "worker" adalah subprocess python terpisah yang memanggil
H.dispatch() pada konfigurasi & berkas yang SAMA (flock nyata, fence file
nyata). `systemctl` diganti runner yang dapat DITAHAN di dalam flock sampai
berkas sinyal muncul — sehingga operasi generasi lama benar-benar sedang
memegang lock saat generasi baru mengambil alih.

Tidak butuh root: instalasi awal memakai chown=False. FN7 (fencing DB) ada
di kelompok berikutnya (butuh tabel mt5_rpc_migrations) — TIDAK diklaim di
sini."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from provisioner import mt5_provision_helper as H
from tests.test_provision_helper import FakeRunner

PA = "a" * 32
M1, M2 = "1" * 32, "2" * 32
BACKEND = str(Path(H.__file__).resolve().parent.parent)

DRIVER = r'''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from provisioner import mt5_provision_helper as H
cfgd = json.loads(sys.argv[2]); argv = json.loads(sys.argv[3])
work = Path(sys.argv[4]); tag = sys.argv[5]
hold = os.environ.get("FN_HOLD") == "1"
log = work / f"{tag}.log"

class R:
    pass_fds = ()
    on_unclean = None
    def run(self, a, check=True):
        with open(log, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "argv": a}) + "\n")
        if hold and ("disable" in a or "enable" in a):
            (work / f"{tag}.started").write_text("1")
            while not (work / f"{tag}.release").exists():
                time.sleep(0.02)
        return 3 if "is-active" in a else 0

paths = ("base_dir", "template_dir", "unit_dir", "proc_root", "lock_dir")
cfg = H.HelperConfig(**{k: (Path(v) if k in paths else v)
                        for k, v in cfgd.items()})
if os.environ.get("FN_SIGNALS") == "1":
    H._install_signal_handlers(cfg)
    if not H._SUBREAPER_OK:
        print(json.dumps({"code": -1, "out": "no-subreaper"})); sys.exit(0)
try:
    out = H.dispatch(cfg, argv, runner=R()); code = 0
except H.HelperError as e:
    out = {"error": e.msg}; code = e.code
print(json.dumps({"code": code, "out": out, "t_end": time.time()}))
'''


class World:
    def __init__(self, tmp_path, **kw):
        self.tmp = tmp_path
        tpl = tmp_path / "template"
        tpl.mkdir()
        (tpl / "terminal64.exe").write_text("bin")
        (tpl / "mt5server.exe").write_text("bin")
        for n in ("terminals", "units", "work"):
            (tmp_path / n).mkdir()
        self.work = tmp_path / "work"
        self.cfgd = dict(base_dir=str(tmp_path / "terminals"),
                         template_dir=str(tpl),
                         unit_dir=str(tmp_path / "units"),
                         service_user="transcendence", port_base=18812,
                         max_slots=4, proc_root=str(tmp_path / "proc"),
                         lock_dir=str(tmp_path / "locks"),
                         lock_wait_seconds=10.0, op_timeout_seconds=30.0)
        self.cfgd.update(kw)
        self.driver = tmp_path / "work" / "driver.py"
        self.driver.write_text(DRIVER)
        self.procs = []

    @property
    def cfg(self):
        paths = ("base_dir", "template_dir", "unit_dir", "proc_root",
                 "lock_dir")
        return H.HelperConfig(**{k: (Path(v) if k in paths else v)
                                 for k, v in self.cfgd.items()})

    def spawn(self, tag, argv, hold=False, signals=False):
        env = dict(os.environ, FN_HOLD="1" if hold else "0",
                   FN_SIGNALS="1" if signals else "0")
        p = subprocess.Popen(
            [sys.executable, "-I", str(self.driver), BACKEND,
             json.dumps(self.cfgd), json.dumps(argv), str(self.work), tag],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=str(self.work))
        self.procs.append(p)
        return p

    def result(self, p, timeout=30):
        out, err = p.communicate(timeout=timeout)
        assert out.strip(), err
        return json.loads(out.strip().splitlines()[-1])

    def wait_flag(self, tag, timeout=10):
        end = time.time() + timeout
        f = self.work / f"{tag}.started"
        while not f.exists():
            assert time.time() < end, f"{tag} tidak pernah memegang lock"
            time.sleep(0.02)

    def release(self, tag):
        (self.work / f"{tag}.release").write_text("1")

    def calls(self, tag):
        f = self.work / f"{tag}.log"
        if not f.exists():
            return []
        return [json.loads(x) for x in f.read_text().splitlines()]

    def fence(self):
        return H._fence_public(self.cfg, "C001")

    def close(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()


@pytest.fixture()
def world(tmp_path):
    w = World(tmp_path)
    H.op_install(w.cfg, "C001", 18812, PA, FakeRunner(), chown=False,
                 rpc_mode="classic")
    yield w
    w.close()


def adv(mid, gen):
    return ["fence-advance", "--code", "C001", "--migration-id", mid,
            "--gen", str(gen)]


def stop(mid, gen):
    return ["stop", "--code", "C001", "--expect-pid", PA,
            "--migration-id", mid, "--fence", str(gen)]


def test_fn1_advance_menunggu_operasi_gen1_di_dalam_lock(world):
    assert world.result(world.spawn("a0", adv(M1, 1)))["code"] == 0
    p1 = world.spawn("p1", stop(M1, 1), hold=True)
    world.wait_flag("p1")                       # gen1 MEMEGANG flock
    p2 = world.spawn("p2", adv(M1, 2))
    time.sleep(0.8)
    assert p2.poll() is None, "advance tidak boleh selesai saat gen1 jalan"
    assert world.fence()["gen"] == 1
    world.release("p1")
    r1, r2 = world.result(p1), world.result(p2)
    assert r1["code"] == 0 and r2["code"] == 0, (r1, r2)
    assert r2["t_end"] >= r1["t_end"]           # advance SESUDAH gen1 selesai
    assert world.fence() == {"migration_id": M1, "gen": 2,
                             "released": False}
    # inspect di dalam lock yang sama mencerminkan keadaan nyata
    assert r2["out"]["marker"] == PA and r2["out"]["phase"] == "complete"


def test_fn1b_gen1_lebih_lama_dari_lock_wait_advance_sibuk(tmp_path):
    w = World(tmp_path, lock_wait_seconds=0.5)
    try:
        H.op_install(w.cfg, "C001", 18812, PA, FakeRunner(), chown=False,
                     rpc_mode="classic")
        assert w.result(w.spawn("a0", adv(M1, 1)))["code"] == 0
        p1 = w.spawn("p1", stop(M1, 1), hold=True)
        w.wait_flag("p1")
        r2 = w.result(w.spawn("p2", adv(M1, 2)))
        assert r2["code"] == H.EXIT_BUSY        # API: ulangi s/d T_drain
        assert w.fence()["gen"] == 1            # tidak ada perubahan
        w.release("p1")
        assert w.result(p1)["code"] == 0
        assert w.result(w.spawn("p3", adv(M1, 2)))["code"] == 0
    finally:
        w.close()


@pytest.mark.parametrize("_round", range(3))
def test_fn2_gen1_antre_bersama_advance_tidak_berefek_setelah_fence(world,
                                                                     _round):
    """Operasi gen1 yang ANTRE flock bersamaan dengan advance gen2: bila
    gen1 mendapat lock lebih dulu, efeknya terjadi SEBELUM fence=2 ditulis;
    bila sesudahnya, ditolak exit 8. Tidak pernah ada efek gen1 setelah
    fence=2."""
    assert world.result(world.spawn("a0", adv(M1, 1)))["code"] == 0
    h = world.spawn("h", stop(M1, 1), hold=True)
    world.wait_flag("h")
    p1 = world.spawn("p1", ["start", "--code", "C001", "--port", "18812",
                            "--expect-pid", PA, "--migration-id", M1,
                            "--fence", "1"])
    p2 = world.spawn("p2", adv(M1, 2))
    time.sleep(0.5)
    world.release("h")
    assert world.result(h)["code"] == 0
    r1, r2 = world.result(p1), world.result(p2)
    assert r2["code"] == 0
    fence_written = os.stat(Path(world.cfgd["base_dir"]) / H.FENCE_DIR /
                            "C001").st_mtime
    if r1["code"] == 0:
        enable = [c for c in world.calls("p1") if "enable" in c["argv"]]
        assert enable and enable[-1]["t"] <= fence_written
    else:
        assert r1["code"] == H.EXIT_FENCE and world.calls("p1") == []
    # setelah fence=2, gen1 SELALU ditolak
    r3 = world.result(world.spawn("p3", stop(M1, 1)))
    assert r3["code"] == H.EXIT_FENCE and world.calls("p3") == []


@pytest.mark.parametrize("argv", [
    ["install", "--code", "C001", "--port", "18812", "--provision-id", PA,
     "--rpc-mode", "classic", "--migration-id", M1, "--fence", "1"],
    ["start", "--code", "C001", "--port", "18812", "--expect-pid", PA,
     "--migration-id", M1, "--fence", "1"],
    stop(M1, 1),
    ["remove", "--code", "C001", "--provision-id", PA, "--migration-id", M1,
     "--fence", "1"],
])
def test_fn3_semua_operasi_gen1_ditolak_setelah_advance(world, argv):
    assert world.result(world.spawn("a1", adv(M1, 1)))["code"] == 0
    assert world.result(world.spawn("a2", adv(M1, 2)))["code"] == 0
    base = Path(world.cfgd["base_dir"]) / "C001"
    before = sorted(str(p.relative_to(base)) for p in base.rglob("*"))
    r = world.result(world.spawn("g1", argv))
    assert r["code"] == H.EXIT_FENCE, r
    assert world.calls("g1") == []
    assert sorted(str(p.relative_to(base)) for p in base.rglob("*")) == before


def test_fn4_gen1_melewati_op_timeout_berhenti_sendiri_lalu_advance(tmp_path):
    w = World(tmp_path, op_timeout_seconds=1.0)
    try:
        H.op_install(w.cfg, "C001", 18812, PA, FakeRunner(), chown=False,
                     rpc_mode="classic")
        assert w.result(w.spawn("a0", adv(M1, 1)))["code"] == 0
        p1 = w.spawn("p1", stop(M1, 1), hold=True, signals=True)
        r1 = w.result(p1, timeout=20)        # TIDAK pernah dilepas
        if r1["code"] == -1:
            pytest.skip("PR_SET_CHILD_SUBREAPER tidak tersedia")
        assert r1["code"] == H.EXIT_INTERRUPTED, r1
        r2 = w.result(w.spawn("p2", adv(M1, 2)))
        assert r2["code"] == 0 and w.fence()["gen"] == 2
    finally:
        w.close()


def test_fn5_proses_tersisa_unclean_advance_sibuk_sampai_mati(world):
    assert world.result(world.spawn("a0", adv(M1, 1)))["code"] == 0
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        start = None
        for _ in range(50):
            start = H._proc_start(sleeper.pid)
            if start:
                break
            time.sleep(0.02)
        assert start
        H._lock_dir(world.cfg)
        H.record_unclean(world.cfg, "C001", [(sleeper.pid, start)])
        for _ in range(2):
            r = world.result(world.spawn("p", adv(M1, 2)))
            assert r["code"] == H.EXIT_BUSY, r   # API: BLOCKED setelah T_drain
        assert world.fence()["gen"] == 1
    finally:
        sleeper.send_signal(signal.SIGKILL)
        sleeper.wait()
    r = world.result(world.spawn("q", adv(M1, 2)))
    assert r["code"] == 0 and world.fence()["gen"] == 2


def test_fn6_migrasi_lain_dengan_gen_lebih_besar_ditolak(world):
    assert world.result(world.spawn("a0", adv(M1, 1)))["code"] == 0
    r = world.result(world.spawn("b", adv(M2, 99)))
    assert r["code"] == H.EXIT_FENCE
    r = world.result(world.spawn("c", stop(M2, 99)))
    assert r["code"] == H.EXIT_FENCE and world.calls("c") == []
    assert world.fence() == {"migration_id": M1, "gen": 1, "released": False}


def test_fn_jalur_tanpa_fence_ditolak_saat_migrasi_lalu_bebas_setelah_release(
        world):
    assert world.result(world.spawn("a0", adv(M1, 1)))["code"] == 0
    r = world.result(world.spawn("x", ["stop", "--code", "C001"]))
    assert r["code"] == H.EXIT_FENCE and world.calls("x") == []
    assert world.result(world.spawn("r", [
        "fence-release", "--code", "C001", "--migration-id", M1,
        "--gen", "1"]))["code"] == 0
    r = world.result(world.spawn("y", ["stop", "--code", "C001"]))
    assert r["code"] == 0
