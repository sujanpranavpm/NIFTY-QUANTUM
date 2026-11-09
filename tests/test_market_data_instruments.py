"""Live market data: instrument discovery, option-chain parsing and subscription limits.

Fixtures are synthetic and dated far in the future, so nothing here depends on the wall
clock or on a live provider.
"""

from __future__ import annotations

import pandas as pd
import pytest

from nifty_quant.market_data import _test_support as T
from nifty_quant.market_data.config import MarketDataConfig
from nifty_quant.market_data.instruments import (
    DiscoveryError,
    atm_strike,
    discover_universe,
    find_futures,
    find_index_key,
    normalise_expiry,
    select_strikes,
)
from nifty_quant.market_data.providers.base import (
    InstrumentKind,
    ModeLimit,
    ProviderCapabilities,
    Subscription,
    SubscriptionLimitError,
    SubscriptionMode,
    enforce_subscription_limits,
)
from nifty_quant.market_data.providers.upstox import parse_option_chain


def config(**overrides) -> MarketDataConfig:
    defaults = dict(strikes_above_atm=2, strikes_below_atm=2, data_root="data/live")
    defaults.update(overrides)
    return MarketDataConfig(**defaults)


def provider_with_chain(expiry: str = T.FUTURE_EXPIRY_ISO) -> T.FakeProvider:
    return T.FakeProvider(
        master=T.index_master_rows(),
        expiries=[expiry],
        chains={expiry: T.option_chain_frame(expiry=expiry)},
    )


# ---------------------------------------------------------------------------
# Expiry and identity
# ---------------------------------------------------------------------------
def test_expiry_is_read_from_epoch_milliseconds():
    assert normalise_expiry(1893628800000) == "2030-01-03"


def test_expiry_is_read_from_a_date_string_or_object():
    import datetime as dt

    assert normalise_expiry("2030-01-03") == "2030-01-03"
    assert normalise_expiry("2030-01-03T00:00:00+05:30") == "2030-01-03"
    assert normalise_expiry(dt.date(2030, 1, 3)) == "2030-01-03"
    assert normalise_expiry(dt.datetime(2030, 1, 3, 15, 30)) == "2030-01-03"


def test_unparsable_expiry_is_refused():
    with pytest.raises(DiscoveryError):
        normalise_expiry("not-a-date")


# ---------------------------------------------------------------------------
# Discovery from the instrument master
# ---------------------------------------------------------------------------
def test_index_keys_come_from_the_master():
    master = T.index_master_rows()
    assert find_index_key(master, "NIFTY 50") == "NSE_INDEX|Nifty 50"
    assert find_index_key(master, "INDIA VIX") == "NSE_INDEX|India VIX"


def test_unknown_index_is_reported_not_guessed():
    with pytest.raises(DiscoveryError):
        find_index_key(T.index_master_rows(), "BANKNIFTY")


def test_expired_futures_are_filtered_out():
    rows = find_futures(T.index_master_rows(), "NIFTY")
    keys = [row["instrument_key"] for row in rows]
    assert keys == ["NSE_FO|FUT-JAN30"]
    assert all(row["expiry_iso"] >= "2026-01-01" for row in rows)


# ---------------------------------------------------------------------------
# Option chain parsing
# ---------------------------------------------------------------------------
def provider_chain_entries() -> list[dict]:
    return [
        {
            "expiry": T.FUTURE_EXPIRY_ISO,
            "strike_price": 25000,
            "underlying_spot_price": 25010.5,
            "call_options": {
                "instrument_key": "NSE_FO|CE25000",
                "market_data": {"ltp": 120.5, "oi": 5000, "prev_oi": 4800, "volume": 900,
                                "bid_price": 120.0, "bid_qty": 75, "ask_price": 121.0,
                                "ask_qty": 150},
                "option_greeks": {"delta": 0.52, "theta": -3.1, "gamma": 0.002,
                                  "vega": 11.5, "rho": 0.4, "iv": 12.5},
            },
            "put_options": {
                "instrument_key": "NSE_FO|PE25000",
                "market_data": {"ltp": 95.0, "oi": 6000},
                "option_greeks": {"delta": -0.48, "iv": 13.0},
            },
        }
    ]


def test_option_chain_keeps_both_legs_and_their_provider_keys():
    rows = parse_option_chain(provider_chain_entries())
    keys = sorted(row["instrument_key"] for row in rows)
    assert keys == ["NSE_FO|CE25000", "NSE_FO|PE25000"]
    call = next(row for row in rows if row["option_type"] == "CE")
    assert call["strike"] == 25000
    assert call["expiry"] == T.FUTURE_EXPIRY_ISO
    assert call["delta"] == pytest.approx(0.52)
    assert call["oi"] == pytest.approx(5000.0)
    assert call["underlying_spot_price"] == pytest.approx(25010.5)


def test_omitted_chain_fields_are_nan_not_zero():
    rows = parse_option_chain(provider_chain_entries())
    put = next(row for row in rows if row["option_type"] == "PE")
    assert pd.isna(put["bid_price"])       # not reported, and not 0
    assert pd.isna(put["volume"])
    assert put["oi"] == pytest.approx(6000.0)


def test_chain_entries_without_an_instrument_key_are_skipped():
    entries = provider_chain_entries()
    del entries[0]["call_options"]["instrument_key"]
    rows = parse_option_chain(entries)
    assert [row["option_type"] for row in rows] == ["PE"]


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------
def test_atm_is_the_listed_strike_nearest_the_spot():
    chain = T.option_chain_frame(strikes=range(24800, 25201, 50), spot=25026.0)
    assert atm_strike(chain, 25026.0) == 25050
    assert atm_strike(chain, 25010.0) == 25000


def test_atm_falls_back_to_open_interest_without_a_spot_price():
    chain = T.option_chain_frame()
    chain = chain.copy()
    chain.loc[chain["strike"] == 24800, "oi"] = 999999
    assert atm_strike(chain, None) == 24800


def test_strike_window_is_symmetric_around_the_money():
    strikes = [24800, 24850, 24900, 24950, 25000, 25050, 25100]
    assert select_strikes(strikes, 25000, 2, 2) == (24900, 24950, 25000, 25050, 25100)


def test_strike_window_is_not_padded_past_the_listed_grid():
    strikes = [24950, 25000, 25050]
    assert select_strikes(strikes, 25000, 5, 5) == (24950, 25000, 25050)


# ---------------------------------------------------------------------------
# Universe construction
# ---------------------------------------------------------------------------
def test_universe_contains_index_vix_future_and_option_legs():
    provider = provider_with_chain()
    universe = discover_universe(provider, config())
    kinds = {sub.kind for sub in universe.subscriptions}
    assert kinds == {InstrumentKind.INDEX, InstrumentKind.FUTURE, InstrumentKind.OPTION}
    assert "NSE_INDEX|Nifty 50" in universe.keys()
    assert "NSE_INDEX|India VIX" in universe.keys()
    assert "NSE_FO|FUT-JAN30" in universe.keys()
    assert len(universe.options) == 10          # 5 strikes x CE/PE
    assert universe.atm_strike == 25000
    assert universe.expiries == (T.FUTURE_EXPIRY_ISO,)


def test_universe_mode_is_minimal_for_indexes_and_full_for_contracts():
    universe = discover_universe(provider_with_chain(), config())
    assert universe.index.mode is SubscriptionMode.LTPC
    assert universe.vix.mode is SubscriptionMode.LTPC
    assert all(sub.mode is SubscriptionMode.FULL for sub in universe.options)


def test_universe_carries_the_previous_open_interest_from_the_chain():
    universe = discover_universe(provider_with_chain(), config())
    assert universe.previous_open_interest
    assert all(value == pytest.approx(4800.0)
               for value in universe.previous_open_interest.values())


def test_universe_reports_a_missing_master_instead_of_failing():
    provider = T.FakeProvider(master=[], expiries=[T.FUTURE_EXPIRY_ISO],
                              chains={T.FUTURE_EXPIRY_ISO: T.option_chain_frame()})

    class Raising(T.FakeProvider):
        def instrument_master(self):
            raise OSError("download failed")

    provider = Raising(expiries=[T.FUTURE_EXPIRY_ISO],
                       chains={T.FUTURE_EXPIRY_ISO: T.option_chain_frame()})
    universe = discover_universe(provider, config())
    notes = " ".join(universe.notes)
    assert "instrument master unavailable" in notes
    assert "futures skipped" in notes
    assert universe.index is not None       # documented fallback still applies
    assert not universe.futures


def test_chain_without_a_spot_price_is_reported():
    chain = T.option_chain_frame(spot=None)
    provider = T.FakeProvider(master=T.index_master_rows(),
                              expiries=[T.FUTURE_EXPIRY_ISO],
                              chains={T.FUTURE_EXPIRY_ISO: chain})
    universe = discover_universe(provider, config())
    assert any("no spot price" in note for note in universe.notes)


# ---------------------------------------------------------------------------
# Subscription limits
# ---------------------------------------------------------------------------
def caps(full_individual: int = 100, full_combined: int = 12) -> ProviderCapabilities:
    return ProviderCapabilities(
        name="fake",
        mode_limits={SubscriptionMode.LTPC: ModeLimit(100, 100),
                     SubscriptionMode.FULL: ModeLimit(full_individual, full_combined),
                     SubscriptionMode.OPTION_GREEKS: ModeLimit(100, 100),
                     SubscriptionMode.FULL_D30: ModeLimit(5, 5)})


def test_combined_limit_is_enforced_rather_than_truncated():
    subs = [T.subscription(f"NSE_FO|{i}") for i in range(13)]
    with pytest.raises(SubscriptionLimitError, match="combined"):
        enforce_subscription_limits(subs, caps())


def test_individual_limit_is_enforced():
    subs = [T.subscription(f"NSE_FO|{i}") for i in range(4)]
    with pytest.raises(SubscriptionLimitError, match="individual"):
        enforce_subscription_limits(subs, caps(full_individual=3, full_combined=100))


def test_duplicate_keys_are_refused():
    subs = [T.subscription("NSE_FO|1"), T.subscription("NSE_FO|1")]
    with pytest.raises(SubscriptionLimitError, match="duplicate"):
        enforce_subscription_limits(subs, caps())


def test_limit_report_counts_each_mode():
    subs = [
        T.subscription("NSE_FO|1"),
        T.subscription("NSE_INDEX|Nifty 50", InstrumentKind.INDEX,
                       mode=SubscriptionMode.LTPC, strike=None, option_type=""),
    ]
    report = enforce_subscription_limits(subs, caps())
    assert report["by_mode"] == {"full": 1, "ltpc": 1}


def test_option_subscription_requires_a_contract_identity():
    with pytest.raises(ValueError):
        Subscription(instrument_key="NSE_FO|1", kind=InstrumentKind.OPTION,
                     mode=SubscriptionMode.FULL, expiry="2030-01-03", strike=None,
                     option_type="CE")
    with pytest.raises(ValueError):
        Subscription(instrument_key="NSE_FO|1", kind=InstrumentKind.OPTION,
                     mode=SubscriptionMode.FULL, expiry="2030-01-03", strike=25000,
                     option_type="XX")


def test_no_subscriptions_is_a_valid_empty_report():
    report = enforce_subscription_limits([], caps())
    assert report == {"total": 0, "by_mode": {}, "modes_used": [],
                      "max_individual_limit": 0}
