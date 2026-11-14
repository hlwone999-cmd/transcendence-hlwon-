"""MT5 RPC Hardening — kelompok 1: kebijakan tunggal security/rpc_policy.py.

Murni (tanpa DB/jaringan). ID tes merujuk spesifikasi revisi:
G2 (parsing mode ketat), CD3/CD4 + koreksi #1 (capability COMMITTED &
SYNCED), koreksi #5 (readiness FAILED_CREDENTIALS), FP1/FP4/FP5 (bukti
secret), TO1 (syarat lease)."""
import pytest

from security import rpc_policy as P

MID = "a" * 32
MID2 = "b" * 32
OLD, NEW = "1" * 32, "2" * 32


# ------------------------------------------------------------- G2: mode
@pytest.mark.parametrize("raw,want", [
    (None, "ro"), ("", "ro"), ("   ", "ro"), ("ro", "ro"),
    (" ro ", "ro"), ("classic", "classic"),
])
def test_g2_parse_rpc_mode_sah(raw, want):
    assert P.parse_rpc_mode(raw) == want


@pytest.mark.parametrize("raw", ["RO", "rO", "Ro ", "xyz", "classicc",
                                 "ro,classic", 1, b"ro"])
def test_g2_parse_rpc_mode_tidak_dikenal_ditolak_bukan_classic(raw):
    with pytest.raises(ValueError):
        P.parse_rpc_mode(raw)


def test_parse_enforcement():
    for v in P.ENFORCEMENTS:
        assert P.parse_enforcement(v) == v
    for v in ("", "STRICT", "prod", None):
        with pytest.raises(ValueError):
            P.parse_enforcement(v)


# ------------------------------------------------------------ owner tag
def test_owner_tag_roundtrip_dan_validasi():
    tag = P.make_owner_tag(MID, 3)
    assert tag == f"migrate:{MID}:3"
    assert P.parse_owner_tag(tag) == (MID, 3)
    for bad in (None, "job:x", f"migrate:{MID}:0", f"migrate:{MID}:-1",
                f"migrate:{MID[:-1]}:1", f"migrate:{MID}:1:x", "migrate:"):
        assert P.parse_owner_tag(bad) is None
    with pytest.raises(ValueError):
        P.make_owner_tag("X" * 32, 1)
    with pytest.raises(ValueError):
        P.make_owner_tag(MID, 0)
    with pytest.raises(ValueError):
        P.make_owner_tag(MID, True)


# ----------------------------------------------- check_terminal_usable
def term(**kw):
    t = {"id": 7, "rpc_mode": "ro", "op_owner": None, "provision_id": NEW,
         "lifecycle": "connected"}
    t.update(kw)
    return t


def mig(state="committed", gen=2, **kw):
    m = {"id": MID, "generation": gen, "terminal_id": 7, "account_id": 70,
         "state": state, "owner_tag": P.make_owner_tag(MID, gen),
         "new_provision_id": NEW, "old_provision_id": OLD}
    m.update(kw)
    return m


def cap(gen=2, **kw):
    d = {"migration_id": MID, "generation": gen, "terminal_id": 7,
         "account_id": 70}
    d.update(kw)
    return P.Capability(**d)


def reason(fn):
    with pytest.raises(P.PolicyViolation) as e:
        fn()
    return e.value.reason


def test_ro_tanpa_migrasi_boleh_di_semua_enforcement():
    for enf in P.ENFORCEMENTS:
        P.check_terminal_usable(term(), None, enforcement=enf)


def test_classic_diblokir_migration_dan_strict_diizinkan_off():
    t = term(rpc_mode="classic")
    for enf in ("migration", "strict"):
        assert reason(lambda enf=enf: P.check_terminal_usable(
            t, None, enforcement=enf)) == "classic_blocked"
    P.check_terminal_usable(t, None, enforcement="off")


@pytest.mark.parametrize("mode", [None, "", "RO", "x"])
def test_mode_tidak_sah_selalu_ditolak(mode):
    for enf in P.ENFORCEMENTS:
        assert reason(lambda enf=enf: P.check_terminal_usable(
            term(rpc_mode=mode), None, enforcement=enf)) == "rpc_mode_invalid"


def test_terminal_hilang_dan_enforcement_tidak_sah():
    assert reason(lambda: P.check_terminal_usable(
        None, None, enforcement="strict")) == "terminal_missing"
    assert reason(lambda: P.check_terminal_usable(
        term(), None, enforcement="x")) == "enforcement_invalid"


@pytest.mark.parametrize("state", [s for s in P.MIGRATION_STATES
                                   if s not in P.MIGRATION_FINAL_STATES])
def test_cd3_migrasi_non_final_memblokir_tanpa_capability(state):
    m = mig(state=state)
    t = term(op_owner=m["owner_tag"])
    for enf in P.ENFORCEMENTS:
        assert reason(lambda enf=enf: P.check_terminal_usable(
            t, m, enforcement=enf)) == "migration_in_progress"


def test_k4_owner_null_tetapi_migrasi_non_final_tetap_blokir():
    """K4: blokir berdasar baris migrasi, bukan hanya op_owner."""
    m = mig(state="committed")
    assert reason(lambda: P.check_terminal_usable(
        term(op_owner=None), m, enforcement="strict")) == \
        "migration_in_progress"


def test_owner_migrate_tanpa_baris_migrasi_diblokir():
    assert reason(lambda: P.check_terminal_usable(
        term(op_owner=P.make_owner_tag(MID, 1)), None,
        enforcement="off")) == "migration_owner_orphan"


def test_baris_final_tidak_boleh_dikirim_sebagai_non_final():
    m = mig(state="done")
    assert reason(lambda: P.check_terminal_usable(
        term(), m, enforcement="strict")) == "migration_row_final_passed"


@pytest.mark.parametrize("state", ["committed", "synced"])
def test_koreksi1_capability_sah_di_committed_dan_synced(state):
    m = mig(state=state)
    t = term(op_owner=m["owner_tag"])
    for enf in P.ENFORCEMENTS:
        P.check_terminal_usable(t, m, enforcement=enf, capability=cap())


@pytest.mark.parametrize("state", [s for s in P.MIGRATION_STATES
                                   if s not in P.CAPABILITY_STATES and
                                   s not in P.MIGRATION_FINAL_STATES])
def test_koreksi1_capability_tidak_sah_di_state_lain(state):
    m = mig(state=state)
    t = term(op_owner=m["owner_tag"])
    assert reason(lambda: P.check_terminal_usable(
        t, m, enforcement="strict", capability=cap())) == \
        "capability_mismatch"


@pytest.mark.parametrize("bad", [
    {"generation": 1},                     # CD4: capability gen lama
    {"generation": 3},
    {"migration_id": MID2},
    {"terminal_id": 8},
    {"account_id": 71},
])
def test_cd4_capability_basi_atau_salah_ditolak(bad):
    m = mig(state="committed")
    t = term(op_owner=m["owner_tag"])
    assert reason(lambda: P.check_terminal_usable(
        t, m, enforcement="strict", capability=cap(**bad))) == \
        "capability_mismatch"


def test_capability_owner_tag_harus_sama_dengan_op_owner():
    m = mig(state="committed")
    t = term(op_owner=P.make_owner_tag(MID, 1))     # owner basi di terminal
    assert reason(lambda: P.check_terminal_usable(
        t, m, enforcement="strict", capability=cap())) == \
        "capability_mismatch"


def test_capability_identitas_belum_commit_ditolak():
    m = mig(state="committed")
    t = term(op_owner=m["owner_tag"], provision_id=OLD)
    assert reason(lambda: P.check_terminal_usable(
        t, m, enforcement="strict", capability=cap())) == \
        "capability_identity_mismatch"
    t2 = term(op_owner=m["owner_tag"], rpc_mode="classic")
    assert reason(lambda: P.check_terminal_usable(
        t2, m, enforcement="strict", capability=cap())) == \
        "capability_identity_mismatch"


def test_capability_tanpa_migrasi_ditolak():
    assert reason(lambda: P.check_terminal_usable(
        term(), None, enforcement="strict", capability=cap())) == \
        "capability_unexpected"


def test_owner_lain_bukan_wewenang_modul():
    """op_owner job:/remove: ditangani logika provisioning lama."""
    P.check_terminal_usable(term(op_owner="job:" + "c" * 32), None,
                            enforcement="strict")


# -------------------------------------------- readiness (koreksi #5)
def acc(state="active"):
    return {"id": 70, "state": state}


def test_readiness_done_sehat_dan_tidak_sehat():
    m = mig(state="done")
    assert P.migration_readiness_problems(m, term(), acc()) == []
    assert "terminal_not_ro" in P.migration_readiness_problems(
        m, term(rpc_mode="classic"), acc())
    assert "terminal_pid_mismatch" in P.migration_readiness_problems(
        m, term(provision_id=OLD), acc())
    assert "terminal_owner_held" in P.migration_readiness_problems(
        m, term(op_owner=m["owner_tag"]), acc())


@pytest.mark.parametrize("state", [s for s in P.MIGRATION_STATES
                                   if s not in P.MIGRATION_FINAL_STATES])
def test_readiness_migrasi_terbuka_merah(state):
    assert P.migration_readiness_problems(mig(state=state), term(),
                                          acc()) == ["migration_open"]


def test_koreksi5_failed_credentials_sehat_hanya_bila_direkonsiliasi():
    m = mig(state="failed_credentials")
    ok_t = term(lifecycle="stopped")
    assert P.migration_readiness_problems(
        m, ok_t, acc("credentials_rejected")) == []
    cases = {
        "failed_credentials_terminal_not_stopped":
            (term(lifecycle="connected"), acc("credentials_rejected")),
        "failed_credentials_account_not_rejected":
            (ok_t, acc("active")),
        "terminal_not_ro":
            (term(lifecycle="stopped", rpc_mode="classic"),
             acc("credentials_rejected")),
        "terminal_pid_mismatch":
            (term(lifecycle="stopped", provision_id=OLD),
             acc("credentials_rejected")),
        "terminal_owner_held":
            (term(lifecycle="stopped", op_owner=m["owner_tag"]),
             acc("credentials_rejected")),
    }
    for want, (t, a) in cases.items():
        assert want in P.migration_readiness_problems(m, t, a), want
    assert P.migration_readiness_problems(
        dict(m, new_provision_id=None), ok_t,
        acc("credentials_rejected")) == ["terminal_pid_mismatch"]


def test_readiness_cancelled():
    m = mig(state="cancelled")
    assert P.migration_readiness_problems(m, term(rpc_mode="classic"),
                                          acc()) == []
    assert P.migration_readiness_problems(
        m, term(op_owner=m["owner_tag"]), acc()) == ["cancelled_owner_held"]


def test_readiness_state_atau_identitas_tidak_sah():
    assert P.migration_readiness_problems(mig(state="xx"), term(),
                                          acc()) == ["migration_state_invalid"]
    assert P.migration_readiness_problems(
        mig(state="done", id="zz"), term(), acc()) == \
        ["migration_identity_invalid"]
    assert P.migration_readiness_problems(mig(state="done"), None,
                                          acc()) == ["terminal_missing"]


# ---------------------------------------------------------- broker_allow
@pytest.mark.parametrize("raw,want", [
    ("10.0.0.0/8", ["10.0.0.0/8"]),
    ("10.0.0.5/8 192.0.2.1", ["10.0.0.0/8", "192.0.2.1/32"]),
    ("2001:db8::/32 2001:db8::/32", ["2001:db8::/32"]),
])
def test_broker_allow_cidr_sah(raw, want):
    assert P.parse_broker_allow(raw, production=True) == want
    assert P.parse_broker_allow(raw, production=False) == want


def test_broker_allow_any_hanya_non_produksi():
    assert P.parse_broker_allow("any", production=False) == ["any"]
    with pytest.raises(ValueError):
        P.parse_broker_allow("any", production=True)


@pytest.mark.parametrize("raw", [
    "", "   ", "broker.example.com", "10.0.0.0/8\nExecStart=/bin/sh",
    "10.0.0.0/8\r", "any 10.0.0.0/8", "10.0.0.0/33", "1.2.3", None,
    "10.0.0.0/8\x00",
])
def test_broker_allow_tidak_sah(raw):
    for prod in (True, False):
        with pytest.raises(ValueError):
            P.parse_broker_allow(raw, production=prod)


# ---------------------------------------------------------------- secret
HEX64 = "0123456789abcdef" * 4                 # 32 byte
B64 = "Tm90QVNlY3JldEp1c3RBVGVzdFZhbHVlRm9yUG9saWN5MTI"   # >= 32 byte


def test_fp5_kekuatan_secret():
    assert P.secret_strength_problem(HEX64) is None
    assert P.secret_strength_problem(B64) is None
    assert P.secret_strength_problem(None) == "secret_missing"
    assert P.secret_strength_problem("") == "secret_placeholder"
    assert P.secret_strength_problem("changeme") == "secret_placeholder"
    assert P.secret_strength_problem("<openssl rand -hex 32>") == \
        "secret_placeholder"
    assert P.secret_strength_problem("ab" * 31) == "secret_too_short"
    assert P.secret_strength_problem("00" * 40) == "secret_low_entropy"
    assert P.secret_strength_problem("p@ss word!") == "secret_encoding"
    assert P.secret_strength_problem(HEX64, forbid_equal_to=(HEX64,)) == \
        "secret_reused"
    assert P.secret_strength_problem(HEX64, forbid_equal_to=("", None)) \
        is None


def test_fp1_fp4_bukti_selfcheck():
    n1, n2 = P.new_nonce(), P.new_nonce()
    assert n1 != n2 and len(n1) == 64
    proof = P.selfcheck_proof(HEX64, n1)
    assert len(proof) == 64 and HEX64 not in proof
    assert P.verify_selfcheck_proof(HEX64, n1, proof) is True
    # FP2: secret berbeda -> false; FP4: bukti nonce lama untuk nonce baru
    assert P.verify_selfcheck_proof("f" * 64, n1, proof) is False
    assert P.verify_selfcheck_proof(HEX64, n2, proof) is False
    for bad in (None, "", proof.upper(), proof[:-1], 12):
        assert P.verify_selfcheck_proof(HEX64, n1, bad) is False
    for bad_nonce in ("", "A" * 64, "a" * 63, None):
        with pytest.raises(ValueError):
            P.selfcheck_proof(HEX64, bad_nonce)
    with pytest.raises(ValueError):
        P.selfcheck_proof("", n1)


# ------------------------------------------------------------ TO1: lease
def test_to1_syarat_lease():
    need = P.min_lease_seconds(45, 5, 10, 15)
    assert need == 150.0
    assert P.lease_problem(170, op_timeout=45, lock_wait=5, term_grace=10,
                           inspect_timeout=15) is None
    assert P.lease_problem(149, op_timeout=45, lock_wait=5, term_grace=10,
                           inspect_timeout=15).startswith("lease_too_short")
    assert P.drain_seconds(45, 5, 10) == 70.0


def test_konstanta_state_konsisten():
    assert set(P.CAPABILITY_STATES) <= set(P.MIGRATION_FORWARD_STATES)
    assert set(P.MIGRATION_FINAL_STATES) <= set(P.MIGRATION_STATES)
    assert set(P.CANCELLABLE_STATES) <= set(P.MIGRATION_FORWARD_STATES)
    assert "removing" not in P.CANCELLABLE_STATES
    assert len(set(P.MIGRATION_STATES)) == len(P.MIGRATION_STATES)


# ------------------------- review G1 fix 2: audit-all fail-closed (RD)
_AUDIT_OK = {"ok": True, "audit_complete": True, "inventory_errors": [],
             "terminals": [{"code": "C001"}]}


def test_audit_inventory_lengkap_sehat():
    assert P.audit_inventory_problems(dict(_AUDIT_OK)) == []


@pytest.mark.parametrize("patch,expect", [
    ({"audit_complete": False, "inventory_errors": ["proc:PermissionError"]},
     ["audit_incomplete", "inventory_error:proc:PermissionError"]),
    ({"audit_complete": None}, ["audit_incomplete"]),
    ({"audit_complete": "true"}, ["audit_incomplete"]),
    ({"audit_complete": 1}, ["audit_incomplete"]),
    ({"inventory_errors": ["pwd:OSError"]},
     ["audit_complete_inconsistent", "inventory_error:pwd:OSError"]),
    ({"inventory_errors": None}, ["audit_inventory_errors_missing"]),
    ({"inventory_errors": [5]}, ["audit_complete_inconsistent",
                                 "inventory_error:invalid"]),
    ({"ok": False}, ["audit_not_ok"]),
    ({"terminals": None}, ["audit_terminals_missing"]),
    ({"terminals": [{"code": "C001", "error": True}]},
     ["audit_terminal_unreadable"]),
])
def test_audit_inventory_tidak_lengkap_merah(patch, expect):
    a = dict(_AUDIT_OK)
    a.update(patch)
    assert P.audit_inventory_problems(a) == expect


def test_audit_inventory_helper_lama_tanpa_field_merah():
    old = {"ok": True, "terminals": []}       # helper sebelum fix: tidak ada
    probs = P.audit_inventory_problems(old)
    assert "audit_incomplete" in probs
    assert "audit_inventory_errors_missing" in probs


@pytest.mark.parametrize("bad", [None, [], "ok", 0])
def test_audit_inventory_bukan_dict_merah(bad):
    assert P.audit_inventory_problems(bad) == ["audit_missing"]
