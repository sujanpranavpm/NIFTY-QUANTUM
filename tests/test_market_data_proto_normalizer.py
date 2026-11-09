"""Live market data: protobuf decoding and tick normalisation.

The encoder in ``_test_support`` is an independent implementation of the provider's
published schema, so a round-trip test would fail if either side used the wrong field
number instead of agreeing with itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nifty_quant.market_data import _test_support as T
from nifty_quant.market_data import proto
from nifty_quant.market_data.instruments import InstrumentUniverse
from nifty_quant.market_data.normalizer import (
    ABNORMAL_MOVE,
    CLEAN,
    DUPLICATE,
    MISSING_PRICE,
    MISSING_TIMESTAMP,
    OUT_OF_ORDER,
    OUT_OF_SESSION,
    STALE,
    UNKNOWN_INSTRUMENT,
    TickNormalizer,
)
from nifty_quant.market_data.providers.base import InstrumentKind, SubscriptionMode

IST = ZoneInfo("Asia/Kolkata")
SESSION_TIME = datetime(2030, 1, 3, 10, 0, tzinfo=IST)


def at(minute: int = 0, second: int = 0, hour: int = 10) -> datetime:
    return datetime(2030, 1, 3, hour, minute, second, tzinfo=IST)


OPTION_KEY = "NSE_FO|25000CE"
INDEX_KEY = "NSE_INDEX|Nifty 50"


def option_sub():
    return T.subscription(OPTION_KEY, InstrumentKind.OPTION, symbol="NIFTY 03 JAN 30 25000 CE",
                          expiry="2030-01-03", strike=25000, option_type="CE")


def index_sub(mode=SubscriptionMode.LTPC):
    return T.subscription(INDEX_KEY, InstrumentKind.INDEX, mode=mode,
                          symbol="Nifty 50", underlying="NIFTY", strike=None,
                          option_type="")


def universe(*subs):
    return InstrumentUniverse(provider="fake", generated_at="2030-01-03T04:30:00",
                              subscriptions=tuple(subs or (option_sub(),)))


def full_frame(ltp=100.0, ltt=None, ltq=75, cp=99.0, *, vtt=1000, oi=5000.0,
               iv=12.0, depth=None, greeks=True, key=OPTION_KEY) -> bytes:
    stamp = T.ms(ltt or SESSION_TIME)
    market = T.market_full_feed(
        ltp, stamp, ltq, cp, vtt=vtt, oi=oi, iv=iv,
        depth=depth if depth is not None else [(75, 99.5, 150, 100.5)],
        greeks=T.option_greeks() if greeks else None)
    return T.feed_response({key: T.feed_full(market)}, current_ts=stamp)


def index_frame(ltp=25010.5, ltt=None) -> bytes:
    stamp = T.ms(ltt or SESSION_TIME)
    return T.feed_response(
        {INDEX_KEY: T.feed_index(T.index_full_feed(ltp, stamp, 0, 25000.0))},
        current_ts=stamp)


def normalizer(*subs, **kwargs) -> TickNormalizer:
    return TickNormalizer(universe(*subs), **kwargs)


# ---------------------------------------------------------------------------
# Protobuf decoding
# ---------------------------------------------------------------------------
def test_full_feed_decodes_price_depth_volume_oi_and_greeks():
    frame = proto.decode_feed_response(full_frame())
    assert frame["type"] == "live_feed"
    assert frame["current_ts"] == T.ms(SESSION_TIME)
    feed = frame["feeds"][OPTION_KEY]
    assert feed["mode"] == "full"
    market = feed["market_ff"]
    assert market["ltpc"]["ltp"] == pytest.approx(100.0)
    assert market["ltpc"]["ltt"] == T.ms(SESSION_TIME)
    assert market["ltpc"]["ltq"] == 75
    assert market["volume"] == 1000
    assert market["oi"] == pytest.approx(5000.0)
    assert market["depth"][0]["bid_price"] == pytest.approx(99.5)
    assert market["depth"][0]["ask_qty"] == 150
    assert market["greeks"]["delta"] == pytest.approx(0.5)


def test_index_feed_decodes_without_volume_or_depth():
    frame = proto.decode_feed_response(index_frame())
    feed = frame["feeds"][INDEX_KEY]
    assert feed["index_ff"]["ltpc"]["ltp"] == pytest.approx(25010.5)
    assert feed["market_ff"] is None


def test_ltpc_only_feed_decodes():
    stamp = T.ms(SESSION_TIME)
    frame = proto.decode_feed_response(
        T.feed_response({INDEX_KEY: T.feed_ltpc(T.ltpc(25000.0, stamp, 0, 24990.0))}))
    feed = frame["feeds"][INDEX_KEY]
    assert feed["mode"] == "ltpc"
    assert feed["ltpc"]["cp"] == pytest.approx(24990.0)


def test_market_info_frame_reports_segment_status():
    frame = proto.decode_feed_response(T.market_info_frame({"NSE_EQ": 2, "NSE_FO": 3}))
    assert frame["type"] == "market_info"
    assert frame["market_info"]["segment_status"] == {"NSE_EQ": "NORMAL_OPEN",
                                                      "NSE_FO": "NORMAL_CLOSE"}
    assert frame["feeds"] == {}


def test_malformed_frame_is_an_error_not_an_empty_result():
    with pytest.raises(proto.ProtobufError):
        proto.decode_feed_response(b"\xff\xff\xff")


def test_truncated_length_delimited_field_is_refused():
    good = full_frame()
    with pytest.raises(proto.ProtobufError):
        proto.decode_feed_response(good[:-8])


def test_unknown_fields_are_ignored():
    extended = full_frame() + T.f_varint(99, 1) + T.f_string(120, "future")
    frame = proto.decode_feed_response(extended)
    assert frame["feeds"][OPTION_KEY]["market_ff"]["ltpc"]["ltp"] == pytest.approx(100.0)


def test_negative_int64_is_decoded_as_signed():
    # A provider clock skew can produce a negative ltt; it must not become 2**64.
    stamp = -1000
    payload = T.feed_response({OPTION_KEY: T.feed_ltpc(T.ltpc(1.0, stamp, 0, 1.0))})
    assert proto.decode_feed_response(payload)["feeds"][OPTION_KEY]["ltpc"]["ltt"] == -1000


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def test_clean_tick_carries_the_full_contract_identity_and_quote():
    norm = normalizer()
    ticks = norm.normalize_frame(proto.decode_feed_response(full_frame()),
                                 received_at=SESSION_TIME)
    tick = ticks[0]
    assert tick.quality == CLEAN
    assert tick.instrument_key == OPTION_KEY
    assert tick.kind == "option"
    assert tick.strike == 25000
    assert tick.option_type == "CE"
    assert tick.expiry == "2030-01-03"
    assert tick.ltp == pytest.approx(100.0)
    assert tick.bid_price == pytest.approx(99.5)
    assert tick.volume == 1000                 # cumulative, as delivered
    assert tick.open_interest == pytest.approx(5000.0)
    assert tick.delta == pytest.approx(0.5)
    assert tick.usable is True


def test_duplicate_update_is_flagged_and_not_usable():
    norm = normalizer()
    frame = proto.decode_feed_response(full_frame())
    first = norm.normalize_frame(frame, received_at=SESSION_TIME)[0]
    second = norm.normalize_frame(frame, received_at=SESSION_TIME)[0]
    assert first.quality == CLEAN
    assert second.quality == DUPLICATE
    assert second.usable is False


def test_out_of_order_update_is_flagged():
    norm = normalizer()
    later = SESSION_TIME + timedelta(seconds=30)
    norm.normalize_frame(proto.decode_feed_response(full_frame(ltp=101.0, ltt=later)),
                         received_at=later)
    earlier = SESSION_TIME
    tick = norm.normalize_frame(
        proto.decode_feed_response(full_frame(ltp=100.5, ltt=earlier)),
        received_at=SESSION_TIME + timedelta(seconds=30))[0]
    assert tick.quality == OUT_OF_ORDER


def test_stale_update_is_flagged_when_the_exchange_time_lags():
    norm = normalizer(stale_after_seconds=30)
    tick = norm.normalize_frame(
        proto.decode_feed_response(full_frame()),
        received_at=SESSION_TIME + timedelta(minutes=5))[0]
    assert tick.quality == STALE


def test_abnormal_move_is_flagged_but_still_usable():
    norm = normalizer(abnormal_move_pct=0.25)
    norm.normalize_frame(proto.decode_feed_response(full_frame(ltp=100.0)),
                         received_at=SESSION_TIME)
    later = SESSION_TIME + timedelta(seconds=5)
    tick = norm.normalize_frame(
        proto.decode_feed_response(full_frame(ltp=180.0, ltt=later)),
        received_at=later)[0]
    assert tick.quality == ABNORMAL_MOVE
    assert tick.usable is True


def test_missing_exchange_timestamp_is_flagged():
    payload = T.feed_response({OPTION_KEY: T.feed_ltpc(T.ltpc(100.0, None, 75, 99.0))})
    tick = normalizer().normalize_frame(proto.decode_feed_response(payload),
                                        received_at=SESSION_TIME)[0]
    assert tick.quality == MISSING_TIMESTAMP
    assert tick.exchange_ts_ist is None


def test_price_less_option_update_is_flagged():
    payload = T.feed_response({OPTION_KEY: T.feed_ltpc(T.ltpc(None, T.ms(SESSION_TIME), 0, 99.0))})
    tick = normalizer().normalize_frame(proto.decode_feed_response(payload),
                                        received_at=SESSION_TIME)[0]
    assert tick.quality == MISSING_PRICE


def test_price_less_index_update_is_acceptable():
    # An index has no traded price of its own; a timestamp+counter update is legitimate.
    payload = T.feed_response(
        {INDEX_KEY: T.feed_ltpc(T.ltpc(None, T.ms(SESSION_TIME), 0, 25000.0))})
    tick = normalizer(index_sub()).normalize_frame(proto.decode_feed_response(payload),
                                                   received_at=SESSION_TIME)[0]
    assert tick.quality == CLEAN
    assert tick.ltp is None


def test_feeds_for_unsubscribed_instruments_are_flagged_not_dropped():
    norm = normalizer()
    frame = proto.decode_feed_response(full_frame(key="NSE_FO|NOT_SUBSCRIBED"))
    ticks = norm.normalize_frame(frame, received_at=SESSION_TIME)
    assert len(ticks) == 1
    assert ticks[0].quality == UNKNOWN_INSTRUMENT
    assert ticks[0].usable is False


def test_print_outside_the_session_is_flagged():
    pre_open = at(hour=8, minute=0)
    tick = normalizer().normalize_frame(
        proto.decode_feed_response(full_frame(ltt=pre_open)), received_at=pre_open)[0]
    assert tick.quality == OUT_OF_SESSION


def test_previous_open_interest_is_taken_from_the_chain_snapshot():
    norm = normalizer()
    norm.set_previous_open_interest(OPTION_KEY, 4800.0)
    tick = norm.normalize_frame(proto.decode_feed_response(full_frame()),
                                received_at=SESSION_TIME)[0]
    assert tick.open_interest_previous == pytest.approx(4800.0)


def test_quality_counts_are_accumulated():
    norm = normalizer()
    frame = proto.decode_feed_response(full_frame())
    norm.normalize_frame(frame, received_at=SESSION_TIME)
    norm.normalize_frame(frame, received_at=SESSION_TIME)
    assert norm.counts() == {CLEAN: 1, DUPLICATE: 1}


def test_tick_record_is_flat_and_names_the_provider():
    tick = normalizer().normalize_frame(proto.decode_feed_response(full_frame()),
                                        received_at=SESSION_TIME)[0]
    record = tick.to_record()
    assert record["instrument_key"] == OPTION_KEY
    assert record["volume_cumulative"] == 1000
    assert record["depth_levels"] == 1
    assert record["quality"] == CLEAN
    assert "source_provider" in record
