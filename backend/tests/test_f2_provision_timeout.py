"""Remediasi F2 — timeout helper provisioning, lock per customer, penanda
vs bukti selesai, idempotensi, dan pemulihan fail-closed.

Tingkat bukti:
- helper: direktori sementara + runner palsu (tanpa sudo/systemd nyata);
  uji batas waktu/sinyal `main()` butuh root (dilewati bila bukan root).
- klien API: subprocess.Popen palsu (urutan SIGTERM, grace, inspect).
- coordinator: MockProvisioner dengan kegagalan disuntik.
Bukti sudo/root NYATA ada di skrip integrasi terpisah (laporan Tahap B).
"""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import db as dbmod
from provisioner import mt5_provision_helper as H
from services import provisioner as P
from tests.test_provision_helper import FakeRunner
from tests.test_remediation_pr5 import _app, _cfg, _n, _post

try:   # fixture PostgreSQL (pg, env) dipakai ulang bila pgserver tersedia
    from tests.test_remediation_pr5_pg import env, pg  # noqa: F401
except Exception:  # noqa: BLE001
    pass

PID_A, PID_B = "a" * 32, "b" * 32
INSTALL = ["install", "--code", "C001", "--port", "18812",
           "--provision-id", PID_A]


@pytest.fixture(autouse=True)
def _tanpa_chown(monkeypatch):
    """chown ke user layanan butuh user sistem nyata; di uji ini dimatikan
    (cakupan chown ada di test_provision_helper.py)."""
    orig = H.op_install
    monkeypatch.setattr(H, "op_install",
                        lambda *a, **k: orig(*a, **dict(k, chown=False)))


@pytest.fixture()
def hcfg(tmp_path):
    tpl = tmp_path / "template"
    (tpl / "sub").mkdir(parents=True)
    for i in range(5):
        (tpl / f"f{i}.bin").write_text("x" * (i + 1))
    (tpl / "sub" / "d.dat").write_text("data")
    (tmp_path / "terminals").mkdir()
    (tmp_path / "units").mkdir()
    return H.HelperConfig(base_dir=tmp_path / "terminals", template_dir=tpl,
                          unit_dir=tmp_path / "units",
                          service_user="transcendence", port_base=18812,
                          max_slots=4, proc_root=tmp_path / "proc",
                          lock_dir=tmp_path / "locks", lock_wait_seconds=0.2)


def _install(hcfg, argv=INSTALL):
    return H.dispatch(hcfg, argv, runner=FakeRunner())


def _p(hcfg, code="C001"):
    return H.paths(hcfg, code)


# ------------------------------------------------------- helper: selesai
def test_penanda_saja_bukan_bukti_selesai(hcfg):
    p = _p(hcfg)
    p["base"].mkdir()
    p["marker"].write_text(PID_A)                 # klaim tanpa salinan/unit
    (p["base"] / "terminal").mkdir()
    (p["base"] / "terminal" / "f0.bin").write_text("x")   # salinan parsial
    p["unit_file"].write_text("sisa")             # unit sisa tanpa .complete
    with pytest.raises(H.HelperError) as ei:
        H.op_start(hcfg, "C001", 18812, FakeRunner())
    assert ei.value.code == H.EXIT_OWNERSHIP
    st = H.op_status(hcfg, "C001", 18812, FakeRunner())
    assert st["installed"] is False and st["running"] is False
    assert H.op_inspect(hcfg, "C001", PID_A)["phase"] == "incomplete"


def test_install_ulang_atas_parsial_menyalin_ulang_lengkap(hcfg):
    p = _p(hcfg)
    p["base"].mkdir()
    p["marker"].write_text(PID_A)
    (p["base"] / "terminal").mkdir()
    (p["base"] / "terminal" / "f0.bin").write_text("rusak-ukuran-beda")
    (p["base"] / H.PARTIAL).mkdir()
    out = _install(hcfg)
    assert out == {"ok": True, "idempotent": False}
    assert H.verify_copy(hcfg.template_dir, p["terminal"]) == 6
    assert (p["terminal"] / "f0.bin").read_text() == "x"
    assert not (p["base"] / H.PARTIAL).exists()
    assert H.read_complete(p, PID_A)["provision_id"] == PID_A
    assert sorted(os.listdir(hcfg.unit_dir)) == ["transcendence-mt5-c001.service"]
    # idempoten: tanpa salin ulang, tanpa unit ganda
    assert _install(hcfg) == {"ok": True, "idempotent": True}
    assert sorted(os.listdir(hcfg.unit_dir)) == ["transcendence-mt5-c001.service"]
    assert sorted(os.listdir(hcfg.base_dir)) == ["C001"]


def test_verify_copy_menolak_salinan_tidak_lengkap(hcfg, tmp_path):
    dst = tmp_path / "dst"
    import shutil
    shutil.copytree(hcfg.template_dir, dst)
    assert H.verify_copy(hcfg.template_dir, dst) == 6
    (dst / "sub" / "d.dat").unlink()
    with pytest.raises(H.HelperError):
        H.verify_copy(hcfg.template_dir, dst)
    (dst / "sub" / "d.dat").write_text("dat")       # ukuran beda
    with pytest.raises(H.HelperError):
        H.verify_copy(hcfg.template_dir, dst)


def test_galat_salin_tidak_menulis_unit_maupun_complete(hcfg, monkeypatch):
    def boom(*a, **k):
        raise H.HelperError(H.EXIT_INTERRUPTED, "dihentikan")
    monkeypatch.setattr(H, "verify_copy", boom)
    with pytest.raises(H.HelperError):
        _install(hcfg)
    p = _p(hcfg)
    assert H.read_marker(p) == PID_A                # klaim -> bisa dibersihkan
    assert not p["complete"].exists() and not p["unit_file"].exists()
    assert H.op_inspect(hcfg, "C001", PID_A)["phase"] == "incomplete"
    monkeypatch.undo()
    assert H.op_remove(hcfg, "C001", PID_A, FakeRunner())["ok"] is True
    assert not p["base"].exists()


def test_galat_setelah_salin_sebelum_unit(hcfg, monkeypatch):
    monkeypatch.setattr(H, "_write_unit", lambda *a: (_ for _ in ()).throw(
        H.HelperError(H.EXIT_INTERRUPTED, "dihentikan")))
    with pytest.raises(H.HelperError):
        _install(hcfg)
    p = _p(hcfg)
    assert H.verify_copy(hcfg.template_dir, p["terminal"]) == 6
    assert not p["complete"].exists() and not p["unit_file"].exists()
    with pytest.raises(H.HelperError):
        H.op_start(hcfg, "C001", 18812, FakeRunner())


def test_remove_mencabut_bukti_selesai_lebih_dulu(hcfg, monkeypatch):
    _install(hcfg)
    p = _p(hcfg)
    import shutil
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: (_ for _ in ())
                        .throw(OSError("disk")))
    with pytest.raises(OSError):
        H.op_remove(hcfg, "C001", PID_A, FakeRunner())
    # T2: direktori sudah dipindah ATOMIK ke tombstone berpenanda; bukti
    # kepemilikan (manifest) tetap ada walau rmtree gagal
    tomb, manifest = H.tomb_paths(hcfg, "C001", PID_A)
    assert not p["base"].exists() and tomb.is_dir()
    assert H.read_manifest(manifest, "C001", PID_A)
    assert not (tomb / H.COMPLETE).exists()         # tidak dapat di-start lagi
    with pytest.raises(H.HelperError):
        H.op_start(hcfg, "C001", 18812, FakeRunner())
    monkeypatch.undo()
    out = H.op_remove(hcfg, "C001", PID_A, FakeRunner())
    assert out["ok"] is True and out["resumed"] is True
    assert not tomb.exists() and not manifest.exists()


def test_penanda_rusak_tidak_dianggap_milik_siapa_pun(hcfg):
    p = _p(hcfg)
    p["base"].mkdir()
    p["marker"].write_text("aaaa")                  # tertulis setengah
    assert H.read_marker(p) is None
    with pytest.raises(H.HelperError) as ei:
        H.op_remove(hcfg, "C001", PID_A, FakeRunner())
    assert ei.value.code == H.EXIT_OWNERSHIP and p["base"].exists()


# ------------------------------------------------------- helper: lock
def test_lock_install_dan_remove_bersamaan_sibuk_tanpa_perubahan(hcfg):
    _install(hcfg)
    p = _p(hcfg)
    before = sorted(str(x) for x in p["base"].rglob("*"))
    fd = H.acquire_lock(hcfg, "C001")               # operasi lain berjalan
    try:
        for argv in (INSTALL, ["remove", "--code", "C001",
                               "--provision-id", PID_A],
                     ["start", "--code", "C001", "--port", "18812"]):
            with pytest.raises(H.HelperError) as ei:
                H.dispatch(hcfg, argv, runner=FakeRunner())
            assert ei.value.code == H.EXIT_BUSY
        assert H.op_inspect(hcfg, "C001", PID_A)["busy"] is True
        st = H.op_status(hcfg, "C001", 18812, FakeRunner())
        assert st["busy"] is True and st["running"] is False
        # customer LAIN tidak terkunci
        out = H.dispatch(hcfg, ["install", "--code", "C002", "--port",
                                "18813", "--provision-id", PID_B],
                         runner=FakeRunner())
        assert out["ok"] is True
    finally:
        os.close(fd)
    assert sorted(str(x) for x in p["base"].rglob("*")) == before
    assert H.op_inspect(hcfg, "C001", PID_A)["busy"] is False


def test_lock_dilepas_saat_pemegang_mati(hcfg):
    code = textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, {str(Path(H.__file__).parent.parent)!r})
        from provisioner import mt5_provision_helper as H
        from pathlib import Path
        cfg = H.HelperConfig(base_dir=Path({str(hcfg.base_dir)!r}),
            template_dir=Path({str(hcfg.template_dir)!r}),
            unit_dir=Path({str(hcfg.unit_dir)!r}), service_user="x",
            port_base=18812, max_slots=4, lock_dir=Path({str(hcfg.lock_dir)!r}))
        H.acquire_lock(cfg, "C001"); print("held", flush=True); time.sleep(60)
    """)
    pr = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                          text=True)
    assert pr.stdout.readline().strip() == "held"
    assert H.lock_busy(hcfg, "C001") is True
    pr.kill()
    pr.wait()
    assert H.lock_busy(hcfg, "C001") is False       # kernel melepas flock


def test_lock_dir_tidak_aman_ditolak(hcfg):
    hcfg.lock_dir.mkdir(mode=0o777)
    os.chmod(hcfg.lock_dir, 0o777)
    with pytest.raises(H.HelperError) as ei:
        H.dispatch(hcfg, INSTALL, runner=FakeRunner())
    assert ei.value.code == H.EXIT_FAIL
    assert not _p(hcfg)["base"].exists()


# ------------------------------------------------------- helper: main()
needs_root = pytest.mark.skipif(os.geteuid() != 0,
                                reason="main() mewajibkan euid 0")


def _main_proc(hcfg, tmp_path, op_timeout, delay, ignore_term=False):
    cfgf = tmp_path / "prov.json"
    cfgf.write_text(json.dumps({
        "base_dir": str(hcfg.base_dir), "template_dir": str(hcfg.template_dir),
        "unit_dir": str(hcfg.unit_dir), "service_user": "root", "allow_shared_service_user": True,
        "port_base": 18812, "max_slots": 4, "systemctl": "/bin/true",
        "lock_dir": str(hcfg.lock_dir), "op_timeout_seconds": op_timeout}))
    os.chmod(cfgf, 0o644)
    code = textwrap.dedent(f"""
        import shutil, signal, sys, time
        sys.path.insert(0, {str(Path(H.__file__).parent.parent)!r})
        from provisioner import mt5_provision_helper as H
        _o = H._copy_template
        def slow(cfg, p):
            time.sleep({delay}); return _o(cfg, p)
        H._copy_template = slow
        if {ignore_term!r}:
            _h = H._install_signal_handlers
            def h(cfg):
                _h(cfg); signal.signal(signal.SIGTERM, signal.SIG_IGN)
            H._install_signal_handlers = h
        sys.exit(H.main({INSTALL!r}, config_path={str(cfgf)!r}))
    """)
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)


@needs_root
def test_main_batas_waktu_helper_sendiri_exit_5(hcfg, tmp_path):
    t0 = time.monotonic()
    pr = _main_proc(hcfg, tmp_path, op_timeout=1, delay=30)
    out, _ = pr.communicate(timeout=20)
    assert pr.returncode == H.EXIT_INTERRUPTED, out
    assert time.monotonic() - t0 < 10
    assert H.op_inspect(hcfg, "C001", PID_A)["phase"] == "incomplete"
    assert not _p(hcfg)["unit_file"].exists()


@needs_root
def test_main_sigterm_exit_5_dan_lock_lepas(hcfg, tmp_path):
    pr = _main_proc(hcfg, tmp_path, op_timeout=60, delay=30)
    end = time.monotonic() + 10
    while not H.lock_busy(hcfg, "C001") and time.monotonic() < end:
        time.sleep(0.05)
    assert H.lock_busy(hcfg, "C001") is True
    pr.send_signal(signal.SIGTERM)
    out, _ = pr.communicate(timeout=10)
    assert pr.returncode == H.EXIT_INTERRUPTED
    assert json.loads(out.strip().splitlines()[-1])["code"] == 5
    assert H.lock_busy(hcfg, "C001") is False
    assert not _p(hcfg)["complete"].exists()


# ------------------------------------------------------- klien API (Popen palsu)
class _Cfg:
    rpyc_port_base = 18812
    max_customers = 4
    provisioner_helper = "/usr/local/sbin/transcendence-mt5-provision"
    provisioner_sudo = "/usr/bin/sudo"
    provisioner_timeout_seconds = 0.2
    provisioner_term_grace_seconds = 0.2
    provisioner_inspect_timeout_seconds = 0.2


class _Proc:
    """Popen palsu; perilaku per argv[3] (op)."""
    plan = {}
    log = []

    def __init__(self, argv, **kw):
        self.op = argv[3]
        self.pid = 4242
        self.returncode = None
        self.calls = 0
        _Proc.log.append(("spawn", self.op, kw.get("start_new_session")))

    def communicate(self, timeout=None):
        self.calls += 1
        beh = _Proc.plan[self.op]
        if beh[0] == "hang" or (beh[0] == "hang_then_exit" and self.calls == 1):
            raise subprocess.TimeoutExpired("sudo", timeout)
        rc, out = beh[1], beh[2]
        self.returncode = rc
        return out, ""

    def wait(self, timeout=None):
        return 0


@pytest.fixture()
def popen(monkeypatch):
    _Proc.plan, _Proc.log = {}, []
    kills = []
    monkeypatch.setattr(subprocess, "Popen", _Proc)
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    return kills


SPEC = P.TerminalSpec("C001", 18812, PID_A)


def _insp(**kw):
    d = {"ok": True, "busy": False, "phase": "incomplete",
         "owner_match": True, "unit_file": False, "partial": True}
    d.update(kw)
    return json.dumps(d)


@pytest.mark.parametrize("insp,state,uncertain", [
    (_insp(busy=True), "helper_running", True),
    (_insp(), "stopped_incomplete", False),
    (_insp(phase="complete", unit_file=True), "stopped_complete", False),
    (_insp(phase="absent"), "stopped_absent", False),
    (_insp(phase="complete", owner_match=False), "unknown", True),
    ("bukan json", "unknown", True),
])
def test_klien_timeout_sigterm_lalu_inspect(popen, insp, state, uncertain):
    _Proc.plan = {"install": ("hang_then_exit", 5, '{"ok": false}'),
                  "inspect": ("exit", 0, insp)}
    with pytest.raises(P.ProvisionTimeout) as ei:
        P.HelperProvisioner(_Cfg()).install(SPEC)
    assert ei.value.state == state and ei.value.uncertain is uncertain
    assert popen == [(4242, signal.SIGTERM)]          # SIGTERM, BUKAN SIGKILL
    assert ("spawn", "install", True) in _Proc.log    # sesi baru
    assert ("spawn", "inspect", True) in _Proc.log


def test_klien_sudo_masih_hidup_setelah_grace_selalu_helper_running(popen):
    _Proc.plan = {"install": ("hang",),
                  "inspect": ("exit", 0, _insp(busy=False))}
    with pytest.raises(P.ProvisionTimeout) as ei:
        P.HelperProvisioner(_Cfg()).install(SPEC)
    assert ei.value.state == "helper_running" and ei.value.uncertain
    assert all(sig == signal.SIGTERM for _, sig in popen)
    assert ("spawn", "inspect", True) not in _Proc.log


def test_klien_inspect_juga_timeout_unknown(popen):
    _Proc.plan = {"remove": ("hang_then_exit", 5, ""),
                  "inspect": ("hang",)}
    with pytest.raises(P.ProvisionTimeout) as ei:
        P.HelperProvisioner(_Cfg()).remove(SPEC)
    assert ei.value.state == "unknown" and ei.value.uncertain


@pytest.mark.parametrize("rc,exc", [(6, P.ProvisionBusy),
                                    (5, P.ProvisionIncomplete),
                                    (3, P.OwnershipError),
                                    (4, P.ProvisionError)])
def test_klien_peta_exit_helper(popen, rc, exc):
    _Proc.plan = {"install": ("exit", rc, '{"ok": false, "error": "x"}')}
    with pytest.raises(exc):
        P.HelperProvisioner(_Cfg()).install(SPEC)
    assert popen == []


def test_klien_status_sibuk_berarti_tidak_running(popen):
    _Proc.plan = {"status": ("exit", 0, json.dumps(
        {"ok": True, "busy": True, "running": True, "installed": True}))}
    assert P.HelperProvisioner(_Cfg()).status(SPEC)["running"] is False


# ------------------------------------------------------- coordinator
def _terminal(cfg):
    conn = dbmod.get_db(cfg)
    conn.rollback()
    return conn.execute("SELECT id, lifecycle, last_error, provision_id"
                        " FROM mt5_terminals").fetchall()


def test_job_install_timeout_tidak_pasti_slot_ditahan_tanpa_token():
    cfg = _cfg()
    app = _app(cfg)
    prov = app.extensions["mt5_provisioner"]
    real_remove = prov.remove

    def install(spec):
        raise P.ProvisionTimeout("helper install timeout", "helper_running")

    def remove(spec):
        raise P.ProvisionBusy("sibuk")
    prov.install, prov.remove = install, remove
    r = _post(app, 10000011)
    assert r.status_code == 503, r.get_json()
    assert _n(cfg, "SELECT COUNT(*) AS n FROM auth_tokens") == 0
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_credentials") == 0
    rows = _terminal(cfg)
    assert len(rows) == 1 and rows[0]["lifecycle"] == "failed"
    assert rows[0]["last_error"].startswith("retry:1:ProvisionBusy")
    assert _n(cfg, "SELECT COUNT(*) AS n FROM mt5_connect_jobs WHERE"
                   " token_issued_at IS NOT NULL") == 0
    # pemulihan saat helper masih sibuk: tetap ditahan
    coord = app.extensions["mt5_coordinator"]
    assert coord.retry_uncertain_removals(dbmod.get_db(cfg)) == 0
    assert len(_terminal(cfg)) == 1
    # helper selesai -> pemulihan menghapus (kepemilikan terbukti)
    prov.remove = real_remove
    assert coord.retry_uncertain_removals(dbmod.get_db(cfg)) == 1
    assert _terminal(cfg) == []
    for t in ("customers", "mt5_accounts", "mt5_credentials", "auth_tokens"):
        assert _n(cfg, f"SELECT COUNT(*) AS n FROM {t}") == 0, t


def test_restart_api_memulihkan_terminal_tertahan():
    cfg = _cfg()
    app = _app(cfg)
    prov = app.extensions["mt5_provisioner"]
    prov.fail = {"start": True}

    def remove(spec):
        raise P.ProvisionTimeout("helper remove timeout", "unknown")
    prov.remove = remove
    assert _post(app, 10000012).status_code == 503
    assert _terminal(cfg)[0]["last_error"].startswith("retry:1:ProvisionTimeout")
    dbmod.close_db()
    _app(cfg)                       # restart: startup -> retry_uncertain_removals
    assert _terminal(cfg) == []


def test_kepemilikan_tidak_terbukti_tidak_diulang_otomatis():
    cfg = _cfg()
    app = _app(cfg)
    prov = app.extensions["mt5_provisioner"]
    prov.fail = {"start": True}
    calls = []

    def remove(spec):
        calls.append(spec.code)
        raise P.OwnershipError("kepemilikan tidak terbukti")
    prov.remove = remove
    assert _post(app, 10000013).status_code == 503
    assert _terminal(cfg)[0]["last_error"] == "remove-failed:OwnershipError"
    n = len(calls)
    assert app.extensions["mt5_coordinator"].retry_uncertain_removals(
        dbmod.get_db(cfg)) == 0
    assert len(calls) == n and len(_terminal(cfg)) == 1


def test_retry_tidak_menyentuh_terminal_milik_job_lain():
    cfg = _cfg()
    app = _app(cfg)
    prov = app.extensions["mt5_provisioner"]
    prov.fail = {"start": True}
    prov.remove = lambda spec: (_ for _ in ()).throw(P.ProvisionBusy("x"))
    assert _post(app, 10000014).status_code == 503
    conn = dbmod.get_db(cfg)
    # baris terminal kini DIPEGANG job aktif lain (bukan penghapusan)
    tid = _terminal(cfg)[0]["id"]
    conn.execute("INSERT INTO mt5_connect_jobs (id, secret_hash, kind, state,"
                 " customer_id, terminal_id, deadline_at, created_at,"
                 " updated_at) SELECT 'jobaktif', 'x', 'bind', 'verifying',"
                 " customer_id, id, ?, 'a', 'a' FROM mt5_terminals WHERE id=?",
                 (time.time() + 3600, tid))
    conn.execute("UPDATE mt5_terminals SET op_owner='job:jobaktif',"
                 " lifecycle='verifying' WHERE id=?", (tid,))
    conn.commit()
    removed = []
    prov.remove = lambda spec: removed.append(spec)
    assert app.extensions["mt5_coordinator"].retry_uncertain_removals(
        conn) == 0
    assert removed == [] and len(_terminal(cfg)) == 1


def test_mock_start_butuh_instalasi_lengkap():
    cfg = _cfg()
    mp = P.MockProvisioner(cfg)
    spec = P.TerminalSpec("C001", int(cfg.rpyc_port_base), PID_A)
    mp.fail = {"install": True}
    with pytest.raises(P.ProvisionError):
        mp.install(spec)
    with pytest.raises(P.OwnershipError):
        mp.start(spec)
    assert mp.inspect(spec)["phase"] == "incomplete"


# ------------------------------------------------------- PostgreSQL + RLS (non-owner)
def test_pg_rls_retry_terminal_tertahan_setelah_restart(request):
    """Jalur pemulihan F2 di PostgreSQL SUNGGUHAN dengan role non-owner
    (RLS aktif): baris ditahan saat helper sibuk, dihapus setelah restart."""
    pytest.importorskip("pgserver")
    pytest.importorskip("psycopg2")
    from tests import test_remediation_pr5_pg as PG
    env = request.getfixturevalue("env")
    owner_uri, app_uri = env
    cfg = PG._cfg(app_uri)
    app = PG._app(cfg)
    prov = app.extensions["mt5_provisioner"]
    prov.fail = {"start": True}
    prov.remove = lambda spec: (_ for _ in ()).throw(
        P.ProvisionTimeout("helper remove timeout", "helper_running"))
    r = PG._post(app, 33333333)
    assert r.status_code == 503, r.get_json()
    assert PG._own(owner_uri, "SELECT COUNT(*) FROM auth_tokens") == 0
    assert PG._own(owner_uri, "SELECT last_error FROM mt5_terminals") \
        .startswith("retry:1:ProvisionTimeout:helper_running")
    dbmod.close_db()
    PG._app(cfg)                    # restart -> retry_uncertain_removals
    for t in ("mt5_terminals", "customers", "mt5_accounts", "auth_tokens"):
        assert PG._own(owner_uri, f"SELECT COUNT(*) FROM {t}") == 0, t
