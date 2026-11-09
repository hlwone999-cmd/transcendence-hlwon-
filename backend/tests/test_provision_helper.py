"""Helper provisioning privileged (Tahap 3B) + klien sudo di API.

Helper dijalankan dengan direktori sementara & runner palsu (tanpa
sudo/systemd nyata). Membuktikan: validasi ketat, tanpa shell, path dari
konfigurasi, penolakan port/direktori milik customer lain, penghapusan
HANYA dengan bukti kepemilikan, dan pengecekan listener milik unit.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from provisioner import mt5_provision_helper as H
from services import provisioner as P

PID_A = "a" * 32
PID_B = "b" * 32


class FakeRunner:
    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def run(self, argv, check=True):
        assert os.path.isabs(argv[0])
        self.calls.append(list(argv))
        if "is-active" in argv:
            return 3        # systemd: unit TIDAK aktif (setelah stop)
        return self.rc


@pytest.fixture()
def hcfg(tmp_path):
    tpl = tmp_path / "template"
    tpl.mkdir()
    (tpl / "terminal64.exe").write_text("bin")
    (tpl / "mt5server.exe").write_text("bin")
    (tmp_path / "terminals").mkdir()
    (tmp_path / "units").mkdir()
    return H.HelperConfig(base_dir=tmp_path / "terminals", template_dir=tpl,
                          unit_dir=tmp_path / "units",
                          service_user="transcendence", port_base=18812,
                          max_slots=4, proc_root=tmp_path / "proc")


@pytest.mark.parametrize("argv", [
    ["install", "--code", "C1", "--port", "18812", "--provision-id", PID_A],
    ["install", "--code", "../etc", "--port", "18812",
     "--provision-id", PID_A],
    ["install", "--code", "C001;rm -rf /", "--port", "18812",
     "--provision-id", PID_A],
    ["install", "--code", "C001", "--port", "22", "--provision-id", PID_A],
    ["install", "--code", "C001", "--port", "18816", "--provision-id", PID_A],
    ["install", "--code", "C001", "--port", "18812", "--provision-id", "x"],
    ["remove", "--code", "C001"],
    ["exec", "--code", "C001"],
    ["install", "--code", "C001", "--port", "18812", "--provision-id",
     PID_A, "--unit", "evil"],
    ["status", "--code", "C001", "--port", "abc"],
])
def test_helper_menolak_argumen_tidak_sah(hcfg, argv):
    with pytest.raises(H.HelperError) as ei:
        H.dispatch(hcfg, argv, runner=FakeRunner())
    assert ei.value.code == H.EXIT_ARGS
    assert list(hcfg.base_dir.iterdir()) == []


def test_helper_install_menulis_dari_template_sendiri(hcfg):
    r = FakeRunner()
    out = H.op_install(hcfg, "C001", 18812, PID_A, r, chown=False)
    assert out["ok"]
    base = hcfg.base_dir / "C001"
    assert (base / ".provision_id").read_text() == PID_A
    assert oct((base / ".provision_id").stat().st_mode)[-3:] == "600"
    assert (base / "terminal" / "terminal64.exe").exists()
    run = (base / "terminal" / "run-terminal.sh").read_text()
    assert "--host 127.0.0.1 --port 18812" in run
    unit = (hcfg.unit_dir / "transcendence-mt5-c001.service").read_text()
    assert "User=transcendence" in unit and "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert r.calls == [["/usr/bin/systemctl", "daemon-reload"]]
    # idempoten utk provision_id yang sama
    assert H.op_install(hcfg, "C001", 18812, PID_A, r,
                        chown=False)["idempotent"] is True


def test_helper_install_menolak_milik_lain_dan_port_customer_lain(hcfg):
    H.op_install(hcfg, "C001", 18812, PID_A, FakeRunner(), chown=False)
    with pytest.raises(H.HelperError) as ei:
        H.op_install(hcfg, "C001", 18812, PID_B, FakeRunner(), chown=False)
    assert ei.value.code == H.EXIT_OWNERSHIP
    with pytest.raises(H.HelperError) as ei:
        H.op_install(hcfg, "C002", 18812, PID_B, FakeRunner(), chown=False)
    assert ei.value.code == H.EXIT_OWNERSHIP       # port dipakai C001
    foreign = hcfg.base_dir / "C003"
    foreign.mkdir()
    (foreign / "data.bin").write_text("x")          # tanpa penanda
    with pytest.raises(H.HelperError):
        H.op_install(hcfg, "C003", 18814, PID_B, FakeRunner(), chown=False)
    assert (foreign / "data.bin").exists()


def test_helper_remove_hanya_dengan_bukti_kepemilikan(hcfg):
    H.op_install(hcfg, "C001", 18812, PID_A, FakeRunner(), chown=False)
    with pytest.raises(H.HelperError) as ei:
        H.op_remove(hcfg, "C001", PID_B, FakeRunner())
    assert ei.value.code == H.EXIT_OWNERSHIP
    assert (hcfg.base_dir / "C001").exists()
    r = FakeRunner()
    assert H.op_remove(hcfg, "C001", PID_A, r)["ok"]
    assert not (hcfg.base_dir / "C001").exists()
    assert not (hcfg.unit_dir / "transcendence-mt5-c001.service").exists()
    assert ["/usr/bin/systemctl", "disable", "--now",
            "transcendence-mt5-c001.service"] in r.calls


def test_helper_tolak_symlink_direktori_customer(hcfg, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (hcfg.base_dir / "C001").symlink_to(target)
    with pytest.raises(H.HelperError):
        H.op_install(hcfg, "C001", 18812, PID_A, FakeRunner(), chown=False)


def test_helper_listener_harus_milik_unit_customer(hcfg):
    proc = hcfg.proc_root
    (proc / "net").mkdir(parents=True)
    hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when" \
          " retrnsmt   uid  timeout inode\n"
    line = ("   0: 0100007F:4974 00000000:0000 0A 00000000:00000000 00:00000000"
            " 00000000  1000        0 55555 1 0000000000000000 100 0 0 10 0\n")
    (proc / "net" / "tcp").write_text(hdr + line)     # 0x4974 = 18804
    pid = proc / "4242"
    (pid / "fd").mkdir(parents=True)
    os.symlink("socket:[55555]", pid / "fd" / "3")
    (pid / "cgroup").write_text(
        "0::/system.slice/transcendence-mt5-c002.service\n")
    assert H.socket_owned_by_unit(hcfg, "transcendence-mt5-c002", 18804)
    # listener yang sama BUKAN milik unit customer lain
    assert not H.socket_owned_by_unit(hcfg, "transcendence-mt5-c001", 18804)
    assert not H.socket_owned_by_unit(hcfg, "transcendence-mt5-c002", 18805)


def test_helper_runner_tanpa_shell_dan_path_absolut():
    src = Path(H.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src and "os.system" not in src
    assert "os.popen" not in src
    with pytest.raises(H.HelperError):
        H.Runner().run(["systemctl", "status"])


@pytest.mark.skipif(os.geteuid() == 0, reason="cek non-root")
def test_helper_main_menolak_non_root(capsys):
    assert H.main(["status", "--code", "C001", "--port", "18812"]) \
        == H.EXIT_FAIL
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ------------------------------------------------- klien sudo di API
class _Cfg:
    rpyc_port_base = 18812
    max_customers = 4
    provisioner_helper = "/usr/local/sbin/transcendence-mt5-provision"
    provisioner_sudo = "/usr/bin/sudo"
    provisioner_timeout_seconds = 5


def test_klien_sudo_argv_terbatas_tanpa_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    hp = P.HelperProvisioner(_Cfg())
    hp.install(P.TerminalSpec("C007", 18813, PID_A))
    assert seen["argv"] == ["/usr/bin/sudo", "-n",
                            "/usr/local/sbin/transcendence-mt5-provision",
                            "install", "--code", "C007", "--port", "18813",
                            "--provision-id", PID_A, "--rpc-mode", "classic"]
    assert seen["kw"]["shell"] is False
    assert set(seen["kw"]["env"]) == {"PATH"}


def test_klien_sudo_validasi_sebelum_memanggil(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: called.append(a))
    hp = P.HelperProvisioner(_Cfg())
    for spec in (P.TerminalSpec("C007; id", 18813, PID_A),
                 P.TerminalSpec("C007", 9999, PID_A),
                 P.TerminalSpec("C007", 18813, "bukan-hex")):
        with pytest.raises(P.ProvisionError):
            hp.install(spec)
    assert called == []


def test_klien_sudo_exit_3_jadi_ownership_error(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(
                            argv, 3, '{"ok": false, "error": "x"}', ""))
    with pytest.raises(P.OwnershipError):
        P.HelperProvisioner(_Cfg()).remove(P.TerminalSpec("C007", 18813,
                                                          PID_A))


def test_klien_sudo_rpc_mode_tervalidasi(monkeypatch):
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(P.ProvisionError):
        P.HelperProvisioner(_Cfg()).install(
            P.TerminalSpec("C007", 18813, PID_A, rpc_mode="eval"))
    assert called == []


def test_helper_rpc_mode_ro_butuh_izin_konfigurasi(hcfg):
    with pytest.raises(H.HelperError):
        H.op_install(hcfg, "C001", 18812, PID_A, FakeRunner(), chown=False,
                     rpc_mode="ro")                # ro_service=False
    with pytest.raises(H.HelperError) as ei:
        H.dispatch(hcfg, ["install", "--code", "C001", "--port", "18812",
                          "--provision-id", PID_A, "--rpc-mode", "x"],
                   runner=FakeRunner())
    assert ei.value.code == H.EXIT_ARGS


def test_helper_user_per_customer_diturunkan_dari_kode(hcfg):
    import dataclasses
    c = dataclasses.replace(hcfg, service_user_mode="per_customer")
    assert H.service_user_for(c, "C007") == "mt5-c007"
    assert H.service_user_for(hcfg, "C007") == "transcendence"
    with pytest.raises(H.HelperError):
        H.validate(dataclasses.replace(hcfg, service_user_mode="root"),
                   "status", "C001", 18812, None)
