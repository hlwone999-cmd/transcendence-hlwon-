"""B8/S5 — helper root tidak pernah mengubah berkas di luar pohon customer
yang sedang diproses (symlink / TOCTOU pada chown-chmod).

Uji kepemilikan butuh root (chown ke user lain) dan memakai user sistem
'nobody' sebagai user layanan — DILEWATI di CI non-root (tidak boleh
diklaim lulus di sana). Uji chown_tree_fd dasar berjalan tanpa root.
Sandbox lokal, bukan staging."""
import json
import os
import pwd
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from provisioner import mt5_provision_helper as H
from tests.test_provision_helper import FakeRunner

PA, PB = "a" * 32, "b" * 32
ROOT = str(Path(H.__file__).parent.parent)
needs_root = pytest.mark.skipif(os.geteuid() != 0,
                                reason="butuh root (chown ke user layanan)")
try:
    NOBODY = pwd.getpwnam("nobody")
except KeyError:                                    # pragma: no cover
    NOBODY = None


def _ident(p):
    st = os.lstat(p)
    return (st.st_ino, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))


def _traversable(tmp_path):
    """Jadikan jalur tmp dapat DILINTASI user layanan (x saja) agar penyerang
    benar-benar dapat mencapai direktori customer saat tidak terkunci."""
    p = Path(tmp_path)
    changed = []
    while str(p) != "/":
        m = stat.S_IMODE(os.stat(p).st_mode)
        if not m & 0o001:
            os.chmod(p, m | 0o001)
            changed.append((p, m))
        p = p.parent
    return changed


@pytest.fixture()
def world(tmp_path):
    """Customer A (C001, diserang lewat isinya) & B (C002, korban), user
    layanan 'nobody' (mode shared = kasus terburuk: UID sama)."""
    changed = _traversable(tmp_path)
    tpl = tmp_path / "template"
    (tpl / "sub").mkdir(parents=True)
    for i in range(8):
        (tpl / f"f{i}.bin").write_text("x" * (i + 1))
    (tpl / "sub" / "d.dat").write_text("data")
    for n in ("terminals", "units", "luar"):
        (tmp_path / n).mkdir()
    for n in (tpl, tmp_path / "terminals"):
        os.chmod(n, 0o755)
    cfg = H.HelperConfig(base_dir=tmp_path / "terminals", template_dir=tpl,
                         unit_dir=tmp_path / "units", service_user="nobody",
                         port_base=18812, max_slots=4,
                         proc_root=tmp_path / "proc",
                         lock_dir=tmp_path / "locks")
    H.op_install(cfg, "C002", 18813, PB, FakeRunner())
    vfile = cfg.base_dir / "C002" / "rahasia-B"
    vfile.write_text("token B")
    os.chown(vfile, 0, 0)
    os.chmod(vfile, 0o600)
    vdir = cfg.base_dir / "C002" / "dir-B"
    vdir.mkdir()
    os.chmod(vdir, 0o755)
    outside = tmp_path / "luar" / "berkas-sistem"
    outside.write_text("di luar pohon customer")
    os.chmod(outside, 0o644)
    victims = [vfile, vdir, outside,
               cfg.base_dir / "C002" / "terminal" / "f0.bin",
               cfg.base_dir / "C002"]
    yield cfg, tmp_path, victims
    for p, m in changed:
        os.chmod(p, m)


def _incomplete(cfg, code="C001"):
    """Instalasi A menjadi 'incomplete' dengan isi yang dikuasai user
    layanan (mis. remove terputus sebelum rename tombstone)."""
    try:
        os.unlink(cfg.base_dir / code / H.COMPLETE)
    except FileNotFoundError:
        pass


def _plant(cfg, name, target=None, directory=False):
    """Seperti proses user layanan: tanam entri di data/ milik A."""
    data = cfg.base_dir / "C001" / "data"
    p = data / name
    if target is not None:
        os.symlink(target, p)
        os.lchown(p, NOBODY.pw_uid, NOBODY.pw_gid)
    elif directory:
        p.mkdir()
        os.chown(p, NOBODY.pw_uid, NOBODY.pw_gid)
    else:
        p.write_text("milik A")
        os.chown(p, NOBODY.pw_uid, NOBODY.pw_gid)
    return p


# ============================================================ chown_tree_fd
def test_chown_tree_fd_menolak_symlink_hardlink_dan_berkas_khusus(tmp_path):
    """Tanpa root: uid/gid sendiri. Entri tidak sah -> EXIT_OWNERSHIP, dan
    target di luar pohon TIDAK berubah."""
    uid, gid = os.getuid(), os.getgid()
    outside = tmp_path / "luar"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    before = _ident(outside)
    for kind in ("symlink", "hardlink", "fifo"):
        tree = tmp_path / f"t-{kind}"
        (tree / "sub").mkdir(parents=True)
        (tree / "sub" / "ok").write_text("ok")
        bad = tree / "sub" / "jahat"
        if kind == "symlink":
            os.symlink(outside, bad)
        elif kind == "hardlink":
            os.link(outside, bad)
        else:
            os.mkfifo(bad)
        fd = os.open(tree, H._DIR_FLAGS)
        try:
            with pytest.raises(H.HelperError) as ei:
                H.chown_tree_fd(fd, uid, gid)
        finally:
            os.close(fd)
        assert ei.value.code == H.EXIT_OWNERSHIP, kind
        if kind == "hardlink":
            os.unlink(bad)
        assert _ident(outside) == before, kind


def test_chown_tree_fd_tukar_setelah_lstat_ditolak(tmp_path, monkeypatch):
    """TOCTOU: entri ditukar menjadi symlink SETELAH lstat, SEBELUM dibuka ->
    O_NOFOLLOW / verifikasi inode menolak; target tidak disentuh."""
    uid, gid = os.getuid(), os.getgid()
    for kind in ("file", "dir"):
        outside = tmp_path / f"luar-{kind}"
        if kind == "file":
            outside.write_text("x")
        else:
            outside.mkdir()
        os.chmod(outside, 0o755)
        before = _ident(outside)
        tree = tmp_path / f"t-{kind}"
        tree.mkdir()
        x = tree / "x"
        x.write_text("a") if kind == "file" else x.mkdir()
        real = os.stat
        done = {}

        def swap_after_stat(name, *a, **k):
            r = real(name, *a, **k)
            if name == "x" and not done:
                done["x"] = 1
                (x.unlink() if kind == "file" else x.rmdir())
                os.symlink(outside, x)              # langkah penyerang
            return r
        monkeypatch.setattr(H.os, "stat", swap_after_stat)
        fd = os.open(tree, H._DIR_FLAGS)
        try:
            with pytest.raises(H.HelperError) as ei:
                H.chown_tree_fd(fd, uid, gid)
        finally:
            os.close(fd)
            monkeypatch.undo()
        assert ei.value.code == H.EXIT_OWNERSHIP and done
        assert _ident(outside) == before


def test_chown_tree_fd_daun_ke_akar(tmp_path, monkeypatch):
    (tmp_path / "t" / "a" / "b").mkdir(parents=True)
    (tmp_path / "t" / "a" / "b" / "f").write_text("x")
    order = []
    real = os.fchown

    def spy(fd, u, g):
        order.append(os.readlink(f"/proc/self/fd/{fd}").split("/t/")[-1])
        return real(fd, u, g)
    monkeypatch.setattr(H.os, "fchown", spy)
    fd = os.open(tmp_path / "t", H._DIR_FLAGS)
    try:
        assert H.chown_tree_fd(fd, os.getuid(), os.getgid()) == 3
    finally:
        os.close(fd)
    assert order == ["a/b/f", "a/b", "a"]           # anak sebelum induk


# ============================================================ install
@needs_root
def test_install_pertama_terkunci_lalu_dibuka_paling_akhir(world,
                                                           monkeypatch):
    cfg, tmp, victims = world
    seen = []
    real = H._write_unit

    def spy(p, text):
        st = os.stat(p["base"])
        seen.append((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)))
        return real(p, text)
    monkeypatch.setattr(H, "_write_unit", spy)
    before = [_ident(v) for v in victims]
    H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    assert seen == [(0, 0, 0o700)]                  # terkunci selama proses
    base = cfg.base_dir / "C001"
    st = os.stat(base)
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == \
        (0, NOBODY.pw_gid, 0o750)                   # dibuka di akhir
    for sub in ("terminal", "wine-prefix", "data"):
        s2 = os.stat(base / sub)
        assert (s2.st_uid, stat.S_IMODE(s2.st_mode)) == (NOBODY.pw_uid, 0o700)
    assert os.stat(base / "terminal" / "sub" / "d.dat").st_uid == \
        NOBODY.pw_uid
    assert os.stat(base / H.MARKER).st_uid == 0     # bukti tetap milik root
    assert [_ident(v) for v in victims] == before


@needs_root
@pytest.mark.parametrize("kind", ["file_B", "dir_B", "luar", "rantai"])
def test_reinstall_symlink_statis_tidak_mengubah_target(world, kind):
    """Symlink tertanam di data/ A menuju berkas B / direktori B / lokasi di
    luar pohon customer: isi lama DIBUANG, target tidak berubah sedikit pun."""
    cfg, tmp, victims = world
    H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    target = {"file_B": victims[0], "dir_B": victims[1], "luar": victims[2],
              "rantai": victims[0]}[kind]
    _plant(cfg, "x", target=target)
    if kind == "rantai":                            # symlink -> symlink -> B
        _plant(cfg, "y", target=cfg.base_dir / "C001" / "data" / "x")
    d = _plant(cfg, "d", directory=True)
    os.symlink(target, d / "dalam")
    _incomplete(cfg)
    before = [_ident(v) for v in victims]
    content = victims[0].read_text()
    H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    assert [_ident(v) for v in victims] == before
    assert victims[0].read_text() == content
    assert os.listdir(cfg.base_dir / "C001" / "data") == []   # isi lama dibuang
    assert H.phase_of(H.paths(cfg, "C001")) == "complete"


@needs_root
def test_install_gagal_tengah_jalan_tetap_terkunci(world, monkeypatch):
    """Terputus sebelum selesai: direktori customer TETAP root:root 0700
    (user layanan tidak dapat masuk); install ulang & remove tetap bisa."""
    cfg, tmp, victims = world
    monkeypatch.setattr(H, "_write_unit", lambda *a: (_ for _ in ()).throw(
        H.HelperError(H.EXIT_INTERRUPTED, "dihentikan")))
    with pytest.raises(H.HelperError):
        H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    monkeypatch.undo()
    st = os.stat(cfg.base_dir / "C001")
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (0, 0, 0o700)
    r = subprocess.run(["runuser", "-u", "nobody", "--", "ls",
                        str(cfg.base_dir / "C001" / "data")],
                       capture_output=True)
    assert r.returncode != 0                         # tidak dapat masuk
    assert H.op_install(cfg, "C001", 18812, PA, FakeRunner())["ok"] is True
    assert H.op_remove(cfg, "C001", PA, FakeRunner())["ok"] is True


# ============================================================ race nyata
_FLIP = """
import os, signal, sys, time
mode, target, victim, ctl, out = sys.argv[1:6]
os.setgroups([]); os.setgid({gid}); os.setuid({uid})
n = {{"coba": 0, "tukar": 0, "ditolak": 0, "kontrol": 0}}
def done(*a):
    open(out, "w").write(repr(n)); os._exit(0)
signal.signal(signal.SIGTERM, done)
if mode == "cwd":
    while True:                       # tunggu sampai data/ dapat dimasuki
        try:
            os.chdir(target); break
        except OSError:
            time.sleep(0.001)
    x = "x"
else:
    x = os.path.join(target, "x")
c = os.path.join(ctl, "c")
while True:
    n["coba"] += 1
    try:                              # KONTROL: bukti penukar berfungsi
        os.symlink(victim, c + ".l"); os.rename(c + ".l", c)
        open(c + ".f", "w").close(); os.rename(c + ".f", c)
        n["kontrol"] += 1
    except OSError:
        pass
    try:
        os.symlink(victim, x + ".l"); os.rename(x + ".l", x)
        open(x + ".f", "w").close(); os.rename(x + ".f", x)
        n["tukar"] += 1
    except OSError:
        n["ditolak"] += 1
"""


def _race(world, mode, rounds, first_install):
    cfg, tmp, victims = world
    ctl = tmp / "kontrol"
    ctl.mkdir()
    os.chown(ctl, NOBODY.pw_uid, NOBODY.pw_gid)
    out = ctl / "hasil"
    H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    for i in range(150):                             # perlebar pohon
        _plant(cfg, f"pad{i:03d}")
    before = [_ident(v) for v in victims]
    content = victims[0].read_text()
    fl = subprocess.Popen([sys.executable, "-c",
                           _FLIP.format(uid=NOBODY.pw_uid,
                                        gid=NOBODY.pw_gid),
                           mode, str(cfg.base_dir / "C001" / "data"),
                           str(victims[0]), str(ctl), str(out)])
    ok = gagal = 0
    try:
        time.sleep(0.3)
        for i in range(rounds):
            if first_install:                        # hapus -> install PERTAMA
                try:
                    H.op_remove(cfg, "C001", PA, FakeRunner())
                except Exception:  # noqa: BLE001
                    pass
            else:
                _incomplete(cfg)
            try:
                H.op_install(cfg, "C001", 18812, PA, FakeRunner())
                ok += 1
            except Exception:  # noqa: BLE001 — gagal bersih boleh; bocor tidak
                gagal += 1
            assert [_ident(v) for v in victims] == before, f"putaran {i}"
            if mode == "abs" and i % 10 == 0:
                time.sleep(0.01)                     # beri giliran penukar
    finally:
        fl.send_signal(signal.SIGTERM)
        fl.wait(timeout=10)
    stats = eval(out.read_text())                    # noqa: S307 — keluaran uji sendiri
    assert victims[0].read_text() == content
    return stats, ok, gagal


@needs_root
@pytest.mark.parametrize("mode,first", [("abs", False), ("abs", True),
                                        ("cwd", False)])
def test_race_nyata_penukar_aktif_target_tidak_pernah_berubah(world, mode,
                                                              first):
    """Proses ber-UID user layanan (= proses customer lain pada mode shared)
    menukar berkas<->symlink menuju berkas B TERUS-MENERUS selama ratusan
    install. Tes hanya sah bila penukar TERBUKTI aktif."""
    stats, ok, gagal = _race(world, mode, rounds=300, first_install=first)
    print(f"\nRACE mode={mode} install_pertama={first}: penukar={stats} "
          f"install_ok={ok} install_gagal_bersih={gagal}")
    assert stats["kontrol"] > 100, stats             # penukar BERFUNGSI
    assert stats["coba"] > 100, stats                # dan terus mencoba
    assert stats["tukar"] + stats["ditolak"] > 100, stats
    if mode == "abs" and not first:
        assert stats["tukar"] > 0, stats             # sempat masuk saat terbuka
        assert stats["ditolak"] > 0, stats           # ditolak saat terkunci
    assert ok > 0, (ok, gagal)


# ============================================================ mode shared
def _main(cfgf, pre=""):
    code = (f"import os, sys; sys.path.insert(0, {ROOT!r})\n"
            "from provisioner import mt5_provision_helper as H\n" + pre +
            "\nsys.exit(H.main(['install','--code','C001','--port','18812',"
            f"'--provision-id','{PA}'], config_path={str(cfgf)!r}))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=60)
    return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])


def _cfgfile(tmp_path, **kw):
    tpl = tmp_path / "template"
    tpl.mkdir()
    (tpl / "terminal64.exe").write_text("bin")
    for n in ("terminals", "units"):
        (tmp_path / n).mkdir()
    sc = tmp_path / "fake-systemctl"
    sc.write_text('#!/bin/sh\n[ "$1" = is-active ] && exit 3\nexit 0\n')
    sc.chmod(0o755)
    f = tmp_path / "prov.json"
    d = {"base_dir": str(tmp_path / "terminals"), "template_dir": str(tpl),
         "unit_dir": str(tmp_path / "units"), "service_user": "nobody",
         "port_base": 18812, "max_slots": 4, "systemctl": str(sc),
         "lock_dir": str(tmp_path / "locks")}
    d.update(kw)
    f.write_text(json.dumps(d))
    f.chmod(0o644)
    return f


@needs_root
def test_mode_shared_ditolak_proses_helper_sebelum_perubahan(tmp_path):
    cfgf = _cfgfile(tmp_path)                        # default: shared
    rc, out = _main(cfgf)
    assert rc == H.EXIT_FAIL and out["ok"] is False
    assert "per_customer" in out["error"]
    assert not (tmp_path / "terminals" / "C001").exists()
    cfgf = _cfgfile_again(tmp_path, service_user_mode="shared",
                          allow_shared_service_user="yes")   # bukan true
    rc, out = _main(cfgf)
    assert rc == H.EXIT_FAIL


def _cfgfile_again(tmp_path, **kw):
    f = tmp_path / "prov.json"
    d = json.loads(f.read_text())
    d.update(kw)
    f.write_text(json.dumps(d))
    return f


@needs_root
def test_mode_shared_hanya_dengan_izin_eksplisit(tmp_path):
    cfgf = _cfgfile(tmp_path, allow_shared_service_user=True)
    rc, out = _main(cfgf)
    assert rc == 0 and out["ok"] is True, out


@needs_root
def test_mode_per_customer_diterima(tmp_path):
    """per_customer: user mt5-c001 (useradd/userdel dipalsukan agar tidak
    membuat user sistem di mesin uji; kepemilikan memakai 'nobody')."""
    ua = tmp_path / "fake-useradd"
    ua.write_text("#!/bin/sh\nexit 0\n")
    ua.chmod(0o755)
    cfgf = _cfgfile(tmp_path, service_user_mode="per_customer",
                    useradd=str(ua), userdel=str(ua))
    pre = ("import pwd\n_g = pwd.getpwnam\n"
           "pwd.getpwnam = lambda n: _g('nobody') if n == 'mt5-c001' "
           "else _g(n)\n")
    rc, out = _main(cfgf, pre=pre)
    assert rc == 0 and out["ok"] is True, out
    st = os.stat(tmp_path / "terminals" / "C001")
    assert (st.st_uid, stat.S_IMODE(st.st_mode)) == (0, 0o750)


def test_default_konfigurasi_tidak_mengizinkan_shared(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"base_dir": "/x", "template_dir": "/y",
                             "service_user": "u", "port_base": 1,
                             "max_slots": 1}))
    os.chmod(f, 0o644)
    if os.geteuid() != 0:
        pytest.skip("HelperConfig.load mewajibkan berkas milik root")
    cfg = H.HelperConfig.load(str(f))
    assert cfg.service_user_mode == "shared"         # default lama TIDAK diubah
    assert cfg.allow_shared_service_user is False    # tetapi ditolak helper


# ============================================================ B8.1
# Bukti selesai (.install_complete) hanya ditulis SETELAH direktori customer
# dibuka (fchown + fchmod) dan keadaannya terverifikasi; jalur idempoten,
# status, dan start memverifikasi ulang keadaan direktori itu.
def _final_open_fault(monkeypatch, which):
    """Gagalkan HANYA operasi pembukaan akhir direktori customer:
    fchown(base, 0, <gid customer>) atau fchmod(base, 0o750)."""
    real_chown, real_chmod = os.fchown, os.fchmod
    hit = {"n": 0}

    def fchown(fd, uid, gid):
        if which == "fchown" and uid == 0 and gid != 0:
            hit["n"] += 1
            raise OSError(5, "EIO (uji: fchown akhir)")
        return real_chown(fd, uid, gid)

    def fchmod(fd, mode):
        if which == "fchmod" and mode == 0o750:
            hit["n"] += 1
            raise OSError(5, "EIO (uji: fchmod akhir)")
        return real_chmod(fd, mode)
    monkeypatch.setattr(H.os, "fchown", fchown)
    monkeypatch.setattr(H.os, "fchmod", fchmod)
    return hit


def _base_state(cfg, code="C001"):
    st = os.lstat(cfg.base_dir / code)
    return (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))


@needs_root
@pytest.mark.parametrize("which", ["fchown", "fchmod"])
def test_b81_gagal_buka_akhir_tanpa_false_success_lalu_pulih(world,
                                                             monkeypatch,
                                                             which):
    cfg, tmp, victims = world
    before = [_ident(v) for v in victims]
    hit = _final_open_fault(monkeypatch, which)
    with pytest.raises(OSError):
        H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    monkeypatch.undo()
    assert hit["n"] == 1                                 # fault benar terjadi
    p = H.paths(cfg, "C001")
    assert not p["complete"].exists()                    # TIDAK ada bukti selesai
    assert H.phase_of(p) == "incomplete"
    assert H.op_status(cfg, "C001", 18812, FakeRunner())["installed"] is False
    with pytest.raises(H.HelperError) as ei:
        H.op_start(cfg, "C001", 18812, FakeRunner())
    assert ei.value.code == H.EXIT_OWNERSHIP
    # direktori tidak terbuka untuk grup (fchown gagal -> grup root;
    # fchmod gagal -> mode 0700)
    uid, gid, mode = _base_state(cfg)
    assert uid == 0 and (gid == 0 or mode == 0o700)
    # retry: BUKAN idempoten (bukti tidak ada) -> instalasi ulang penuh
    out = H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    assert out == {"ok": True, "idempotent": False}
    assert _base_state(cfg) == (0, NOBODY.pw_gid, 0o750)
    assert H.phase_of(p) == "complete"
    assert H.op_install(cfg, "C001", 18812, PA, FakeRunner()) == \
        {"ok": True, "idempotent": True}
    assert [_ident(v) for v in victims] == before


@needs_root
def test_b81_terputus_setelah_dibuka_sebelum_bukti_selesai(world,
                                                           monkeypatch):
    """Direktori sudah dibuka (0750) tetapi proses terputus SEBELUM
    .install_complete ditulis: tidak pernah dinyatakan selesai; retry
    MENGUNCI ulang dulu lalu membangun ulang."""
    cfg, tmp, victims = world
    real = H._atomic_write

    def cut(path, data, mode):
        if Path(path).name == H.COMPLETE:
            raise H.HelperError(H.EXIT_INTERRUPTED, "dihentikan (uji)")
        return real(path, data, mode)
    monkeypatch.setattr(H, "_atomic_write", cut)
    with pytest.raises(H.HelperError):
        H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    monkeypatch.undo()
    p = H.paths(cfg, "C001")
    assert _base_state(cfg) == (0, NOBODY.pw_gid, 0o750)  # terbuka...
    assert not p["complete"].exists()                     # ...tanpa bukti
    assert H.phase_of(p) == "incomplete"
    with pytest.raises(H.HelperError):
        H.op_start(cfg, "C001", 18812, FakeRunner())
    states = []
    real_unit = H._write_unit

    def spy(pp, text):
        states.append(_base_state(cfg))
        return real_unit(pp, text)
    monkeypatch.setattr(H, "_write_unit", spy)
    assert H.op_install(cfg, "C001", 18812, PA, FakeRunner())["idempotent"] \
        is False
    monkeypatch.undo()
    assert states == [(0, 0, 0o700)]                      # dikunci ulang dulu
    assert H.phase_of(p) == "complete"


@needs_root
@pytest.mark.parametrize("tamper", ["mode_0700", "grup_root", "mode_0755",
                                    "owner_user", "marker_lama",
                                    "pid_lain", "unit_hilang"])
def test_b81_idempoten_memverifikasi_keadaan_nyata(world, tamper):
    """Bukti selesai ADA tetapi syarat lain tidak terpenuhi -> BUKAN
    idempoten, BUKAN complete, start ditolak; install memulihkan."""
    cfg, tmp, victims = world
    H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    p = H.paths(cfg, "C001")
    base = cfg.base_dir / "C001"
    if tamper == "mode_0700":
        os.chmod(base, 0o700)
    elif tamper == "grup_root":
        os.chown(base, 0, 0)
    elif tamper == "mode_0755":
        os.chmod(base, 0o755)
    elif tamper == "owner_user":
        os.chown(base, NOBODY.pw_uid, NOBODY.pw_gid)
    elif tamper == "marker_lama":                         # format pra-B8.1
        p["complete"].write_text(json.dumps(
            {"provision_id": PA, "port": 18812, "rpc_mode": "classic"}))
    elif tamper == "pid_lain":
        d = json.loads(p["complete"].read_text())
        d["provision_id"] = PB
        p["complete"].write_text(json.dumps(d))
    elif tamper == "unit_hilang":
        p["unit_file"].unlink()
    assert H.phase_of(p) != "complete"
    assert H.op_status(cfg, "C001", 18812, FakeRunner())["installed"] is False
    with pytest.raises(H.HelperError):
        H.op_start(cfg, "C001", 18812, FakeRunner())
    out = H.op_install(cfg, "C001", 18812, PA, FakeRunner())
    assert out == {"ok": True, "idempotent": False}       # dibangun ulang
    assert _base_state(cfg) == (0, NOBODY.pw_gid, 0o750)
    assert H.phase_of(p) == "complete"


@needs_root
def test_b81_sigkill_nyata_antara_buka_dan_bukti_selesai(tmp_path):
    """Proses helper SUNGGUHAN di-SIGKILL tepat setelah direktori dibuka,
    sebelum .install_complete: inspect bukan complete; helper berikutnya
    membangun ulang dan selesai."""
    cfgf = _cfgfile(tmp_path, allow_shared_service_user=True)
    marker = tmp_path / "terbuka"
    pre = ("import os, time\n_real = H._atomic_write\n"
           "def _aw(path, data, mode):\n"
           f"    if os.path.basename(str(path)) == {H.COMPLETE!r}:\n"
           f"        open({str(marker)!r}, 'w').close(); time.sleep(60)\n"
           "    return _real(path, data, mode)\n"
           "H._atomic_write = _aw\n")
    code = (f"import os, sys; sys.path.insert(0, {ROOT!r})\n"
            "from provisioner import mt5_provision_helper as H\n" + pre +
            "\nsys.exit(H.main(['install','--code','C001','--port','18812',"
            f"'--provision-id','{PA}'], config_path={str(cfgf)!r}))")
    pr = subprocess.Popen([sys.executable, "-c", code],
                          stdout=subprocess.PIPE, text=True)
    end = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert marker.exists()
    pr.kill()
    pr.wait()
    cfg = H.HelperConfig.load(str(cfgf))
    p = H.paths(cfg, "C001")
    st = os.lstat(p["base"])
    assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == \
        (0, NOBODY.pw_gid, 0o750)                         # sudah dibuka
    assert not p["complete"].exists()
    assert H.op_inspect(cfg, "C001", PA)["phase"] == "incomplete"
    rc, out = _main(cfgf)
    assert rc == 0 and out == {"ok": True, "idempotent": False}, out
    assert H.op_inspect(cfg, "C001", PA)["phase"] == "complete"
    rc, out = _main(cfgf)
    assert rc == 0 and out["idempotent"] is True


def test_b81_bukti_selesai_mencatat_keadaan_direktori(tmp_path):
    """Tanpa root (chown=False): .install_complete memuat base_uid/gid/mode
    sesuai fstat; perubahan mode sesudahnya membatalkan status selesai."""
    tpl = tmp_path / "t"
    tpl.mkdir()
    (tpl / "a.bin").write_text("x")
    for n in ("b", "u"):
        (tmp_path / n).mkdir()
    cfg = H.HelperConfig(base_dir=tmp_path / "b", template_dir=tpl,
                         unit_dir=tmp_path / "u", service_user="x",
                         port_base=18812, max_slots=4,
                         proc_root=tmp_path / "proc",
                         lock_dir=tmp_path / "l")
    H.op_install(cfg, "C001", 18812, PA, FakeRunner(), chown=False)
    p = H.paths(cfg, "C001")
    d = json.loads(p["complete"].read_text())
    st = os.lstat(p["base"])
    assert (d["base_uid"], d["base_gid"], d["base_mode"]) == \
        (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
    assert H.install_complete(p, PA) is not None
    os.chmod(p["base"], stat.S_IMODE(st.st_mode) ^ 0o050)
    assert H.install_complete(p, PA) is None
    assert H.phase_of(p) == "incomplete"
