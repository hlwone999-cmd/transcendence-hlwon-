"""Mock MT5 client untuk automated tests — TANPA terminal asli.

Field namedtuple mengikuti package resmi MetaTrader5 (py):
- TradeDeal: ticket, order, time, time_msc, type, entry, magic,
  position_id, reason, volume, price, commission, swap, profit,
  symbol, comment, external_id, fee
- TradeOrder: ..., state, magic, position_id, volume_initial,
  volume_current, price_open, sl, tp, price_current, symbol, comment
- TradePosition: ticket, time, type, magic, identifier, volume,
  price_open, sl, tp, price_current, swap, profit, symbol, comment
- AccountInfo: login, leverage, balance, equity, margin_free, currency,
  name, server, company, ...

Konstanta mengikuti MQL5:
DEAL_TYPE: BUY=0 SELL=1 BALANCE=2 CREDIT=3 CHARGE=4 CORRECTION=5 BONUS=6
DEAL_ENTRY: IN=0 OUT=1 INOUT=2 OUT_BY=3
DEAL_REASON: CLIENT=0 MOBILE=1 WEB=2 EXPERT=3 SL=4 TP=5
ORDER_TYPE: BUY=0 SELL=1 ...; ORDER_STATE_FILLED=4 PARTIAL=3
"""
from __future__ import annotations

import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from .provider import MT5ConnectionError, MT5ProviderBase, plain

# --- konstanta MQL5 -------------------------------------------------------
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_TYPE_BALANCE = 2
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_REASON_CLIENT = 0
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_STATE_FILLED = 4

TradeDeal = namedtuple("TradeDeal", "ticket order time time_msc type entry magic position_id reason volume price commission swap profit symbol comment external_id fee")
TradeOrder = namedtuple(
    "TradeOrder",
    "ticket time time_msc time_update time_update_msc time_setup time_setup_msc "
    "time_done time_done_msc time_expiration type type_time type_filling state "
    "magic position_id volume_initial volume_current price_open sl tp "
    "price_current symbol comment external_id",
)
TradePosition = namedtuple(
    "TradePosition",
    "ticket time time_msc time_update time_update_msc type magic identifier "
    "volume price_open sl tp price_current swap profit symbol comment external_id",
)
AccountInfo = namedtuple(
    "AccountInfo",
    "login trade_mode leverage limit_orders margin_so_mode trade_allowed "
    "trade_expert margin_mode currency_digits fifo_close balance credit profit "
    "equity margin margin_free margin_level margin_so_call margin_so_so "
    "margin_initial margin_maintenance assets liabilities commission_blocked "
    "name email server currency company",
)
SymbolInfo = namedtuple(
    "SymbolInfo",
    "name point digits trade_tick_size trade_tick_value trade_contract_size description",
)

T0 = int(datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc).timestamp())


def t(minutes: int) -> int:
    """Epoch detik setelah T0 — deterministik untuk test."""
    return T0 + minutes * 60


def _deal(ticket, order, minutes, dtype, entry, position_id, volume, price,
          profit=0.0, commission=0.0, swap=0.0, fee=0.0,
          symbol="XAUUSD", comment="", magic=777, reason=DEAL_REASON_CLIENT):
    sec = t(minutes)
    return TradeDeal(
        ticket=ticket, order=order, time=sec, time_msc=sec * 1000,
        type=dtype, entry=entry, magic=magic, position_id=position_id,
        reason=reason, volume=volume, price=price,
        commission=commission, swap=swap, profit=profit,
        symbol=symbol, comment=comment, external_id=0, fee=fee,
    )


def _order(ticket, minutes, otype, position_id, volume, price_open, sl, tp,
           symbol="XAUUSD", state=ORDER_STATE_FILLED, volume_current=0.0, magic=777):
    sec = t(minutes)
    return TradeOrder(
        ticket=ticket, time=sec, time_msc=sec * 1000, time_update=sec,
        time_update_msc=sec * 1000, time_setup=sec, time_setup_msc=sec * 1000,
        time_done=sec, time_done_msc=sec * 1000, time_expiration=0,
        type=otype, type_time=0, type_filling=0, state=state,
        magic=magic, position_id=position_id,
        volume_initial=volume, volume_current=volume_current,
        price_open=price_open, sl=sl, tp=tp, price_current=price_open,
        symbol=symbol, comment="", external_id=0,
    )


def scenario_dataset():
    """Skenario lengkap & deterministik:

    - Deposit +1000 (deal 100) dan withdrawal -250 (deal 200) -> BUKAN trade.
    - Posisi 101 XAUUSD BUY : scale-in (0.10@2000.00 + 0.10@2005.00),
      partial close (0.05@2010.00 +50), close (0.15@2015.00 +150).
      Komisi -2.0 per lot-entry deal, swap -1.0 pada deal penutup terakhir.
    - Posisi 105 XAUUSD SELL : 0.02@2020.00 masih TERBUKA.
    - Posisi 110 EURUSD BUY : scale-out (0.50@1.10000; keluar 0.20@1.10200
      +40 lalu 0.30@1.09800 -60), komisi -3.0. Trade rugi.
    """
    deals = [
        # --- non-trading -------------------------------------------------
        _deal(100, 0, 0, DEAL_TYPE_BALANCE, DEAL_ENTRY_IN, 0, 0.0, 0.0, profit=1000.0, symbol="", comment="Deposit"),
        _deal(200, 0, 6000, DEAL_TYPE_BALANCE, DEAL_ENTRY_IN, 0, 0.0, 0.0, profit=-250.0, symbol="", comment="Withdrawal"),
        # --- posisi 101: BUY scale-in + partial close + close ------------
        _deal(101, 501, 10, DEAL_TYPE_BUY, DEAL_ENTRY_IN, 101, 0.10, 2000.00, commission=-2.0),
        _deal(102, 502, 20, DEAL_TYPE_BUY, DEAL_ENTRY_IN, 101, 0.10, 2005.00, commission=-2.0),
        _deal(103, 503, 30, DEAL_TYPE_SELL, DEAL_ENTRY_OUT, 101, 0.05, 2010.00, profit=50.0),
        _deal(104, 504, 40, DEAL_TYPE_SELL, DEAL_ENTRY_OUT, 101, 0.15, 2015.00, profit=150.0, swap=-1.0, reason=DEAL_REASON_TP),
        # --- posisi 105: SELL masih terbuka ------------------------------
        _deal(105, 505, 50, DEAL_TYPE_SELL, DEAL_ENTRY_IN, 105, 0.02, 2020.00),
        # --- posisi 110: EURUSD scale-out --------------------------------
        _deal(110, 510, 70, DEAL_TYPE_BUY, DEAL_ENTRY_IN, 110, 0.50, 1.10000, commission=-3.0, symbol="EURUSD"),
        _deal(111, 511, 80, DEAL_TYPE_SELL, DEAL_ENTRY_OUT, 110, 0.20, 1.10200, profit=40.0, symbol="EURUSD"),
        _deal(112, 512, 90, DEAL_TYPE_SELL, DEAL_ENTRY_OUT, 110, 0.30, 1.09800, profit=-60.0, symbol="EURUSD"),
    ]
    orders = [
        _order(501, 10, ORDER_TYPE_BUY, 101, 0.10, 2000.00, 1995.00, 2050.00),
        _order(502, 20, ORDER_TYPE_BUY, 101, 0.10, 2005.00, 1995.00, 2050.00),
        _order(505, 50, ORDER_TYPE_SELL, 105, 0.02, 2020.00, 2030.00, 2000.00),
        _order(510, 70, ORDER_TYPE_BUY, 110, 0.50, 1.10000, 1.09500, 0.0, symbol="EURUSD"),
    ]
    positions = [
        TradePosition(
            ticket=105, time=t(50), time_msc=t(50) * 1000, time_update=t(50),
            time_update_msc=t(50) * 1000, type=ORDER_TYPE_SELL, magic=777,
            identifier=105, volume=0.02, price_open=2020.00, sl=2030.00,
            tp=2000.00, price_current=2019.50, swap=0.0, profit=0.10,
            symbol="XAUUSD", comment="", external_id=0,
        ),
    ]
    symbols = {
        "XAUUSD": SymbolInfo(name="XAUUSD", point=0.01, digits=2,
                             trade_tick_size=0.01, trade_tick_value=1.0,
                             trade_contract_size=100.0, description="Gold vs USD"),
        "EURUSD": SymbolInfo(name="EURUSD", point=0.00001, digits=5,
                             trade_tick_size=0.00001, trade_tick_value=1.0,
                             trade_contract_size=100000.0, description="Euro vs USD"),
    }
    account = AccountInfo(
        login=12345678, trade_mode=0, leverage=500, limit_orders=200,
        margin_so_mode=0, trade_allowed=False, trade_expert=False,
        margin_mode=0, currency_digits=2, fifo_close=False,
        balance=750.0, credit=0.0, profit=0.10, equity=750.10,
        margin=0.0, margin_free=750.10, margin_level=0.0,
        margin_so_call=50.0, margin_so_so=20.0, margin_initial=0.0,
        margin_maintenance=0.0, assets=0.0, liabilities=0.0,
        commission_blocked=False, name="Trader Uji", email="t@example.com",
        server="Exness-MT5Real", currency="USD", company="Exness",
    )
    return {"deals": deals, "orders": orders, "positions": positions,
            "symbols": symbols, "account": account}


class MockMT5Client(MT5ProviderBase):
    """Mock provider: dataset statis + mode gagal untuk test error path.

    Knob test verifikasi live (hardening post-redesign):
    - fail_connect : connect() raise MT5ConnectionError (terminal/broker
      tidak terjangkau — jalur unreachable/timeout).
    - fail_auth    : connect() raise MT5ConnectionError dengan pesan
      "Invalid login or password" — simulasi broker MENOLAK kredensial
      (password salah / server tidak cocok), berbeda dari unreachable.
    - trade_allowed: override account_info().trade_allowed dataset.
      None = pakai nilai dataset. False = sesi INVESTOR read-only
      (semestinya); True = sesi password MASTER (dipakai test deteksi
      password master — MetaTrader5 tidak membedakan keduanya di
      initialize, satu-satunya sinyalnya trade_allowed sesi ini).

    Catatan: class ini sengaja TIDAK mendefinisikan order_send/eval/execute —
    pemanggilannya jatuh ke __getattr__ dan raise ReadOnlyViolationError.
    """

    def __init__(self, dataset: dict | None = None, *,
                 fail_connect: bool = False,
                 fail_history: bool = False,
                 fail_auth: bool = False,
                 trade_allowed: bool | None = None,
                 connect_delay: float = 0.0):
        self._ds = dataset if dataset is not None else scenario_dataset()
        if trade_allowed is not None and self._ds.get("account") is not None:
            self._ds["account"] = self._ds["account"]._replace(
                trade_allowed=trade_allowed)
        self._connected = False
        self.fail_connect = fail_connect
        self.fail_history = fail_history
        self.fail_auth = fail_auth
        self.connect_delay = connect_delay
        self.connect_calls = 0

    # --- lifecycle ------------------------------------------------------
    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_delay:
            time.sleep(self.connect_delay)
        if self.fail_connect:
            raise MT5ConnectionError("mock: mt5server tidak terjangkau")
        if self.fail_auth:
            # Bentuk pesan meniru MT5LinuxProvider: initialize gagal +
            # last_error terminal berisi alasan otorisasi broker.
            raise MT5ConnectionError(
                "MT5 initialize gagal (-6, 'Terminal: Invalid login or "
                "password')")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # --- baca -------------------------------------------------------------
    def account_info(self):
        return plain(self._ds["account"])

    def terminal_info(self):
        return {"name": "MockTerminal", "connected": True}

    def symbol_info(self, symbol: str):
        info = self._ds["symbols"].get(symbol)
        return plain(info) if info else None

    def positions_get(self, symbol: str | None = None):
        rows = self._ds["positions"]
        if symbol:
            rows = [p for p in rows if p.symbol == symbol]
        return plain(rows)

    def history_deals_get(self, date_from: datetime, date_to: datetime):
        if self.fail_history:
            raise MT5ConnectionError("mock: history deals gagal")
        f, to = int(date_from.timestamp()), int(date_to.timestamp())
        return plain([d for d in self._ds["deals"] if f <= d.time <= to])

    def history_orders_get(self, date_from: datetime, date_to: datetime):
        f, to = int(date_from.timestamp()), int(date_to.timestamp())
        return plain([o for o in self._ds["orders"] if f <= int(o.time) <= to])
