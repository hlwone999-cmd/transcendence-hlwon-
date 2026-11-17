"""F2 FINAL REVIEW — remove yang SUDAH berhasil tetapi respons helper hilang,
retry setelah resource tiada, konsistensi DB terminal/customer, dua worker
pemulihan bersamaan, retry tanpa akhir & terminal tertahan permanen.

Helper ASLI (mt5_provision_helper.dispatch) dijalankan in-process pada
direktori sementara dengan lock flock nyata; batas proses sudo diganti
HelperBridge._spawn (respons dapat "hilang" SETELAH operasi selesai).
Coordinator + DB SQLite (dan PostgreSQL non-owner bila tersedia).
"""
import io
import json
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import db as dbmod
from provisioner import mt5_provision_helper as H
from services import provisioner as P
from services import provisioning as PV
from tests.test_provision_helper import FakeRunner
from tests.test_remediation_pr5 import _app, _cfg, _n, _post

try:   # fixture PostgreSQL dipakai ulang bila pgserver tersedia
    from tests.test_remediation_pr5_pg import env, pg  # noqa: F401
except Exception:  # noqa: BLE001
    pass


@pytest.fixture(autouse=True)
def _tanpa_chown(monkeypatch):
    orig = H.op_install
    monkeypatch.setattr(H, "op_install",
                        lambda *a, **k: orig(*a, **dict(k, chown=False)))


class SlowRunner(FakeRunner):
    delay = 0.0

    def run(self, argv, check=True):
        if "disable" in argv and SlowRunner.delay:
            time.sleep(SlowRunner.delay)
        return super().run(argv, check)


class HelperBridge(P.HelperProvisioner):
    """HelperProvisioner dengan helper ASLI in-process. `lose` = set op yang
    responsnya HILANG (API timeout) SETELAH helper menyelesaikan operasinya;
    `fail_start` membuat job gagal sebelum connected."""

    def __init__(self, cfg, hcfg):
        super().__init__(cfg)
        self.hcfg = hcfg
        self.lose = {}            # op -> jumlah respons yang dihilangkan
        self.fail_start = True
        self.calls = []
        self._mu = threading.Lock()

    def _spawn(self, argv, timeout):
        args = list(argv[3:])
        op = args[0]
        with self._mu:
            self.calls.append(op)
        if op == "start" and self.fail_start:
            return 4, '{"ok": false, "error": "start gagal (uji)"}'
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                out = H.dispatch(self.hcfg, args, runner=SlowRunner())
            rc, text = 0, json.dumps(out)
        except H.HelperError as e:
            rc, text = e.code, json.dumps({"ok": False, "error": e.msg})
        with self._mu:
            if self.lose.get(op, 0) > 0:
                self.lose[op] -= 1
                return "timeout", False          # sudo sudah keluar; respons hilang
        return rc, text


def _setup(tmp_path, cfg=None):
    cfg = cfg or _cfg()
    tpl = tmp_path / "template"
    tpl.mkdir()
    (tpl / "terminal64.exe").write_text("bin")
    (tmp_path / "units").mkdir()
    Path(cfg.term_base_dir).mkdir(parents=True, exist_ok=True)
    hcfg = H.HelperConfig(base_dir=Path(cfg.term_base_dir), template_dir=tpl,
                          unit_dir=tmp_path / "units",
                          service_user="transcendence",
                          port_base=int(cfg.rpyc_port_base),
                          max_slots=int(cfg.max_customers),
                          proc_root=tmp_path / "proc",
                          lock_dir=tmp_path / "locks", lock_wait_seconds=0.3)
    app = _app(cfg)
    bridge = HelperBridge(cfg, hcfg)
    app.extensions["mt5_coordinator"].provisioner = bridge
    app.extensions["mt5_provisioner"] = bridge
    return cfg, app, bridge, hcfg


def _rows(cfg):
    conn = dbmod.get_db(cfg)
    conn.rollback()
    return [dict(r) for r in conn.execute(
        "SELECT id, lifecycle, last_error, provision_id FROM mt5_terminals")]


def _resources(hcfg, code="C001"):
    return {"dir": (hcfg.base_dir / code).exists(),
            "unit": (hcfg.unit_dir /
                     f"transcendence-mt5-{code.lower()}.service").exists()}


def _clean_db(cfg):
    for t in ("customers", "mt5_accounts", "mt5_credentials", "mt5_terminals",
              "auth_tokens"):
        assert _n(cfg, f"SELECT COUNT(*) AS n FROM {t}") == 0, t


# ------------------------------------------------------------- kasus inti
def test_remove_selesai_respons_hilang_diselesaikan_tanpa_restart(tmp_path):
    """API timeout SETELAH penghapusan fisik: inspect -> stopped_absent ->
    remove diulang SEKALI (idempoten, already_absent) -> baris & customer
    dibersihkan dalam job yang sama, tanpa menunggu restart."""
    cfg, app, bridge, hcfg = _setup(tmp_path)
    bridge.lose = {"remove": 1}
    r = _post(app, 10000021)
    assert r.status_code == 503, r.get_json()
    assert _resources(hcfg) == {"dir": False, "unit": False}
    assert bridge.calls.count("remove") == 2 and "inspect" in bridge.calls
    assert _rows(cfg) == []
    _clean_db(cfg)


def test_retry_setelah_resource_tiada_idempoten(tmp_path):
    """Respons hilang DUA kali (remove + remove ulang) -> baris ditahan;
    pemulihan berikutnya: helper already_absent -> baris & customer hilang,
    TIDAK ada resource lain yang disentuh."""
    cfg, app, bridge, hcfg = _setup(tmp_path)
    other = hcfg.base_dir / "C004"
    other.mkdir()
    (other / ".provision_id").write_text("f" * 32)
    bridge.lose = {"remove": 2}
    assert _post(app, 10000022).status_code == 503
    rows = _rows(cfg)
    assert len(rows) == 1 and rows[0]["lifecycle"] == "failed"
    assert rows[0]["last_error"].startswith("retry:")
    assert _resources(hcfg) == {"dir": False, "unit": False}
    coord = app.extensions["mt5_coordinator"]
    assert coord.retry_uncertain_removals(dbmod.get_db(cfg)) == 1
    assert _rows(cfg) == []
    _clean_db(cfg)
    assert (other / ".provision_id").read_text() == "f" * 32   # milik lain utuh


def test_stopped_absent_tidak_menghapus_resource_yang_muncul_milik_lain(
        tmp_path):
    """Setelah respons hilang, direktori customer muncul lagi dengan penanda
    LAIN (bukan milik job) -> retry: OwnershipError, TIDAK dihapus, baris
    ditahan untuk admin (tidak diulang tanpa akhir)."""
    cfg, app, bridge, hcfg = _setup(tmp_path)
    bridge.lose = {"remove": 2}
    assert _post(app, 10000023).status_code == 503
    foreign = hcfg.base_dir / "C001"
    foreign.mkdir()
    (foreign / ".provision_id").write_text("e" * 32)
    coord = app.extensions["mt5_coordinator"]
    assert coord.retry_uncertain_removals(dbmod.get_db(cfg)) == 0
    assert (foreign / ".provision_id").read_text() == "e" * 32
    rows = _rows(cfg)
    assert len(rows) == 1 and rows[0]["last_error"] == \
        "remove-failed:OwnershipError"
    n = bridge.calls.count("remove")
    assert coord.retry_uncertain_removals(dbmod.get_db(cfg)) == 0
    assert bridge.calls.count("remove") == n          # tidak diulang lagi


@pytest.mark.parametrize("state", ["helper_running", "unknown"])
def test_helper_running_dan_unknown_tetap_fail_closed(tmp_path, state):
    cfg, app, bridge, hcfg = _setup(tmp_path)
    real = bridge._after_timeout

    def forced(op, spec, sudo_alive):
        if op == "remove":
            raise P.ProvisionTimeout("helper remove timeout", state)
        return real(op, spec, sudo_alive)
    bridge._after_timeout = forced
    bridge.lose = {"remove": 99}
    assert _post(app, 10000024).status_code == 503
    rows = _rows(cfg)
    assert len(rows) == 1 and rows[0]["lifecycle"] == "failed"
    assert state in rows[0]["last_error"]
    # TIDAK ada remove ulang segera untuk keadaan tidak pasti
    assert bridge.calls.count("remove") == 1
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 1   # ditahan


def test_retry_dibatasi_lalu_ditandai_habis(tmp_path):
    """Helper terus 'unknown' -> setiap pemulihan menaikkan hitungan; setelah
    batas, ditandai 'retry-exhausted' dan TIDAK diulang lagi (eksplisit
    untuk admin, bukan diam-diam)."""
    cfg, app, bridge, hcfg = _setup(tmp_path)
    real = bridge._after_timeout
    bridge._after_timeout = lambda op, spec, a: (
        (_ for _ in ()).throw(P.ProvisionTimeout("x", "unknown"))
        if op == "remove" else real(op, spec, a))
    bridge.lose = {"remove": 10 ** 6}
    assert _post(app, 10000025).status_code == 503
    coord = app.extensions["mt5_coordinator"]
    for _ in range(PV.MAX_REMOVE_RETRIES + 3):
        coord.retry_uncertain_removals(dbmod.get_db(cfg))
    rows = _rows(cfg)
    assert rows[0]["last_error"].startswith("retry-exhausted:")
    assert bridge.calls.count("remove") == PV.MAX_REMOVE_RETRIES + 1
    assert _n(cfg, "SELECT COUNT(*) AS n FROM customers") == 1


def test_klaim_pemulihan_yang_terputus_diambil_ulang_setelah_lease(tmp_path):
    """Worker pemulihan mati setelah mengklaim (lifecycle 'stopping'):
    sebelum lease habis tidak diambil worker lain; setelah lease habis
    diambil ulang dan selesai (tidak tertahan permanen)."""
    cfg, app, bridge, hcfg = _setup(tmp_path)
    bridge.lose = {"remove": 2}
    assert _post(app, 10000026).status_code == 503
    conn = dbmod.get_db(cfg)
    tid = _rows(cfg)[0]["id"]
    conn.execute("UPDATE mt5_terminals SET lifecycle='stopping',"
                 " op_owner='remove:worker-mati', updated_at=? WHERE id=?",
                 (PV._now_iso(), tid))                    # klaim lalu "mati"
    conn.commit()
    coord = app.extensions["mt5_coordinator"]
    assert coord.retry_uncertain_removals(conn) == 0       # lease masih aktif
    assert len(_rows(cfg)) == 1
    conn.execute("UPDATE mt5_terminals SET updated_at='2000-01-01T00:00:00Z'"
                 " WHERE id=?", (tid,))
    conn.commit()
    assert coord.retry_uncertain_removals(conn) == 1
    assert _rows(cfg) == []
    _clean_db(cfg)


def test_dua_worker_pemulihan_bersamaan(tmp_path):
    """Dua worker (koneksi DB terpisah) memanggil pemulihan bersamaan:
    tepat SATU yang mengklaim & memanggil helper remove; hasil akhir
    konsisten (baris & customer hilang, tanpa galat)."""
    cfg, app, bridge, hcfg = _setup(tmp_path)
    bridge.lose = {"remove": 2}
    assert _post(app, 10000027).status_code == 503
    before = bridge.calls.count("remove")
    coord = app.extensions["mt5_coordinator"]
    SlowRunner.delay = 0.5
    res, errs = [], []
    barrier = threading.Barrier(2)

    def worker():
        try:
            with app.app_context():
                conn = dbmod.get_db(cfg)
                barrier.wait()
                res.append(coord.retry_uncertain_removals(conn))
                dbmod.close_db()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    try:
        th = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in th]
        [t.join(30) for t in th]
    finally:
        SlowRunner.delay = 0.0
    assert errs == [] and sorted(res) == [0, 1], (res, errs)
    assert bridge.calls.count("remove") - before == 1
    assert _rows(cfg) == []
    _clean_db(cfg)


def test_pg_rls_remove_respons_hilang_dan_retry(request, tmp_path,
                                               monkeypatch):
    """Jalur yang sama di PostgreSQL SUNGGUHAN, role non-owner (RLS aktif)."""
    pytest.importorskip("pgserver")
    pytest.importorskip("psycopg2")
    from tests import test_remediation_pr5_pg as PG
    owner_uri, app_uri = request.getfixturevalue("env")
    cfg = PG._cfg(app_uri)
    cfg, app, bridge, hcfg = _setup(tmp_path, cfg)
    bridge.lose = {"remove": 2}
    r = PG._post(app, 44444444)
    assert r.status_code == 503, r.get_json()
    assert PG._own(owner_uri, "SELECT last_error FROM mt5_terminals") \
        .startswith("retry:")
    assert PG._own(owner_uri, "SELECT COUNT(*) FROM mt5_credentials") == 0
    assert PG._own(owner_uri, "SELECT COUNT(*) FROM mt5_terminals") == 1
    dbmod.close_db()
    # restart API: startup memakai helper (bridge) -> retry_uncertain_removals
    before = bridge.calls.count("remove")
    monkeypatch.setattr(P, "build_provisioner", lambda c: bridge)
    PG._app(cfg)
    assert bridge.calls.count("remove") - before == 1
    for t in ("mt5_terminals", "customers", "mt5_accounts", "auth_tokens"):
        assert PG._own(owner_uri, f"SELECT COUNT(*) FROM {t}") == 0, t


def test_helper_already_absent_tidak_menghapus_apa_pun(tmp_path, monkeypatch):
    """remove kedua (respons pertama hilang): already_absent, tanpa userdel
    walau user per-customer tersisa (tanpa penanda = tanpa bukti)."""
    tpl = tmp_path / "t"
    tpl.mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "u").mkdir()
    hcfg = H.HelperConfig(base_dir=tmp_path / "b", template_dir=tpl,
                          unit_dir=tmp_path / "u", service_user="x",
                          port_base=18812, max_slots=4,
                          service_user_mode="per_customer",
                          lock_dir=tmp_path / "l")
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: object())
    r = FakeRunner()
    out = H.op_remove(hcfg, "C001", "a" * 32, r)
    assert out == {"ok": True, "already_absent": True,
                   "service_user_left": True}
    assert r.calls == []                     # tidak ada userdel/systemctl
