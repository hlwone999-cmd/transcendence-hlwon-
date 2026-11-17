"""READ-ONLY MT5 — brief §11 #12: tidak ada jalur eksekusi order.

Dua lapis:
1. Runtime: whitelist MT5ProviderBase + __getattr__ blocker (test lama:
   test_provider_readonly.py).
2. Statik: scan AST seluruh backend — nama method MT5 tulis/eksekusi
   tidak boleh pernah dipanggil di kode produksi.
"""
import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# Nama API MT5 yang menulis/mengeksekusi — dilarang keras (brief §8).
# `execute`/`eval` dikecualikan dari pemindaian Call biasa karena
# conn.execute() adalah API adapter DB; builtin eval/exec diuji terpisah
# dengan regex yang tidak menyentuh conn.execute().
FORBIDDEN_CALLS = {
    "order_send", "order_check", "order_calc_margin", "order_calc_profit",
    "positions_modify", "positions_close", "positions_close_by",
    "order_modify", "order_delete", "orders_send",
    "buy", "sell", "close_position", "modify_position", "cancel_order",
    "trade_send",
}
# eval/exec/__import__ python-level (tanpa akses ke objek conn)
FORBIDDEN_BUILTIN_RE = r"(?<![\w.])(eval|exec|__import__)\s*\("

# File produksi yang discan (bukan tests/tools/docs).
SCAN_DIRS = ["api", "services", "mt5client", "security"]
SCAN_FILES = ["app.py", "config.py", "db.py", "auth.py", "audit.py",
              "run.py"]


def _py_files():
    files = []
    for d in SCAN_DIRS:
        files += list((BACKEND / d).rglob("*.py"))
    files += [BACKEND / f for f in SCAN_FILES if (BACKEND / f).exists()]
    return files


def test_tidak_ada_pemanggilan_fungsi_trading():
    """AST scan: panggilan order_send dkk tidak ada di backend."""
    violations = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name in FORBIDDEN_CALLS:
                    violations.append(f"{path.name}: {name}() baris "
                                      f"{node.lineno}")
    assert not violations, "DILARANG: " + "; ".join(violations)


def test_tidak_ada_eval_exec_builtin():
    """eval()/exec()/__import__() tidak dipakai di backend."""
    import re
    for path in _py_files():
        src = path.read_text(encoding="utf-8")
        m = re.search(FORBIDDEN_BUILTIN_RE, src)
        assert m is None, f"{path.name}: {m.group(0) if m else ''}"


def test_tidak_ada_placeholder_order_send_dalam_string():
    """Teks source tidak boleh menyertakan 'order_send' kecuali daftar
    larangan eksplisit (provider.py) dan docstring mock (infra test)."""
    allowed = {"provider.py", "mock.py", "__init__.py",
               "mt5linux_provider.py"}  # docstring kebijakan read-only
    for path in _py_files():
        if path.name in allowed:
            continue
        src = path.read_text(encoding="utf-8")
        assert "order_send" not in src, f"{path.name} menyebut order_send"


def test_provider_whitelist_tanpa_method_tulis():
    from mt5client.provider import FORBIDDEN, READ_METHODS
    # tidak ada irisan — whitelist murni baca
    assert not (set(READ_METHODS) & set(FORBIDDEN))
    for m in READ_METHODS:
        assert m in {"connect", "disconnect", "is_connected", "account_info",
                     "terminal_info", "symbol_info", "positions_get",
                     "history_deals_get", "history_orders_get"}


def test_mock_provider_juga_readonly():
    import pytest
    from mt5client.mock import MockMT5Client
    from mt5client.provider import ReadOnlyViolationError
    m = MockMT5Client()
    try:
        m.order_send({})
        raise AssertionError("order_send harus diblokir")
    except ReadOnlyViolationError:
        pass
