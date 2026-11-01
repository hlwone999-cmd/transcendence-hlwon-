"""Test perlindungan READ-ONLY — kasus #10 dari spesifikasi.

Provider TIDAK BOLEH mengekspos order_send/order_check/eval/execute.
"""
import pytest

from mt5client.mock import MockMT5Client
from mt5client.mt5linux_provider import MT5LinuxProvider
from mt5client.provider import FORBIDDEN, READ_METHODS, ReadOnlyViolationError


@pytest.mark.parametrize("name", [
    "order_send", "order_check", "order_calc_margin", "order_calc_profit",
    "eval", "execute", "positions_close", "order_modify", "apa_saja",
])
def test_mock_blocks_mutation(name):
    m = MockMT5Client()
    with pytest.raises(ReadOnlyViolationError):
        getattr(m, name)


def test_real_provider_blocks_mutation(cfg):
    p = MT5LinuxProvider(cfg)
    for name in ("order_send", "order_check", "eval", "execute"):
        with pytest.raises(ReadOnlyViolationError):
            getattr(p, name)


def test_provider_surface_is_whitelisted():
    """Method publik provider = persis whitelist baca (tanpa kejutan)."""
    public = {n for n in dir(MockMT5Client) if not n.startswith("_")}
    expected = set(READ_METHODS) | {
        # atribut mock untuk kontrol test — bukan jalur ke MT5
        "fail_connect", "fail_history", "connect_calls",
    }
    assert public <= expected, public - expected


def test_forbidden_list_covers_execution():
    for name in ("order_send", "eval", "execute"):
        assert name in FORBIDDEN


def test_read_methods_exist():
    m = MockMT5Client()
    for name in ("account_info", "symbol_info", "positions_get",
                 "history_deals_get", "history_orders_get"):
        assert callable(getattr(m, name))
