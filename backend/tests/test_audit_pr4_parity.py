"""Audit paritas PR #4 -> PR #5 (41 baris PR #4 yang tidak verbatim di
PR #5). Tiap test mengunci perilaku PR #4 yang WAJIB tetap ada (atau
lebih kuat) di implementasi PR #5.

R-A  unbind: port/slot tidak boleh dibebaskan selama terminal lama
     mungkin masih hidup (PR #4: baris terminal dipertahankan).
R-B  bind: bentrok UNIQUE tetap tercatat di log operasional (tanpa
     secret).
R-C  bind: keberhasilan tetap tercatat sebagai audit 'bind' + flag
     'reactivate' — juga bila hasil job tidak pernah di-poll klien.
R-E  bind: sync awal tetap dimulai setelah bind sukses — juga bila
     klien tidak mengambil hasil job.
"""
import logging
import os
import time

from provisioner import mt5_provision_helper as H
from tests.conftest import register_and_login
from tests.test_provision_helper import FakeRunner, hcfg  # noqa: F401
from tests.test_remediation_pr5 import _app, _cfg, _n

PID = "c" * 32


# ------------------------------------------------------------------ R-A
class _StillActiveRunner(FakeRunner):
    """systemctl disable --now 'berhasil' tetapi unit MASIH aktif."""

    def run(self, argv, check=True):
        self.calls.append(list(argv))
        if "is-active" in argv:
            return 0                     # masih aktif
        return 0


def test_ra_helper_remove_menolak_bila_unit_masih_aktif(hcfg):  # noqa: F811
    H.op_install(hcfg, "C001", 18812, PID, FakeRunner(), chown=False)
    try:
        H.op_remove(hcfg, "C001", PID, _StillActiveRunner())
        removed = True
    except H.HelperError as e:
        removed = False
        assert e.code == H.EXIT_FAIL
    assert removed is False, "resource dihapus padahal terminal masih hidup"
    assert (hcfg.base_dir / "C001" / ".provision_id").exists()
    assert (hcfg.unit_dir / "transcendence-mt5-c001.service").exists()


def test_ra_unbind_remove_gagal_slot_dan_port_tetap_ditahan():
    cfg = _cfg(max_customers=2)
    app = _app(cfg)
    c = app.test_client()
    hA = register_and_login(c, "ra@t.id")
    r = c.post("/api/v1/accounts/bind", headers=hA,
               json={"login": 81000001, "server": "Exness-MT5Real",
                     "password": "good"})
    assert r.status_code == 201, r.get_json()
    import db as dbmod
    conn = dbmod.get_db(cfg)
    port_a = int(conn.execute("SELECT rpyc_port FROM mt5_terminals"
                              ).fetchone()["rpyc_port"])
    app.extensions["mt5_provisioner"].fail = {"remove": True}
    r = c.post("/api/v1/accounts/unbind", headers=hA)
    assert r.get_json()["terminal_released"] is False
    app.extensions["mt5_provisioner"].fail = {}
    row = conn.execute("SELECT rpyc_port, lifecycle FROM mt5_terminals"
                       ).fetchone()
    assert row["lifecycle"] == "failed" and int(row["rpyc_port"]) == port_a
    hB = register_and_login(c, "rb@t.id")
    r = c.post("/api/v1/accounts/bind", headers=hB,
               json={"login": 81000002, "server": "Exness-MT5Real",
                     "password": "good"})
    assert r.status_code == 201, r.get_json()
    ports = sorted(int(x["rpyc_port"]) for x in conn.execute(
        "SELECT rpyc_port FROM mt5_terminals"))
    assert len(ports) == 2 and ports.count(port_a) == 1


# ------------------------------------------------------------------ R-B
def test_rb_bentrok_unique_bind_tercatat_di_log(monkeypatch, caplog):
    cfg = _cfg()
    app = _app(cfg)
    c = app.test_client()
    h1 = register_and_login(c, "u1@t.id")
    assert c.post("/api/v1/accounts/bind", headers=h1,
                  json={"login": 82000001, "server": "Exness-MT5Real",
                        "password": "good"}).status_code == 201
    h2 = register_and_login(c, "u2@t.id")
    import api.routes as R
    monkeypatch.setattr(R, "_find_account", lambda *a, **k: None)
    with caplog.at_level(logging.WARNING, logger="api.routes"):
        r = c.post("/api/v1/accounts/bind", headers=h2,
                   json={"login": 82000001, "server": "Exness-MT5Real",
                         "password": "RAHASIA-BENTROK"})
    assert r.status_code == 409
    msgs = [m.getMessage() for m in caplog.records
            if "bentrok" in m.getMessage()]
    assert msgs, "bentrok UNIQUE bind tidak tercatat di log"
    assert all("RAHASIA-BENTROK" not in m for m in msgs)


# ------------------------------------------------------- R-C / R-E
def _bind_async(cfg, app, email, login):
    c = app.test_client()
    h = register_and_login(c, email)
    r = c.post("/api/v1/accounts/bind", headers=h,
               json={"login": login, "server": "Exness-MT5Real",
                     "password": "good"})
    return c, h, r


def _wait_job_final(cfg):
    end = time.time() + 10
    while time.time() < end:
        if _n(cfg, "SELECT COUNT(*) AS n FROM mt5_connect_jobs WHERE"
                   " state IN ('connected','failed')") == 1:
            return
        time.sleep(0.05)
    raise AssertionError("job tidak selesai")


def test_rc_audit_bind_sukses_tanpa_polling():
    cfg = _cfg(mt5_connect_sync_wait_seconds=0.0)
    app = _app(cfg)
    app.extensions["mt5_provisioner"].delays = {"start": 0.3}
    _, _, r = _bind_async(cfg, app, "rc@t.id", 83000001)
    assert r.status_code == 202
    _wait_job_final(cfg)
    time.sleep(0.2)
    import db as dbmod
    import json
    rows = [dict(x) for x in dbmod.get_db(cfg).execute(
        "SELECT action, detail FROM audit_logs WHERE action='bind'")]
    assert rows, "bind sukses (job async) tidak tercatat sebagai audit 'bind'"
    d = json.loads(rows[-1]["detail"])
    assert d.get("reactivate") is False and d.get("live_verified") is True


def test_re_sync_awal_dimulai_tanpa_polling(monkeypatch):
    cfg = _cfg(mt5_connect_sync_wait_seconds=0.0)
    app = _app(cfg)
    app.extensions["auto_initial_sync"] = True
    started = []
    monkeypatch.setattr(app.extensions["mt5_sync"], "sync_account",
                        lambda acc, mode="incremental", **k:
                        started.append((acc, mode)))
    app.extensions["mt5_provisioner"].delays = {"start": 0.3}
    _, _, r = _bind_async(cfg, app, "re@t.id", 84000001)
    assert r.status_code == 202
    _wait_job_final(cfg)
    end = time.time() + 3
    while not started and time.time() < end:
        time.sleep(0.05)
    assert started and started[0][1] == "initial", started
    assert os.environ.get("PYTEST_CURRENT_TEST")
