"""Dependency-free protobuf wire decoder for the Upstox Market Data Feed V3.

Why a hand-written decoder
--------------------------
The feed is protobuf-encoded and the official distribution ships a ``.proto`` file plus
generated bindings in each SDK.  This project is pinned to a fixed interpreter/library
contract and must not add a codegen step or a compiled artifact for one message family,
so the schema below is transcribed from the provider's own published proto
(``https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto``) and decoded
with a generic wire-format reader.  Consequences that are deliberate:

* Only the fields this project uses are decoded; unknown fields are skipped, so a
  provider adding a field cannot break collection.
* Groups (wire types 3/4) are rejected rather than mis-parsed.
* A malformed frame raises :class:`ProtobufError`; the caller records the frame and the
  failure instead of dropping it silently.

Decoded form
------------
Plain dictionaries with ``snake_case`` keys and milliseconds kept as integers.  This
module performs **no** semantic validation - a zero price is decoded as zero here and
judged in :mod:`nifty_quant.market_data.normalizer`.
"""

from __future__ import annotations

import struct

#: Wire types.
WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH = 2
WIRE_32BIT = 5

#: ``FeedResponse.Type`` enum.
FEED_TYPE_INITIAL = 0
FEED_TYPE_LIVE = 1
FEED_TYPE_MARKET_INFO = 2
FEED_TYPE_NAMES = {
    FEED_TYPE_INITIAL: "initial_feed",
    FEED_TYPE_LIVE: "live_feed",
    FEED_TYPE_MARKET_INFO: "market_info",
}

#: ``Feed.RequestMode`` enum.
REQUEST_MODE_NAMES = {0: "ltpc", 1: "full_d5", 2: "option_greeks", 3: "full_d30"}

#: ``MarketStatus`` enum.
MARKET_STATUS_NAMES = {
    0: "PRE_OPEN_START",
    1: "PRE_OPEN_END",
    2: "NORMAL_OPEN",
    3: "NORMAL_CLOSE",
    4: "CLOSING_START",
    5: "CLOSING_END",
}


class ProtobufError(ValueError):
    """A frame is not valid protobuf, or violates the schema this decoder implements."""


# ---------------------------------------------------------------------------
# Wire-format primitives
# ---------------------------------------------------------------------------
def read_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    """Read a base-128 varint, returning ``(value, next_position)``."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtobufError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtobufError("varint longer than 64 bits")


def to_int64(value: int) -> int:
    """Reinterpret an unsigned varint as a signed 64-bit integer (protobuf ``int64``)."""
    return value - (1 << 64) if value >= (1 << 63) else value


def iter_fields(buf: bytes):
    """Yield ``(field_number, wire_type, value)`` for every field in ``buf``.

    ``value`` is an ``int`` for varints and ``bytes`` for the fixed-width and
    length-delimited wire types.
    """
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        field = key >> 3
        wire = key & 0x7
        if field == 0:
            raise ProtobufError("field number 0 is invalid")
        if wire == WIRE_VARINT:
            raw, pos = read_varint(buf, pos)
            yield field, wire, to_int64(raw)
        elif wire == WIRE_64BIT:
            if pos + 8 > end:
                raise ProtobufError("truncated 64-bit field")
            yield field, wire, buf[pos:pos + 8]
            pos += 8
        elif wire == WIRE_LENGTH:
            length, pos = read_varint(buf, pos)
            if length < 0 or pos + length > end:
                raise ProtobufError("truncated length-delimited field")
            yield field, wire, buf[pos:pos + length]
            pos += length
        elif wire == WIRE_32BIT:
            if pos + 4 > end:
                raise ProtobufError("truncated 32-bit field")
            yield field, wire, buf[pos:pos + 4]
            pos += 4
        else:
            raise ProtobufError(f"unsupported wire type {wire} (groups are not used)")


def _double(raw: bytes) -> float:
    if len(raw) != 8:
        raise ProtobufError("double field is not 8 bytes")
    return struct.unpack("<d", raw)[0]


def _float(raw: bytes) -> float:
    if len(raw) != 4:
        raise ProtobufError("float field is not 4 bytes")
    return struct.unpack("<f", raw)[0]


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - defensive
        raise ProtobufError(f"string field is not UTF-8: {exc}") from exc


def _expect(value, wire: int, expected: int, field: str):
    if wire != expected:
        raise ProtobufError(f"{field}: wire type {wire} != {expected}")
    return value


# ---------------------------------------------------------------------------
# Message decoders (schema transcribed from MarketDataFeed.proto)
# ---------------------------------------------------------------------------
def decode_ltpc(buf: bytes) -> dict:
    """``LTPC``: ltp 1, ltt 2, ltq 3, cp 4."""
    out = {"ltp": None, "ltt": None, "ltq": None, "cp": None}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            out["ltp"] = _double(_expect(value, wire, WIRE_64BIT, "LTPC.ltp"))
        elif field == 2:
            out["ltt"] = _expect(value, wire, WIRE_VARINT, "LTPC.ltt")
        elif field == 3:
            out["ltq"] = _expect(value, wire, WIRE_VARINT, "LTPC.ltq")
        elif field == 4:
            out["cp"] = _double(_expect(value, wire, WIRE_64BIT, "LTPC.cp"))
    return out


def decode_quote(buf: bytes) -> dict:
    """``Quote``: bidQ 1, bidP 2, askQ 3, askP 4."""
    out = {"bid_qty": None, "bid_price": None, "ask_qty": None, "ask_price": None}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            out["bid_qty"] = _expect(value, wire, WIRE_VARINT, "Quote.bidQ")
        elif field == 2:
            out["bid_price"] = _double(_expect(value, wire, WIRE_64BIT, "Quote.bidP"))
        elif field == 3:
            out["ask_qty"] = _expect(value, wire, WIRE_VARINT, "Quote.askQ")
        elif field == 4:
            out["ask_price"] = _double(_expect(value, wire, WIRE_64BIT, "Quote.askP"))
    return out


def decode_market_level(buf: bytes) -> list[dict]:
    """``MarketLevel``: repeated ``Quote`` at field 1."""
    depth: list[dict] = []
    for field, wire, value in iter_fields(buf):
        if field == 1:
            depth.append(decode_quote(_expect(value, wire, WIRE_LENGTH, "MarketLevel.quote")))
    return depth


def decode_option_greeks(buf: bytes) -> dict:
    """``OptionGreeks``: delta 1, theta 2, gamma 3, vega 4, rho 5."""
    names = {1: "delta", 2: "theta", 3: "gamma", 4: "vega", 5: "rho"}
    out: dict = {name: None for name in names.values()}
    for field, wire, value in iter_fields(buf):
        if field in names:
            out[names[field]] = _double(_expect(value, wire, WIRE_64BIT, f"OptionGreeks.{names[field]}"))
    return out


def decode_ohlc(buf: bytes) -> dict:
    """``OHLC``: interval 1, open 2, high 3, low 4, close 5, vol 6, ts 7."""
    out: dict = {"interval": None, "open": None, "high": None, "low": None,
                 "close": None, "volume": None, "ts": None}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            out["interval"] = _text(_expect(value, wire, WIRE_LENGTH, "OHLC.interval"))
        elif field in (2, 3, 4, 5):
            name = {2: "open", 3: "high", 4: "low", 5: "close"}[field]
            out[name] = _double(_expect(value, wire, WIRE_64BIT, f"OHLC.{name}"))
        elif field == 6:
            out["volume"] = _expect(value, wire, WIRE_VARINT, "OHLC.vol")
        elif field == 7:
            out["ts"] = _expect(value, wire, WIRE_VARINT, "OHLC.ts")
    return out


def decode_market_ohlc(buf: bytes) -> list[dict]:
    """``MarketOHLC``: repeated ``OHLC`` at field 1 (1m / 30m / day intervals)."""
    return [decode_ohlc(value) for field, wire, value in iter_fields(buf)
            if field == 1 and _expect(value, wire, WIRE_LENGTH, "MarketOHLC.ohlc")]


def decode_market_full_feed(buf: bytes) -> dict:
    """``MarketFullFeed``: ltpc 1, marketLevel 2, optionGreeks 3, marketOHLC 4,
    atp 5, vtt 6, oi 7, iv 8, tbq 9, tsq 10."""
    out: dict = {"ltpc": None, "depth": [], "greeks": None, "ohlc": [],
                 "atp": None, "volume": None, "oi": None, "iv": None,
                 "total_buy_qty": None, "total_sell_qty": None}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            out["ltpc"] = decode_ltpc(_expect(value, wire, WIRE_LENGTH, "MarketFullFeed.ltpc"))
        elif field == 2:
            out["depth"] = decode_market_level(
                _expect(value, wire, WIRE_LENGTH, "MarketFullFeed.marketLevel"))
        elif field == 3:
            out["greeks"] = decode_option_greeks(
                _expect(value, wire, WIRE_LENGTH, "MarketFullFeed.optionGreeks"))
        elif field == 4:
            out["ohlc"] = decode_market_ohlc(
                _expect(value, wire, WIRE_LENGTH, "MarketFullFeed.marketOHLC"))
        elif field == 5:
            out["atp"] = _double(_expect(value, wire, WIRE_64BIT, "MarketFullFeed.atp"))
        elif field == 6:
            out["volume"] = _expect(value, wire, WIRE_VARINT, "MarketFullFeed.vtt")
        elif field == 7:
            out["oi"] = _double(_expect(value, wire, WIRE_64BIT, "MarketFullFeed.oi"))
        elif field == 8:
            out["iv"] = _double(_expect(value, wire, WIRE_64BIT, "MarketFullFeed.iv"))
        elif field == 9:
            out["total_buy_qty"] = _double(_expect(value, wire, WIRE_64BIT, "MarketFullFeed.tbq"))
        elif field == 10:
            out["total_sell_qty"] = _double(_expect(value, wire, WIRE_64BIT, "MarketFullFeed.tsq"))
    return out


def decode_index_full_feed(buf: bytes) -> dict:
    """``IndexFullFeed``: ltpc 1, marketOHLC 2 (an index has no volume or depth)."""
    out: dict = {"ltpc": None, "ohlc": []}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            out["ltpc"] = decode_ltpc(_expect(value, wire, WIRE_LENGTH, "IndexFullFeed.ltpc"))
        elif field == 2:
            out["ohlc"] = decode_market_ohlc(
                _expect(value, wire, WIRE_LENGTH, "IndexFullFeed.marketOHLC"))
    return out


def decode_full_feed(buf: bytes) -> dict:
    """``FullFeed`` oneof: marketFF 1, indexFF 2."""
    out: dict = {"market_ff": None, "index_ff": None}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            out["market_ff"] = decode_market_full_feed(
                _expect(value, wire, WIRE_LENGTH, "FullFeed.marketFF"))
        elif field == 2:
            out["index_ff"] = decode_index_full_feed(
                _expect(value, wire, WIRE_LENGTH, "FullFeed.indexFF"))
    return out


def decode_first_level_with_greeks(buf: bytes) -> dict:
    """``FirstLevelWithGreeks``: ltpc 1, firstDepth 2, optionGreeks 3, vtt 4,
    oi 5, iv 6 (the ``option_greeks`` subscription mode)."""
    out: dict = {"ltpc": None, "depth": [], "greeks": None, "volume": None,
                 "oi": None, "iv": None}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            out["ltpc"] = decode_ltpc(
                _expect(value, wire, WIRE_LENGTH, "FirstLevelWithGreeks.ltpc"))
        elif field == 2:
            out["depth"] = [decode_quote(
                _expect(value, wire, WIRE_LENGTH, "FirstLevelWithGreeks.firstDepth"))]
        elif field == 3:
            out["greeks"] = decode_option_greeks(
                _expect(value, wire, WIRE_LENGTH, "FirstLevelWithGreeks.optionGreeks"))
        elif field == 4:
            out["volume"] = _expect(value, wire, WIRE_VARINT, "FirstLevelWithGreeks.vtt")
        elif field == 5:
            out["oi"] = _double(_expect(value, wire, WIRE_64BIT, "FirstLevelWithGreeks.oi"))
        elif field == 6:
            out["iv"] = _double(_expect(value, wire, WIRE_64BIT, "FirstLevelWithGreeks.iv"))
    return out


def decode_feed(buf: bytes) -> dict:
    """``Feed`` oneof: ltpc 1, fullFeed 2, firstLevelWithGreeks 3, requestMode 4."""
    out: dict = {"mode": None, "ltpc": None, "market_ff": None, "index_ff": None,
                 "first_level": None, "request_mode": None}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            out["ltpc"] = decode_ltpc(_expect(value, wire, WIRE_LENGTH, "Feed.ltpc"))
            out["mode"] = "ltpc"
        elif field == 2:
            full = decode_full_feed(_expect(value, wire, WIRE_LENGTH, "Feed.fullFeed"))
            out["market_ff"] = full["market_ff"]
            out["index_ff"] = full["index_ff"]
            out["mode"] = "full"
        elif field == 3:
            out["first_level"] = decode_first_level_with_greeks(
                _expect(value, wire, WIRE_LENGTH, "Feed.firstLevelWithGreeks"))
            out["mode"] = "option_greeks"
        elif field == 4:
            code = _expect(value, wire, WIRE_VARINT, "Feed.requestMode")
            out["request_mode"] = REQUEST_MODE_NAMES.get(code, f"unknown_{code}")
    return out


def _decode_map_entry(entry: bytes, value_decoder) -> tuple[str | None, object]:
    """Decode **one** ``map<string, V>`` entry: key at field 1, value at field 2.

    Protobuf encodes a map as repeated entries, so each occurrence of the map field in the
    parent message is a single entry - not a container.  Reading one extra nesting level
    here is exactly the bug that makes every full-feed frame look like a malformed LTPC.
    """
    key, val = None, None
    for efield, ewire, evalue in iter_fields(entry):
        if efield == 1:
            key = _text(_expect(evalue, ewire, WIRE_LENGTH, "map key"))
        elif efield == 2:
            val = value_decoder(evalue, ewire)
    return key, val


def decode_market_info(buf: bytes) -> dict:
    """``MarketInfo``: ``map<string, MarketStatus> segmentStatus`` at field 1."""
    def status(evalue, ewire):
        return MARKET_STATUS_NAMES.get(_expect(evalue, ewire, WIRE_VARINT, "MarketStatus"),
                                       f"unknown_{evalue}")

    segments: dict = {}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            entry = _expect(value, wire, WIRE_LENGTH, "segmentStatus entry")
            name, state = _decode_map_entry(entry, status)
            if name is not None:
                segments[name] = state
    return {"segment_status": segments}


def decode_feed_response(buf: bytes) -> dict:
    """``FeedResponse``: type 1, map<string, Feed> feeds 2, currentTs 3, marketInfo 4."""
    def feed(evalue, ewire):
        return decode_feed(_expect(evalue, ewire, WIRE_LENGTH, "feeds value"))

    out: dict = {"type": None, "type_code": None, "feeds": {}, "current_ts": None,
                 "market_info": None}
    for field, wire, value in iter_fields(buf):
        if field == 1:
            code = _expect(value, wire, WIRE_VARINT, "FeedResponse.type")
            out["type_code"] = code
            out["type"] = FEED_TYPE_NAMES.get(code, f"unknown_{code}")
        elif field == 2:
            entry = _expect(value, wire, WIRE_LENGTH, "feeds entry")
            name, decoded = _decode_map_entry(entry, feed)
            if name is not None:
                out["feeds"][name] = decoded
        elif field == 3:
            out["current_ts"] = _expect(value, wire, WIRE_VARINT, "FeedResponse.currentTs")
        elif field == 4:
            out["market_info"] = decode_market_info(
                _expect(value, wire, WIRE_LENGTH, "FeedResponse.marketInfo"))
    return out


def is_market_info_frame(buf: bytes) -> bool:
    """True when a frame is the feed's own market-status message (no instruments)."""
    try:
        return decode_feed_response(buf)["type_code"] == FEED_TYPE_MARKET_INFO
    except ProtobufError:
        return False
