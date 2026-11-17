"""MT5 RPC Hardening — kelompok 1: klien provisioning sisi API
(services/provisioner.py). Tanpa sudo/helper nyata: _spawn diganti."""
import json
from types import SimpleNamespace

import pytest

from security import rpc_policy as P
from services import provisioner as PV

PA, PB = "a" * 32, "b" * 32
MID = "c" * 32
SECRET = "0123456789abcdef" * 4


def cfg(tmp_path=None, **kw):
    d = dict(provisioner_helper="/usr/local/libexec/helper",
             provisioner_sudo="/usr/bin/sudo",
             provisioner_timeout_seconds=5.0, rpyc_port_base=18812,
             max_customers=4, mt5_rpc_secret=SECRET,
             term_base_dir=str(tmp_path) if tmp_path else "/nonexistent")
    d.update(kw)
    return SimpleNamespace(**d)


class Spy(PV.HelperProvisioner):
    def __init__(self, c, results):
        super().__init__(c)
        self.argvs = []
        self.results = list(results)

    def _spawn(self, argv, timeout):
        self.argvs.append(list(argv))
        rc, payload = self.results.pop(0)
        return rc, json.dumps(payload)


def spec(**kw):
    d = dict(code="C001", port=18812, provision_id=PA)
    d.update(kw)
    return PV.TerminalSpec(**d)


# -------------------------------------------------------- validate_spec
@pytest.mark.parametrize("kw", [
    {"migration_id": MID},                     # tanpa fence
    {"fence": 1},                              # tanpa migration_id
    {"migration_id": "X" * 32, "fence": 1},
    {"migration_id": MID, "fence": 0},
    {"migration_id": MID, "fence": True},
    {"expect_mode": "RO"},
])
def test_validate_spec_hardening_tidak_sah(kw):
    with pytest.raises(PV.ProvisionError):
        PV.validate_spec(cfg(), spec(**kw), need_pid=False)


def test_default_rpc_mode_tetap_classic_di_kelompok1():
    """Didokumentasikan: peralihan default ke 'ro' di kelompok 2."""
    assert spec().rpc_mode == "classic"


# --------------------------------------------------------------- argv
def test_argv_start_stop_status_membawa_expect_pid_dan_fence():
    s = Spy(cfg(), [(0, {"ok": True})] * 4)
    sp = spec(migration_id=MID, fence=3, expect_mode="ro")
    s.start(sp)
    s.stop(sp)
    s.status(spec())
    s.install(sp)
    start, stop_, status, install = s.argvs
    assert start[3] == "start" and ["--expect-pid", PA] == \
        start[start.index("--expect-pid"):start.index("--expect-pid") + 2]
    assert "--expect-mode" in start and start[start.index(
        "--expect-mode") + 1] == "ro"
    for a in (start, stop_, install):
        assert a[a.index("--migration-id") + 1] == MID
        assert a[a.index("--fence") + 1] == "3"
    assert "--expect-pid" in stop_ and "--expect-pid" in status
    assert "--migration-id" not in status
    assert "--rpc-mode" in install and "--expect-pid" not in install


def test_argv_tanpa_pid_sah_tidak_mengirim_expect_pid():
    s = Spy(cfg(), [(0, {"ok": True})])
    s.stop(spec(provision_id=None))
    assert "--expect-pid" not in s.argvs[0]
    assert "--migration-id" not in s.argvs[0]


# ---------------------------------------------------- exit 8/9 mapping
@pytest.mark.parametrize("rc,exc", [(8, PV.FenceError),
                                    (9, PV.PolicyError)])
def test_exit_fence_dan_policy_dipetakan_pasti(rc, exc):
    for op in ("install", "start", "stop", "remove"):
        s = Spy(cfg(), [(rc, {"ok": False, "error": "x", "code": rc})])
        with pytest.raises(exc) as e:
            getattr(s, op)(spec())
        assert not isinstance(e.value, PV.ProvisionTimeout)  # bukan tak pasti
        assert len(s.argvs) == 1                  # tidak memanggil inspect


def test_exit_tak_dikenal_tetap_abnormal():
    s = Spy(cfg(), [(42, {"ok": False}), (0, {"ok": True, "busy": False,
                                             "phase": "complete"})])
    with pytest.raises(PV.ProvisionAbnormalExit):
        s.start(spec())


# ----------------------------------------------- fence/selfcheck/audit
def test_fence_advance_release_argv_dan_hasil():
    s = Spy(cfg(), [(0, {"ok": True, "fence": {"migration_id": MID,
                                               "gen": 2, "released": False},
                         "marker": PA}),
                    (0, {"ok": True}), (8, {"ok": False, "error": "basi"})])
    out = s.fence_advance(spec(migration_id=MID, fence=2))
    assert out["fence"]["gen"] == 2
    assert s.argvs[0][3:] == ["fence-advance", "--code", "C001",
                              "--migration-id", MID, "--gen", "2"]
    s.fence_release(spec(migration_id=MID, fence=2))
    assert s.argvs[1][3] == "fence-release"
    with pytest.raises(PV.FenceError):
        s.fence_advance(spec(migration_id=MID, fence=1))
    with pytest.raises(PV.ProvisionError):
        s.fence_advance(spec())                     # tanpa migration_id


def test_selfcheck_nonce_dan_bukti():
    nonce = P.new_nonce()
    proof = P.selfcheck_proof(SECRET, nonce)
    s = Spy(cfg(), [(0, {"ok": True, "secret_proof": proof})])
    out = s.selfcheck(nonce)
    assert s.argvs[0][3:] == ["selfcheck", "--nonce", nonce]
    assert P.verify_selfcheck_proof(SECRET, nonce, out["secret_proof"])
    with pytest.raises(PV.ProvisionError):
        s.selfcheck("A" * 64)


def test_audit_all_dan_timeout():
    s = Spy(cfg(), [(0, {"ok": True, "terminals": []})])
    assert s.audit_all()["terminals"] == []
    assert s.argvs[0][3:] == ["audit-all"]
    s2 = Spy(cfg(), [("timeout", True)])
    with pytest.raises(PV.ProvisionTimeout) as e:
        s2.audit_all()
    assert e.value.uncertain


# --------------------------------------------- MockProvisioner paritas
def test_mock_fence_paritas_aturan_helper(tmp_path):
    m = PV.MockProvisioner(cfg(tmp_path))
    m.install(spec())
    out = m.fence_advance(spec(migration_id=MID, fence=1))
    assert out["fence"]["gen"] == 1 and out["marker"] == PA
    with pytest.raises(PV.FenceError):
        m.stop(spec())                               # tanpa fence
    with pytest.raises(PV.FenceError):
        m.stop(spec(migration_id=MID, fence=2))      # belum di-advance
    m.stop(spec(migration_id=MID, fence=1))
    m.fence_advance(spec(migration_id=MID, fence=2))
    with pytest.raises(PV.FenceError):
        m.start(spec(migration_id=MID, fence=1))     # gen basi
    with pytest.raises(PV.FenceError):
        m.fence_advance(spec(migration_id="d" * 32, fence=9))
    m.fence_release(spec(migration_id=MID, fence=2))
    m.stop(spec())                                   # bebas setelah release


def test_mock_expect_pid(tmp_path):
    m = PV.MockProvisioner(cfg(tmp_path))
    m.install(spec())
    with pytest.raises(PV.OwnershipError):
        m.start(spec(provision_id=PB))
    m.start(spec())
    m.stop(spec(code="C002", provision_id=PB))      # tidak ada: idempoten


def test_mock_selfcheck_tidak_pernah_produksi(tmp_path):
    m = PV.MockProvisioner(cfg(tmp_path))
    nonce = P.new_nonce()
    out = m.selfcheck(nonce)
    assert out["production"] is False and out["production_problems"]
    assert P.verify_selfcheck_proof(SECRET, nonce, out["secret_proof"])
    assert SECRET not in json.dumps(out)
