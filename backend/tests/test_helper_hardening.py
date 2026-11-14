"""MT5 RPC Hardening — kelompok 1: helper privileged (in-process).

Tanpa root: install memakai chown=False (kepemilikan berkas tidak diuji di
sini — sudah dicakup tes B8 yang butuh root). ID: H1–H14, FP3, RD6
(inventaris audit-all), validasi argumen."""
import json
import os

import pytest

from provisioner import mt5_provision_helper as H
from security import rpc_policy as P
from tests.test_provision_helper import FakeRunner

PA, PB = "a" * 32, "b" * 32
MID, MID2 = "c" * 32, "d" * 32
SECRET = "0123456789abcdef" * 4


def make_cfg(tmp_path, **kw):
    tpl = tmp_path / "template"
    if not tpl.exists():
        tpl.mkdir()
        (tpl / "terminal64.exe").write_text("bin")
        (tpl / "mt5server.exe").write_text("bin")
        (tpl / "ro_service.py").write_text("# ro")
    for n in ("terminals", "units"):
        (tmp_path / n).mkdir(exist_ok=True)
    base = dict(base_dir=tmp_path / "terminals", template_dir=tpl,
                unit_dir=tmp_path / "units", service_user="transcendence",
                port_base=18812, max_slots=4, proc_root=tmp_path / "proc",
                lock_dir=tmp_path / "locks")
    base.update(kw)
    return H.HelperConfig(**base)


def prod_kw(**kw):
    d = dict(production=True, service_user_mode="per_customer",
             ro_service=True, rpc_secret=SECRET,
             wine_python="/opt/wine/python.exe",
             broker_allow="192.0.2.0/24")
    d.update(kw)
    return d


def install(cfg, code="C001", port=18812, pid=PA, mode="classic"):
    return H.op_install(cfg, code, port, pid, FakeRunner(), chown=False,
                        rpc_mode=mode)


def run(cfg, argv, runner=None):
    try:
        return 0, H.dispatch(cfg, argv, runner=runner or FakeRunner())
    except H.HelperError as e:
        return e.code, {"error": e.msg}


# ------------------------------------------------- H1–H6: produksi
@pytest.mark.parametrize("override,problem", [
    ({"service_user_mode": "shared"}, "service_user_mode_bukan_per_customer"),
    ({"allow_shared_service_user": True, "service_user_mode": "shared"},
     "service_user_mode_bukan_per_customer"),
    ({"ro_service": False}, "ro_service_nonaktif"),
    ({"rpc_secret": "changeme"}, "secret_placeholder"),
    ({"rpc_secret": "ab" * 31}, "secret_too_short"),
    ({"wine_python": ""}, "wine_python_tidak_absolut"),
    ({"wine_python": "python.exe"}, "wine_python_tidak_absolut"),
    ({"broker_allow": "any"}, "broker_allow_bukan_cidr"),
])
def test_h1_h4_produksi_tidak_lengkap_operasi_mutasi_exit9(tmp_path, override,
                                                           problem):
    cfg = make_cfg(tmp_path, **prod_kw(**override))
    assert problem in H.production_problems(cfg)
    for argv in (["install", "--code", "C001", "--port", "18812",
                  "--provision-id", PA, "--rpc-mode", "ro"],
                 ["start", "--code", "C001", "--port", "18812",
                  "--expect-pid", PA],
                 ["stop", "--code", "C001", "--expect-pid", PA],
                 ["remove", "--code", "C001", "--provision-id", PA],
                 ["fence-advance", "--code", "C001", "--migration-id", MID,
                  "--gen", "1"]):
        rc, out = run(cfg, argv)
        assert rc == H.EXIT_POLICY, (argv, out)
    assert not (cfg.base_dir / "C001").exists()
    # operasi diagnostik tetap boleh
    for argv in (["inspect", "--code", "C001"], ["selfcheck"],
                 ["status", "--code", "C001", "--port", "18812",
                  "--expect-pid", PA]):
        assert run(cfg, argv)[0] == 0, argv


def test_h1_produksi_lengkap_tanpa_masalah(tmp_path):
    assert H.production_problems(make_cfg(tmp_path, **prod_kw())) == []


@pytest.mark.parametrize("bad", ["broker.example.com",
                                 "10.0.0.0/8\nExecStart=/bin/sh", ""])
def test_h4_broker_allow_tidak_sah_ditolak_semua_mode(tmp_path, bad):
    for prod in (False, True):
        kw = prod_kw(broker_allow=bad) if prod else {"broker_allow": bad}
        cfg = make_cfg(tmp_path, **kw)
        rc, _ = run(cfg, ["install", "--code", "C001", "--port", "18812",
                          "--provision-id", PA, "--rpc-mode", "classic"])
        assert rc in (H.EXIT_FAIL, H.EXIT_POLICY)
        assert not (cfg.base_dir / "C001").exists()


def test_h5_install_classic_dilarang_produksi(tmp_path):
    cfg = make_cfg(tmp_path, **prod_kw())
    rc, out = run(cfg, ["install", "--code", "C001", "--port", "18812",
                        "--provision-id", PA, "--rpc-mode", "classic"])
    assert rc == H.EXIT_POLICY, out
    # tanpa --rpc-mode di produksi -> exit 2 (tidak ada default diam-diam)
    rc, out = run(cfg, ["install", "--code", "C001", "--port", "18812",
                        "--provision-id", PA])
    assert rc == H.EXIT_ARGS, out
    assert not (cfg.base_dir / "C001").exists()
    with pytest.raises(H.HelperError) as e:
        install(cfg, mode="classic")        # jalur in-process juga ditolak
    assert e.value.code == H.EXIT_POLICY


def test_h5_non_produksi_default_lama_classic(tmp_path, monkeypatch):
    """Non-produksi: tanpa --rpc-mode tetap 'classic' (perilaku lama, dev/
    test). Produksi: wajib eksplisit (lihat test_h5 di atas)."""
    seen = {}

    def fake_install(cfg, code, port, pid, runner, chown=True,
                     rpc_mode=None):
        seen["mode"] = rpc_mode
        return {"ok": True}
    monkeypatch.setattr(H, "op_install", fake_install)
    cfg = make_cfg(tmp_path)
    rc, out = run(cfg, ["install", "--code", "C001", "--port", "18812",
                        "--provision-id", PA])
    assert rc == 0 and seen["mode"] == "classic", out
    rc, out = run(cfg, ["install", "--code", "C001", "--port", "18812",
                        "--provision-id", PA, "--rpc-mode", "ro"])
    assert rc == 0 and seen["mode"] == "ro", out


def test_h6_install_ro_produksi_dan_marker_mode(tmp_path):
    cfg = make_cfg(tmp_path, **prod_kw())
    install(cfg, mode="ro")
    p = H.paths(cfg, "C001")
    assert H._complete_mode(p, PA) == "ro"
    tok = (p["terminal"] / "rpc.token").read_text()
    assert len(tok) == 64 and SECRET not in tok


# ------------------------------------------------ H7–H10: start/stop
def test_h7_start_marker_tidak_cocok_exit3(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    r = FakeRunner()
    rc, _ = run(cfg, ["start", "--code", "C001", "--port", "18812",
                      "--expect-pid", PB], r)
    assert rc == H.EXIT_OWNERSHIP and r.calls == []
    rc, _ = run(cfg, ["start", "--code", "C001", "--port", "18812",
                      "--expect-pid", PA], r)
    assert rc == 0 and r.calls and "enable" in r.calls[-1]


def test_h8_start_mode_tidak_cocok_exit3_dan_classic_produksi_exit9(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg, mode="classic")
    rc, _ = run(cfg, ["start", "--code", "C001", "--port", "18812",
                      "--expect-pid", PA, "--expect-mode", "ro"])
    assert rc == H.EXIT_OWNERSHIP
    # unit classic sisa lama TIDAK boleh di-start setelah produksi aktif
    pcfg = make_cfg(tmp_path, **prod_kw())
    r = FakeRunner()
    rc, out = run(pcfg, ["start", "--code", "C001", "--port", "18812",
                         "--expect-pid", PA], r)
    assert rc == H.EXIT_POLICY, out
    assert r.calls == []


def test_h8b_complete_tanpa_rpc_mode_dianggap_tidak_diketahui(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg, mode="classic")
    p = H.paths(cfg, "C001")
    data = json.loads(p["complete"].read_text())
    del data["rpc_mode"]
    H._atomic_write(p["complete"], json.dumps(data), 0o600)
    pcfg = make_cfg(tmp_path, **prod_kw())
    rc, _ = run(pcfg, ["start", "--code", "C001", "--port", "18812",
                       "--expect-pid", PA])
    assert rc == H.EXIT_POLICY


def test_h9_produksi_wajib_expect_pid(tmp_path):
    cfg = make_cfg(tmp_path, **prod_kw())
    install(cfg, mode="ro")
    assert run(cfg, ["start", "--code", "C001", "--port", "18812"])[0] == \
        H.EXIT_ARGS
    assert run(cfg, ["stop", "--code", "C001"])[0] == H.EXIT_ARGS


def test_h10_stop_expect_pid(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    r = FakeRunner()
    rc, _ = run(cfg, ["stop", "--code", "C001", "--expect-pid", PB], r)
    assert rc == H.EXIT_OWNERSHIP and r.calls == []
    rc, out = run(cfg, ["stop", "--code", "C001", "--expect-pid", PA], r)
    assert rc == 0 and out["was_active"] is False
    # instalasi tidak ada -> idempoten ok
    rc, out = run(cfg, ["stop", "--code", "C002", "--expect-pid", PB])
    assert rc == 0, out


def test_h10b_stop_jalur_lama_urutan_panggilan_tidak_berubah(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    r = FakeRunner()
    assert run(cfg, ["stop", "--code", "C001"], r)[0] == 0
    assert len(r.calls) == 1 and "disable" in r.calls[0]


def test_status_expect_pid_dan_mode(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    rc, out = run(cfg, ["status", "--code", "C001", "--port", "18812",
                        "--expect-pid", PA])
    assert rc == 0 and out["marker_match"] is True and \
        out["rpc_mode"] == "classic" and out["installed"] is True
    rc, out = run(cfg, ["status", "--code", "C001", "--port", "18812",
                        "--expect-pid", PB])
    assert out["marker_match"] is False and out["installed"] is False \
        and out["running"] is False


# --------------------------------------------------- fence (in-process)
def test_fence_advance_release_aturan(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)

    def fa(mid, gen):
        return run(cfg, ["fence-advance", "--code", "C001",
                         "--migration-id", mid, "--gen", str(gen)])
    rc, out = fa(MID, 1)
    assert rc == 0 and out["fence"] == {"migration_id": MID, "gen": 1,
                                        "released": False}
    assert out["marker"] == PA and out["rpc_mode"] == "classic"
    assert fa(MID, 1)[0] == 0                       # idempoten
    assert fa(MID, 3)[0] == 0
    assert fa(MID, 2)[0] == H.EXIT_FENCE            # mundur
    assert fa(MID2, 9)[0] == H.EXIT_FENCE           # FN6: migrasi lain
    st = os.lstat(cfg.base_dir / H.FENCE_DIR / "C001")
    assert oct(st.st_mode & 0o777) == "0o600"
    # release hanya pemegang terkini
    assert run(cfg, ["fence-release", "--code", "C001", "--migration-id",
                     MID, "--gen", "2"])[0] == H.EXIT_FENCE
    assert run(cfg, ["fence-release", "--code", "C001", "--migration-id",
                     MID, "--gen", "3"])[0] == 0
    assert run(cfg, ["fence-release", "--code", "C001", "--migration-id",
                     MID, "--gen", "3"])[0] == 0    # idempoten
    assert fa(MID, 4)[0] == H.EXIT_FENCE            # sudah dilepas
    assert fa(MID2, 1)[0] == 0                      # migrasi baru boleh


def test_fence_operasi_tanpa_fence_ditolak_selama_migrasi_aktif(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    assert run(cfg, ["fence-advance", "--code", "C001", "--migration-id",
                     MID, "--gen", "1"])[0] == 0
    r = FakeRunner()
    for argv in (["stop", "--code", "C001"],
                 ["start", "--code", "C001", "--port", "18812"],
                 ["remove", "--code", "C001", "--provision-id", PA],
                 ["install", "--code", "C001", "--port", "18812",
                  "--provision-id", PA, "--rpc-mode", "classic"]):
        assert run(cfg, argv, r)[0] == H.EXIT_FENCE, argv
    assert r.calls == []
    assert run(cfg, ["stop", "--code", "C001", "--migration-id", MID,
                     "--fence", "1"], r)[0] == 0


def test_fence_tanpa_advance_ditolak_dan_berkas_rusak_fail_closed(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    assert run(cfg, ["stop", "--code", "C001", "--migration-id", MID,
                     "--fence", "1"])[0] == H.EXIT_FENCE
    H._fence_root(cfg)
    (cfg.base_dir / H.FENCE_DIR / "C001").write_text("{rusak")
    os.chmod(cfg.base_dir / H.FENCE_DIR / "C001", 0o600)
    assert run(cfg, ["stop", "--code", "C001"])[0] == H.EXIT_FENCE
    assert run(cfg, ["fence-advance", "--code", "C001", "--migration-id",
                     MID, "--gen", "1"])[0] == H.EXIT_FENCE
    out = run(cfg, ["inspect", "--code", "C001"])[1]
    assert out["fence"] == {"invalid": True}


@pytest.mark.parametrize("argv", [
    ["fence-advance", "--code", "C001", "--migration-id", MID],
    ["fence-advance", "--code", "C001", "--gen", "1"],
    ["fence-advance", "--code", "C001", "--migration-id", "X" * 32,
     "--gen", "1"],
    ["fence-advance", "--code", "C001", "--migration-id", MID, "--gen", "0"],
    ["fence-advance", "--code", "C001", "--migration-id", MID, "--gen", "1",
     "--fence", "1"],
    ["stop", "--code", "C001", "--migration-id", MID],
    ["stop", "--code", "C001", "--fence", "1"],
    ["stop", "--code", "C001", "--gen", "1"],
    ["inspect", "--code", "C001", "--migration-id", MID, "--fence", "1"],
    ["remove", "--code", "C001", "--provision-id", PA, "--expect-pid", PA],
    ["start", "--code", "C001", "--port", "18812", "--expect-pid", "x"],
    ["stop", "--code", "C001", "--expect-mode", "ro"],
    ["status", "--code", "C001", "--port", "18812", "--nonce", "a" * 64],
    ["selfcheck", "--code", "C001"],
    ["audit-all", "--port", "18812"],
    ["selfcheck", "--nonce", "A" * 64],
    ["inspect"],
])
def test_argumen_hardening_tidak_sah_exit2(tmp_path, argv):
    cfg = make_cfg(tmp_path)
    assert run(cfg, argv)[0] == H.EXIT_ARGS


# ------------------------------------------------ H11 / FP3: selfcheck
def test_h11_fp3_selfcheck_tanpa_secret_dan_bukti_cocok(tmp_path, capsys):
    cfg = make_cfg(tmp_path, **prod_kw())
    nonce = P.new_nonce()
    rc, out = run(cfg, ["selfcheck", "--nonce", nonce])
    assert rc == 0
    blob = json.dumps(out)
    assert SECRET not in blob and SECRET[:16] not in blob
    assert P.verify_selfcheck_proof(SECRET, nonce, out["secret_proof"])
    assert not P.verify_selfcheck_proof("f" * 64, nonce, out["secret_proof"])
    assert out["production"] is True and out["production_problems"] == []
    assert out["broker_allow_mode"] == "cidr" and out["secret_ok"] is True
    assert out["op_timeout_seconds"] == 45.0
    # tanpa nonce: tidak ada bukti
    assert "secret_proof" not in run(cfg, ["selfcheck"])[1]


def test_selfcheck_melaporkan_masalah_tanpa_nilai(tmp_path):
    cfg = make_cfg(tmp_path, **prod_kw(rpc_secret="changeme",
                                       broker_allow="any"))
    out = run(cfg, ["selfcheck", "--nonce", P.new_nonce()])[1]
    assert "secret_placeholder" in out["production_problems"]
    assert "broker_allow_bukan_cidr" in out["production_problems"]
    assert out["broker_allow_mode"] == "any"
    assert "changeme" not in json.dumps(out)


# ---------------------------------------------- H12–H14: audit-all
def test_h12_audit_all_inventaris_dan_klasifikasi_mentah(tmp_path):
    cfg = make_cfg(tmp_path, ro_service=True, rpc_secret=SECRET,
                   wine_python="/opt/wine/python.exe")
    install(cfg, "C001", 18812, PA, mode="ro")
    install(cfg, "C002", 18813, PB, mode="classic")
    # parsial di C003, unit yatim C004, entri asing, tombstone C005
    (cfg.base_dir / "C003").mkdir()
    (cfg.base_dir / "C003" / H.PARTIAL).mkdir()
    (cfg.unit_dir / f"{H.UNIT_PREFIX}c004.service").write_text(
        "x (C004, localhost:18815)")
    (cfg.base_dir / "sampah").mkdir()
    (cfg.unit_dir / f"{H.UNIT_PREFIX}bukan_kode.service").write_text("x")
    tomb = H._tomb_root(cfg)
    (tomb / f"C005.{PA}").mkdir()
    rc, out = run(cfg, ["audit-all"])
    assert rc == 0
    t = {x["code"]: x for x in out["terminals"]}
    assert t["C001"]["phase"] == "complete" and \
        t["C001"]["complete"]["rpc_mode"] == "ro" and \
        t["C001"]["run_script_kind"] == "ro" and t["C001"]["marker"] == PA
    assert t["C002"]["complete"]["rpc_mode"] == "classic" and \
        t["C002"]["run_script_kind"] == "classic"
    assert t["C003"]["partial"] is True and t["C003"]["phase"] == "unclaimed"
    assert t["C004"]["phase"] == "orphan_unit"
    assert t["C005"]["tombstones"] == 1 and t["C005"]["phase"] == "removing"
    assert "base:sampah" in out["unknown_entries"]
    assert f"unit:{H.UNIT_PREFIX}bukan_kode.service" in out["unknown_entries"]
    assert out["port_window"] == [18812, 18816]
    assert SECRET not in json.dumps(out)


def test_h13_audit_skrip_diubah_unknown(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    script = H.paths(cfg, "C001")["terminal"] / "run-terminal.sh"
    script.write_text(script.read_text() + "\n# disisipi\n")
    out = run(cfg, ["audit-all"])[1]
    assert out["terminals"][0]["run_script_kind"] == "unknown"
    script.unlink()
    out = run(cfg, ["audit-all"])[1]
    assert out["terminals"][0]["run_script_kind"] == "missing"


def test_h14_inspect_menambah_marker_mode_fence_unclean(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    out = run(cfg, ["inspect", "--code", "C001", "--provision-id", PA])[1]
    assert out["marker"] == PA and out["rpc_mode"] == "classic"
    assert out["fence"] is None and out["unclean"] is False
    assert out["owner_match"] is True


# --------------------------------------------- listener IPv6 (K3-E)
# header persis kernel (tcp4_seq_show / tcp6_seq_show)
HDR4 = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when"
        " retrnsmt   uid  timeout inode\n")
HDR6 = ("  sl  local_address                         remote_address"
        "                        st tx_queue rx_queue tr tm->when retrnsmt"
        "   uid  timeout inode\n")
def _proc(tmp_path, tcp="", tcp6=""):
    net = tmp_path / "proc" / "net"
    net.mkdir(parents=True, exist_ok=True)
    (net / "tcp").write_text(HDR4 + tcp)
    (net / "tcp6").write_text(HDR6 + tcp6)


def test_listener_ipv6_loopback_terdeteksi(tmp_path):
    port = 18812
    line6 = (f"   0: {H._TCP6_LOOPBACK}:{port:04X} "
             f"00000000000000000000000000000000:0000 0A 00000000:00000000 "
             f"00:00000000 00000000     0        0 4242 1\n")
    _proc(tmp_path, tcp6=line6)
    assert H._listening_inodes(tmp_path / "proc", port) == ["4242"]
    inv = H._Inv()
    assert H._listening_ports_checked(tmp_path / "proc", 18812, 18816,
                                      inv) == {18812: ["4242"]}
    assert inv.errors == []
    line4 = (f"   0: 0100007F:{port + 1:04X} 00000000:0000 0A 00000000:"
             f"00000000 00:00000000 00000000     0        0 77 1\n")
    _proc(tmp_path, tcp=line4, tcp6=line6)
    inv = H._Inv()
    assert H._listening_ports_checked(tmp_path / "proc", 18812, 18816,
                                      inv) == {18812: ["4242"], 18813: ["77"]}
    # di luar jendela tidak dilaporkan
    assert H._listening_ports_checked(tmp_path / "proc", 18900, 18904,
                                      inv) == {}
    assert inv.errors == []


# ------------------------------- review G1 fix 1: status --expect-pid
def test_status_produksi_tanpa_expect_pid_exit2(tmp_path):
    cfg = make_cfg(tmp_path, **prod_kw())
    rc, out = run(cfg, ["status", "--code", "C001", "--port", "18812"])
    assert rc == H.EXIT_ARGS, out
    assert "expect-pid" in out["error"]
    with pytest.raises(H.HelperError) as e:      # jalur in-process juga
        H.op_status(cfg, "C001", 18812, FakeRunner())
    assert e.value.code == H.EXIT_ARGS


def test_status_produksi_dengan_expect_pid_dan_mismatch(tmp_path):
    cfg = make_cfg(tmp_path, ro_service=True, rpc_secret=SECRET,
                   wine_python="/opt/wine/python.exe")
    install(cfg, mode="ro")
    cfg = make_cfg(tmp_path, **prod_kw())        # instalasi yang sama
    rc, out = run(cfg, ["status", "--code", "C001", "--port", "18812",
                        "--expect-pid", PA])
    assert rc == 0 and out["marker_match"] is True, out
    rc, out = run(cfg, ["status", "--code", "C001", "--port", "18812",
                        "--expect-pid", PB])
    assert rc == 0 and out["marker_match"] is False, out


def test_status_non_produksi_tanpa_expect_pid_tetap_boleh(tmp_path):
    cfg = make_cfg(tmp_path)
    assert run(cfg, ["status", "--code", "C001", "--port", "18812"])[0] == 0


# ----------------------------- review G1 fix 2: audit-all fail-closed
def _proc_ok(tmp_path):
    """/proc palsu yang LENGKAP: net/tcp + net/tcp6 + satu pid tanpa
    socket target."""
    _proc(tmp_path)
    (tmp_path / "proc" / "1" / "fd").mkdir(parents=True)
    (tmp_path / "proc" / "1" / "cgroup").write_text("0::/init.scope\n")


def _audit(cfg):
    rc, out = run(cfg, ["audit-all"])
    assert rc == 0, out
    return out


def test_audit_lengkap_audit_complete_true(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    _proc_ok(tmp_path)
    out = _audit(cfg)
    assert out["audit_complete"] is True, out["inventory_errors"]
    assert out["inventory_errors"] == []
    assert P.audit_inventory_problems(out) == []


def test_audit_proc_tidak_tersedia_fail_closed(tmp_path):
    cfg = make_cfg(tmp_path)            # proc_root = tmp/proc tidak ada
    install(cfg)
    out = _audit(cfg)
    assert out["audit_complete"] is False
    assert "proc_net_tcp:tidak_ada" in out["inventory_errors"]
    assert "proc_net_tcp6:tidak_ada" in out["inventory_errors"]
    probs = P.audit_inventory_problems(out)
    assert "audit_incomplete" in probs
    assert "inventory_error:proc_net_tcp:tidak_ada" in probs


def test_audit_proc_bukan_direktori_fail_closed(tmp_path):
    proc = tmp_path / "procfile"
    proc.write_text("x")
    cfg = make_cfg(tmp_path, proc_root=proc)
    out = _audit(cfg)
    assert out["audit_complete"] is False
    assert any(e.startswith("proc_net_tcp:") for e in out["inventory_errors"])


def test_audit_tcp6_hilang_fail_closed(tmp_path):
    cfg = make_cfg(tmp_path)
    _proc_ok(tmp_path)
    (tmp_path / "proc" / "net" / "tcp6").unlink()
    out = _audit(cfg)
    assert out["audit_complete"] is False
    assert out["inventory_errors"] == ["proc_net_tcp6:tidak_ada"]


def test_audit_tcp_kosong_tanpa_header_fail_closed(tmp_path):
    cfg = make_cfg(tmp_path)
    _proc_ok(tmp_path)
    (tmp_path / "proc" / "net" / "tcp").write_text("")
    out = _audit(cfg)
    assert "proc_net_tcp:kosong" in out["inventory_errors"]
    assert out["audit_complete"] is False


def _deny_read(monkeypatch, target):
    """Root melewati DAC, jadi izin ditolak disimulasikan pada read_text."""
    real = H.Path.read_text

    def fake(self, *a, **k):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)
    monkeypatch.setattr(H.Path, "read_text", fake)


@pytest.mark.parametrize("name", ["tcp", "tcp6"])
def test_audit_proc_net_izin_ditolak_fail_closed(tmp_path, monkeypatch, name):
    cfg = make_cfg(tmp_path)
    _proc_ok(tmp_path)
    _deny_read(monkeypatch, tmp_path / "proc" / "net" / name)
    out = _audit(cfg)
    assert out["audit_complete"] is False
    assert f"proc_net_{name}:PermissionError" in out["inventory_errors"]


def test_audit_proc_listdir_izin_ditolak_fail_closed(tmp_path, monkeypatch):
    port = 18812
    line = (f"   0: 0100007F:{port:04X} 00000000:0000 0A 00000000:"
            f"00000000 00:00000000 00000000     0        0 99 1\n")
    _proc(tmp_path, tcp=line)
    cfg = make_cfg(tmp_path)
    real = H.os.listdir
    proc = tmp_path / "proc"

    def fake(d):
        if H.Path(d) == proc:
            raise PermissionError(13, "Permission denied", str(d))
        return real(d)
    monkeypatch.setattr(H.os, "listdir", fake)
    out = _audit(cfg)
    assert "proc:PermissionError" in out["inventory_errors"]
    assert out["audit_complete"] is False


def test_audit_proc_pid_fd_izin_ditolak_fail_closed(tmp_path, monkeypatch):
    port = 18812
    line = (f"   0: 0100007F:{port:04X} 00000000:0000 0A 00000000:"
            f"00000000 00:00000000 00000000     0        0 99 1\n")
    _proc(tmp_path, tcp=line)
    fd = tmp_path / "proc" / "77" / "fd"
    fd.mkdir(parents=True)
    real = H.Path.iterdir

    def fake(self):
        if self == fd:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)
    monkeypatch.setattr(H.Path, "iterdir", fake)
    out = _audit(make_cfg(tmp_path))
    assert "proc_pid_fd:PermissionError" in out["inventory_errors"]
    assert out["audit_complete"] is False


def test_audit_proses_keluar_saat_scan_bukan_galat(tmp_path, monkeypatch):
    port = 18812
    line = (f"   0: 0100007F:{port:04X} 00000000:0000 0A 00000000:"
            f"00000000 00:00000000 00000000     0        0 99 1\n")
    _proc(tmp_path, tcp=line)
    (tmp_path / "proc" / "77").mkdir()       # tanpa fd: proses sudah keluar
    out = _audit(make_cfg(tmp_path))
    assert out["inventory_errors"] == [] and out["audit_complete"] is True
    assert out["listeners"] == [{"port": 18812, "owner_units": []}]


def test_audit_pwd_gagal_fail_closed(tmp_path, monkeypatch):
    import pwd

    def boom():
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(pwd, "getpwall", boom)
    cfg = make_cfg(tmp_path)
    _proc_ok(tmp_path)
    out = _audit(cfg)
    assert out["inventory_errors"] == ["pwd:PermissionError"]
    assert out["audit_complete"] is False


@pytest.mark.parametrize("which", ["base_dir", "unit_dir"])
def test_audit_direktori_wajib_hilang_fail_closed(tmp_path, which):
    cfg = make_cfg(tmp_path)
    _proc_ok(tmp_path)
    getattr(cfg, which).rmdir()
    out = _audit(cfg)
    assert f"{which}:tidak_ada" in out["inventory_errors"]
    assert out["audit_complete"] is False


def test_audit_direktori_izin_ditolak_fail_closed(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    _proc_ok(tmp_path)
    real = H.os.listdir

    def fake(d):
        if H.Path(d) == cfg.unit_dir:
            raise PermissionError(13, "Permission denied", str(d))
        return real(d)
    monkeypatch.setattr(H.os, "listdir", fake)
    out = _audit(cfg)
    assert out["inventory_errors"] == ["unit_dir:PermissionError"]


def test_audit_systemctl_gagal_fail_closed(tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    _proc_ok(tmp_path)

    class Boom(FakeRunner):
        def run(self, argv, check=True, **kw):
            if "is-active" in argv:
                raise OSError("systemctl tidak dapat dijalankan")
            return super().run(argv, check=check, **kw)
    out = _audit_runner(cfg, Boom())
    assert out["inventory_errors"] == ["systemctl_C001:OSError"]
    assert out["terminals"][0]["unit_active"] is None


def _audit_runner(cfg, runner):
    rc, out = run(cfg, ["audit-all"], runner=runner)
    assert rc == 0, out
    return out


def test_audit_lock_dir_opsional_tidak_ada_bukan_galat(tmp_path):
    cfg = make_cfg(tmp_path)
    _proc_ok(tmp_path)
    assert not cfg.lock_dir.exists()
    assert _audit(cfg)["audit_complete"] is True


# ------------- final review G1: baris /proc/net/tcp{,6} rusak fail-closed
_V4_OK = ("   0: 0100007F:497C 00000000:0000 0A 00000000:00000000 "
          "00:00000000 00000000     0        0 99 1\n")
_V6_OK = (f"   0: {H._TCP6_LOOPBACK}:497D "
          "00000000000000000000000000000000:0000 0A 00000000:00000000 "
          "00:00000000 00000000     0        0 4242 1\n")


def _scan(tmp_path, tcp="", tcp6=""):
    _proc(tmp_path, tcp=tcp, tcp6=tcp6)
    inv = H._Inv()
    found = H._listening_ports_checked(tmp_path / "proc", 18812, 18816, inv)
    return found, inv.errors


def test_proc_net_baris_valid_tanpa_galat(tmp_path):
    found, errs = _scan(tmp_path, tcp=_V4_OK, tcp6=_V6_OK)
    assert errs == []
    assert found == {18812: ["99"], 18813: ["4242"]}
    # baris valid non-LISTEN / di luar jendela: diabaikan TANPA galat
    found, errs = _scan(tmp_path, tcp=_V4_OK.replace(" 0A ", " 01 ")
                        + _V4_OK.replace("497C", "4A00"))
    assert errs == [] and found == {}


@pytest.mark.parametrize("name", ["tcp", "tcp6"])
@pytest.mark.parametrize("line", [
    "   0: 0100007F:497C 00000000:0000 0A\n",            # berhenti di state
    ("   0: 0100007F:497C 00000000:0000 0A 00000000:00000000 "
     "00:00000000 00000000     0        0\n"),              # tanpa inode
    "   0:\n",
    "x\n",
])
def test_proc_net_kolom_tidak_lengkap_fail_closed(tmp_path, name, line):
    kw = {name: line}
    found, errs = _scan(tmp_path, **kw)
    assert errs == [f"proc_net_{name}:kolom_tidak_lengkap"]
    assert found == {}


def test_proc_net_baris_kosong_di_tengah_fail_closed(tmp_path):
    _, errs = _scan(tmp_path, tcp="\n" + _V4_OK)
    assert errs == ["proc_net_tcp:kolom_tidak_lengkap"]


_RUSAK_V4 = [
    _V4_OK.replace("0100007F:497C", "0100007F:ZZZZ"),     # port bukan hex
    _V4_OK.replace("0100007F:497C", "0100007F"),          # tanpa port
    _V4_OK.replace("0100007F:497C", "7F:497C"),           # alamat pendek
    _V4_OK.replace("00000000:0000 0A", "00000000:00 0A"),  # remote rusak
    _V4_OK.replace(" 0A ", " LISTEN "),                   # state bukan hex
    _V4_OK.replace(" 99 1", " 9x9 1"),                    # inode bukan angka
    _V4_OK.replace("   0:", "   zz:"),                    # sl rusak
    _V4_OK.replace(" 0A ", " 01 ").replace("497C", "ZZZZ"),  # non-LISTEN
]


@pytest.mark.parametrize("line", _RUSAK_V4)
def test_proc_net_tcp_baris_rusak_fail_closed(tmp_path, line):
    found, errs = _scan(tmp_path, tcp=line + _V4_OK)
    assert errs == ["proc_net_tcp:baris_rusak"], (line, errs)
    assert found == {18812: ["99"]}      # baris valid tetap terbaca


def test_proc_net_tcp6_alamat_v4_dianggap_rusak(tmp_path):
    _, errs = _scan(tmp_path, tcp6=_V4_OK)
    assert errs == ["proc_net_tcp6:baris_rusak"]


def test_audit_baris_tcp_rusak_audit_complete_false_dan_readiness_merah(
        tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    _proc_ok(tmp_path)
    (tmp_path / "proc" / "net" / "tcp").write_text(
        HDR4 + _RUSAK_V4[0] + _V4_OK)
    (tmp_path / "proc" / "net" / "tcp6").write_text(HDR6 + "   0: 00:00\n")
    out = _audit(cfg)
    assert out["audit_complete"] is False
    assert out["inventory_errors"] == ["proc_net_tcp:baris_rusak",
                                       "proc_net_tcp6:kolom_tidak_lengkap"]
    probs = P.audit_inventory_problems(out)
    assert "audit_incomplete" in probs
    assert "inventory_error:proc_net_tcp:baris_rusak" in probs
    assert "inventory_error:proc_net_tcp6:kolom_tidak_lengkap" in probs


# ---------------- final edge-case review G1: header /proc/net/tcp{,6}
def _scan_raw(tmp_path, tcp, tcp6):
    net = tmp_path / "proc" / "net"
    net.mkdir(parents=True, exist_ok=True)
    (net / "tcp").write_text(tcp)
    (net / "tcp6").write_text(tcp6)
    inv = H._Inv()
    found = H._listening_ports_checked(tmp_path / "proc", 18812, 18816, inv)
    return found, inv.errors


def test_header_kernel_valid_termasuk_padding_tanpa_galat(tmp_path):
    pad4 = HDR4.rstrip("\n") + " " * 53 + "\n"     # padding seperti kernel
    found, errs = _scan_raw(tmp_path, pad4 + _V4_OK, HDR6 + _V6_OK)
    assert errs == [] and found == {18812: ["99"], 18813: ["4242"]}


def test_header_header_saja_tanpa_baris_valid(tmp_path):
    found, errs = _scan_raw(tmp_path, HDR4, HDR6)
    assert errs == [] and found == {}


@pytest.mark.parametrize("name", ["tcp", "tcp6"])
def test_header_hilang_fail_closed_data_tetap_dipindai(tmp_path, name):
    row = _V4_OK if name == "tcp" else _V6_OK
    tcp = row if name == "tcp" else HDR4
    tcp6 = row if name == "tcp6" else HDR6
    found, errs = _scan_raw(tmp_path, tcp, tcp6)
    assert errs == [f"proc_net_{name}:header_tidak_ada"]
    port = 18812 if name == "tcp" else 18813
    assert found[port]                    # baris pertama tidak hilang


_HDR4_RUSAK = [
    "\n",                                               # baris kosong
    "garbage\n",
    HDR4.replace("inode", ""),                          # kolom kurang
    HDR4.replace("inode", "inode extra"),               # kolom lebih
    HDR4.replace("rem_address", "remote_address"),      # header tcp6
    HDR4.replace("st ", "state "),
    HDR4.replace("local_address", "LOCAL_ADDRESS"),
    HDR4.replace("tm->when", "tm-when"),
]


@pytest.mark.parametrize("hdr", _HDR4_RUSAK)
def test_header_tcp_rusak_fail_closed(tmp_path, hdr):
    found, errs = _scan_raw(tmp_path, hdr + _V4_OK, HDR6)
    assert errs == ["proc_net_tcp:header_rusak"], (hdr, errs)
    assert found == {18812: ["99"]}       # baris valid sesudahnya terbaca


@pytest.mark.parametrize("hdr", [
    HDR4,                                               # header tcp4 di tcp6
    HDR6.replace("remote_address", "rem_addr"),
    HDR6.replace("uid", ""),
    "x\n",
])
def test_header_tcp6_rusak_fail_closed(tmp_path, hdr):
    _, errs = _scan_raw(tmp_path, HDR4, hdr + _V6_OK)
    assert errs == ["proc_net_tcp6:header_rusak"], (hdr, errs)


def test_audit_header_hilang_rusak_audit_complete_false_readiness_merah(
        tmp_path):
    cfg = make_cfg(tmp_path)
    install(cfg)
    _proc_ok(tmp_path)
    (tmp_path / "proc" / "net" / "tcp").write_text(_V4_OK)
    (tmp_path / "proc" / "net" / "tcp6").write_text("sl local\n" + _V6_OK)
    out = _audit(cfg)
    assert out["audit_complete"] is False
    assert out["inventory_errors"] == ["proc_net_tcp:header_tidak_ada",
                                       "proc_net_tcp6:header_rusak"]
    probs = P.audit_inventory_problems(out)
    assert "audit_incomplete" in probs
    assert "inventory_error:proc_net_tcp:header_tidak_ada" in probs
    assert "inventory_error:proc_net_tcp6:header_rusak" in probs
