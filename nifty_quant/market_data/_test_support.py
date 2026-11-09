"""Test-only helpers: a protobuf encoder for V3 frames and fake provider/connection doubles.

This module is imported by the test suite and by nothing else: it is not on any runtime
path, and ``nifty_quant.market_data`` does not import it.  It lives inside the package
(rather than under ``tests/``) so the fixtures are importable identically whether pytest
is invoked as ``pytest`` or ``python -m pytest``.

The encoder writes the same field numbers the decoder reads, from the provider's
published ``MarketDataFeed.proto``.  Encoding and decoding are therefore two independent
implementations of the schema, which is what makes a round-trip test meaningful: a wrong
field number on either side fails the test instead of agreeing with itself.
"""

from __future__ import annotations

import struct
from datetime import datetime

import pandas as pd

from nifty_quant.market_data.providers.base import (
    InstrumentKind,
    MarketPhase,
    MarketStatusSnapshot,
    ModeLimit,
    ProviderCapabilities,
    Subscription,
    SubscriptionMode,
)

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH = 2


# ---------------------------------------------------------------------------
# Encoder primitives
# ---------------------------------------------------------------------------
def varint(value: int) -> bytes:
    """Base-128 varint, two's complement for negative values (protobuf int64)."""
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def key(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def f_varint(field: int, value: int) -> bytes:
    return key(field, WIRE_VARINT) + varint(int(value))


def f_double(field: int, value: float) -> bytes:
    return key(field, WIRE_64BIT) + struct.pack("<d", float(value))


def f_string(field: int, text: str) -> bytes:
    payload = text.encode("utf-8")
    return key(field, WIRE_LENGTH) + varint(len(payload)) + payload


def f_message(field: int, payload: bytes) -> bytes:
    return key(field, WIRE_LENGTH) + varint(len(payload)) + payload


def f_map(entries: dict[str, bytes], field: int = 2) -> bytes:
    """A protobuf map<string, message> as repeated entry messages."""
    out = b""
    for name, payload in entries.items():
        entry = f_string(1, name) + f_message(2, payload)
        out += f_message(field, entry)
    return out


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
def ltpc(ltp: float | None = None, ltt: int | None = None, ltq: int | None = None,
         cp: float | None = None) -> bytes:
    out = b""
    if ltp is not None:
        out += f_double(1, ltp)
    if ltt is not None:
        out += f_varint(2, ltt)
    if ltq is not None:
        out += f_varint(3, ltq)
    if cp is not None:
        out += f_double(4, cp)
    return out


def quote(bid_qty: int, bid_price: float, ask_qty: int, ask_price: float) -> bytes:
    return (f_varint(1, bid_qty) + f_double(2, bid_price)
            + f_varint(3, ask_qty) + f_double(4, ask_price))


def market_level(levels: list[tuple[int, float, int, float]]) -> bytes:
    return b"".join(f_message(1, quote(*level)) for level in levels)


def option_greeks(delta=0.5, theta=-1.0, gamma=0.01, vega=2.0, rho=0.1) -> bytes:
    return (f_double(1, delta) + f_double(2, theta) + f_double(3, gamma)
            + f_double(4, vega) + f_double(5, rho))


def ohlc(interval: str, o: float, h: float, low: float, c: float, vol: int, ts: int) -> bytes:
    return (f_string(1, interval) + f_double(2, o) + f_double(3, h) + f_double(4, low)
            + f_double(5, c) + f_varint(6, vol) + f_varint(7, ts))


def market_ohlc(rows: list[tuple]) -> bytes:
    return b"".join(f_message(1, ohlc(*row)) for row in rows)


def market_full_feed(ltp: float, ltt: int, ltq: int, cp: float, *, vtt: int | None = None,
                     oi: float | None = None, iv: float | None = None,
                     depth: list[tuple] | None = None, greeks: bytes | None = None,
                     atp: float | None = None) -> bytes:
    out = f_message(1, ltpc(ltp=ltp, ltt=ltt, ltq=ltq, cp=cp))
    if depth:
        out += f_message(2, market_level(depth))
    if greeks:
        out += f_message(3, greeks)
    if atp is not None:
        out += f_double(5, atp)
    if vtt is not None:
        out += f_varint(6, vtt)
    if oi is not None:
        out += f_double(7, oi)
    if iv is not None:
        out += f_double(8, iv)
    return out


def index_full_feed(ltp: float, ltt: int, ltq: int, cp: float) -> bytes:
    return f_message(1, ltpc(ltp=ltp, ltt=ltt, ltq=ltq, cp=cp))


def feed_ltpc(payload: bytes) -> bytes:
    return f_message(1, payload)


def feed_full(market: bytes) -> bytes:
    return f_message(2, f_message(1, market))  # Feed.fullFeed -> FullFeed.marketFF


def feed_index(index: bytes) -> bytes:
    return f_message(2, f_message(2, index))  # Feed.fullFeed -> FullFeed.indexFF


def feed_response(feeds: dict[str, bytes], *, type_code: int = 1,
                  current_ts: int | None = None) -> bytes:
    out = f_varint(1, type_code)
    out += f_map(feeds, 2)
    if current_ts is not None:
        out += f_varint(3, current_ts)
    return out


def market_info_frame(segments: dict[str, int]) -> bytes:
    entries = b"".join(f_message(1, f_string(1, name) + f_varint(2, code))
                       for name, code in segments.items())
    return f_varint(1, 2) + f_message(4, entries)


# ---------------------------------------------------------------------------
# Domain doubles
# ---------------------------------------------------------------------------
def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def subscription(key: str, kind: InstrumentKind = InstrumentKind.OPTION, *,
                 mode: SubscriptionMode = SubscriptionMode.FULL, symbol: str = "",
                 underlying: str = "NIFTY", expiry: str = "2026-11-10",
                 strike: int | None = 25000, option_type: str = "CE") -> Subscription:
    return Subscription(instrument_key=key, kind=kind, mode=mode, trading_symbol=symbol,
                        underlying=underlying, expiry=expiry, strike=strike,
                        option_type=option_type)


class FakeProvider:
    """A provider double: canned discovery responses, no network."""

    CAPABILITY = ProviderCapabilities(
        name="fake",
        option_chain=True,
        option_greeks=True,
        historical_intraday=True,
        market_status=True,
        mode_limits={
            SubscriptionMode.LTPC: ModeLimit(10, 10),
            SubscriptionMode.OPTION_GREEKS: ModeLimit(10, 10),
            SubscriptionMode.FULL: ModeLimit(20, 12),
            SubscriptionMode.FULL_D30: ModeLimit(5, 5),
        },
    )

    def __init__(self, *, master: list[dict] | None = None,
                 expiries: list[str] | None = None,
                 chains: dict[str, pd.DataFrame] | None = None,
                 authorized_uri: str = "wss://example.invalid/feed",
                 phase: MarketPhase = MarketPhase.OPEN,
                 status_error: Exception | None = None) -> None:
        self.capability = self.CAPABILITY
        self._master = master or []
        self._expiries = expiries if expiries is not None else ["2026-11-10", "2026-11-17"]
        self._chains = chains or {}
        self._authorized_uri = authorized_uri
        self.phase = phase
        self.status_error = status_error
        self.authorize_calls = 0
        self.chain_calls: list[tuple[str, str]] = []

    def instrument_master(self):
        return self._master

    def option_expiries(self, underlying_key: str):
        return sorted(self._expiries)

    def option_chain(self, underlying_key: str, expiry: str):
        self.chain_calls.append((underlying_key, expiry))
        return self._chains.get(expiry, pd.DataFrame())

    def authorize_feed(self):
        self.authorize_calls += 1
        return self._authorized_uri

    def market_status(self):
        if self.status_error is not None:
            raise self.status_error
        return MarketStatusSnapshot(exchange="NSE", phase=self.phase,
                                    raw_status=f"{self.phase.value}", source="fake",
                                    estimated=False)

    def describe(self):
        return {"provider": "fake"}

    def close(self):
        return None


class FakeConnection:
    """A scripted WebSocket: pops messages, then reports closure or a timeout."""

    def __init__(self, messages: list[bytes | str] | None = None, *,
                 close_after: bool = True, timeout_after: bool = False) -> None:
        self.messages = list(messages or [])
        self.sent: list[str] = []
        self.closed = False
        self.close_after = close_after
        self.timeout_after = timeout_after

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self, timeout: float | None = None):
        if self.messages:
            return self.messages.pop(0)
        if self.timeout_after:
            raise TimeoutError("no frame")
        raise ConnectionError("closed")

    def close(self) -> None:
        self.closed = True


def option_chain_frame(strikes=range(24800, 25201, 50), expiry: str = "2026-11-10",
                       spot: float | None = 25010.0) -> pd.DataFrame:
    """A tidy chain as the provider adapter returns it."""
    rows = []
    for strike in strikes:
        for option_type in ("CE", "PE"):
            rows.append({
                "instrument_key": f"NSE_FO|{strike}{option_type}",
                "expiry": expiry,
                "strike": int(strike),
                "option_type": option_type,
                "ltp": 100.0 + (strike - 25000) / 10,
                "close_price": 99.0,
                "volume": 1000,
                "oi": 5000,
                "prev_oi": 4800,
                "bid_price": 99.5,
                "bid_qty": 75,
                "ask_price": 100.5,
                "ask_qty": 75,
                "delta": 0.5, "theta": -1.0, "gamma": 0.01, "vega": 2.0, "rho": 0.1,
                "iv": 12.0,
                "underlying_spot_price": spot if spot is not None else float("nan"),
                "source_provider": "upstox",
            })
    return pd.DataFrame(rows)


#: Expiries used by the fixtures are far in the future (and one long expired) so the
#: tests never depend on the wall-clock date.
FUTURE_EXPIRY_ISO = "2030-01-03"
NEXT_EXPIRY_ISO = "2030-01-10"
#: 2030-01-03T00:00:00Z in epoch milliseconds, and an already-expired 2020 contract.
FUTURE_EXPIRY_MS = 1893628800000
EXPIRED_EXPIRY_MS = 1600000000000


def index_master_rows() -> list[dict]:
    return [
        {"segment": "NSE_INDEX", "instrument_type": "INDEX", "trading_symbol": "Nifty 50",
         "name": "NIFTY", "instrument_key": "NSE_INDEX|Nifty 50"},
        {"segment": "NSE_INDEX", "instrument_type": "INDEX", "trading_symbol": "India VIX",
         "name": "INDIA VIX", "instrument_key": "NSE_INDEX|India VIX"},
        {"segment": "NSE_FO", "instrument_type": "FUT", "trading_symbol": "NIFTY FUT 03 JAN 30",
         "underlying_symbol": "NIFTY", "instrument_key": "NSE_FO|FUT-JAN30",
         "expiry": FUTURE_EXPIRY_MS, "lot_size": 75},
        {"segment": "NSE_FO", "instrument_type": "FUT", "trading_symbol": "NIFTY FUT 26 MAR 20",
         "underlying_symbol": "NIFTY", "instrument_key": "NSE_FO|FUT-MAR20",
         "expiry": EXPIRED_EXPIRY_MS, "lot_size": 75},
    ]
