"""P-PARITY — helper privileged (stdlib, tidak mengimpor backend) WAJIB
menerapkan kebijakan yang SAMA dengan security/rpc_policy.py."""
import pytest

from provisioner import mt5_provision_helper as H
from security import rpc_policy as P

BROKER_VECTORS = [
    "any", "10.0.0.0/8", "10.0.0.5/8 192.0.2.1", "2001:db8::/32",
    "2001:db8::/32 2001:db8::/32", "", "  ", "broker.example.com",
    "10.0.0.0/8\nExecStart=/bin/sh", "any 10.0.0.0/8", "10.0.0.0/33",
    "1.2.3", "10.0.0.0/8\x00", "\t10.0.0.0/8",
]
SECRET_VECTORS = [
    None, "", "changeme", "CHANGEME", "secret", "<openssl rand -hex 32>",
    "ab" * 31, "00" * 40, "p@ss word!", "0123456789abcdef" * 4,
    "Tm90QVNlY3JldEp1c3RBVGVzdFZhbHVlRm9yUG9saWN5MTI", "x" * 100,
    "  " + "0123456789abcdef" * 4 + "  ",
]


def test_konstanta_identik():
    assert H.HELPER_PRODUCTION_RPC_MODES == P.PRODUCTION_RPC_MODES
    assert H.HELPER_KNOWN_RPC_MODES == P.KNOWN_RPC_MODES
    assert H.SELFCHECK_DOMAIN == P.SELFCHECK_DOMAIN
    assert H.SECRET_MIN_BYTES == P.SECRET_MIN_BYTES
    assert H.SECRET_PLACEHOLDERS == P.SECRET_PLACEHOLDERS
    assert H.EXIT_FENCE == 8 and H.EXIT_POLICY == 9


def _outcome(fn, *a, **kw):
    try:
        return ("ok", fn(*a, **kw))
    except ValueError:
        return ("err", None)


@pytest.mark.parametrize("raw", BROKER_VECTORS)
@pytest.mark.parametrize("prod", [True, False])
def test_parse_broker_allow_identik(raw, prod):
    assert _outcome(H.parse_broker_allow, raw, production=prod) == \
        _outcome(P.parse_broker_allow, raw, production=prod)


@pytest.mark.parametrize("secret", SECRET_VECTORS)
def test_secret_strength_identik(secret):
    assert H.secret_strength_problem(secret) == \
        P.secret_strength_problem(secret)


def test_selfcheck_proof_identik():
    for secret in ("0123456789abcdef" * 4, "s" * 40, "ü-secret"):
        for _ in range(5):
            n = P.new_nonce()
            assert H.selfcheck_proof(secret, n) == \
                P.selfcheck_proof(secret, n)
    for bad in ("", "A" * 64, None):
        assert _outcome(H.selfcheck_proof, "s" * 40, bad) == \
            _outcome(P.selfcheck_proof, "s" * 40, bad)
