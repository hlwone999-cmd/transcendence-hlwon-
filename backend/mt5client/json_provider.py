"""Provider baca-saja dari snapshot JSON MT5."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .provider import (
    MT5ProviderBase,
    MT5ConnectionError,
)

SNAPSHOT = Path.home() / "mt5/sync-service/history.json"


def epoch(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value)


class JSONProvider(MT5ProviderBase):
    def __init__(self, config):
        self._cfg = config
        self._data = None

    def connect(self):
        try:
            self._data = json.loads(
                SNAPSHOT.read_text(encoding="utf-8")
            )
            if not isinstance(self._data.get("account"), dict):
                raise ValueError("Informasi akun tidak tersedia")
        except (OSError, ValueError, json.JSONDecodeError) as e:
            raise MT5ConnectionError(
                f"Snapshot MT5 tidak valid: {e}"
            ) from e

    def disconnect(self):
        self._data = None

    def is_connected(self):
        return self._data is not None

    def _require(self):
        if self._data is None:
            raise MT5ConnectionError("Provider belum terhubung")
        return self._data

    def account_info(self):
        return self._require()["account"]

    def terminal_info(self):
        return {}

    def symbol_info(self, symbol):
        return self._require()["symbols"].get(symbol)

    def positions_get(self, symbol=None):
        positions = self._require()["positions"]
        if symbol:
            return [p for p in positions if p.get("symbol") == symbol]
        return positions

    def history_deals_get(self, date_from, date_to):
        start, end = epoch(date_from), epoch(date_to)
        return [
            d for d in self._require()["deals"]
            if start <= d.get("time", 0) <= end
        ]

    def history_orders_get(self, date_from, date_to):
        start, end = epoch(date_from), epoch(date_to)
        return [
            o for o in self._require()["orders"]
            if start <= (
                o.get("time_done") or o.get("time_setup") or 0
            ) <= end
        ]
