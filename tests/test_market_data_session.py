"""Live market data: session windows, trading-day determination and stop conditions.

Each test pins the clock, so "is the market open?" is decided by the code under test
rather than by when the suite happens to run.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nifty_quant.market_data.config import MarketDataConfig
from nifty_quant.market_data.providers.base import MarketPhase, MarketStatusSnapshot
from nifty_quant.market_data.session import MarketSessionManager, SessionWindow

IST = ZoneInfo("Asia/Kolkata")


def at(year=2030, month=1, day=3, hour=10, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


def manager(clock: datetime, *, trading_days=None, status=None, status_error=None,
            source: str = "test-calendar") -> MarketSessionManager:
    def provider():
        if status_error is not None:
            raise status_error
        return status

    return MarketSessionManager(
        MarketDataConfig(), status_provider=provider,
        trading_days=set(trading_days or ()), trading_days_source=source,
        clock=lambda: clock)


def status(phase: MarketPhase, *, estimated: bool = False,
           raw: str = "NORMAL_OPEN") -> MarketStatusSnapshot:
    return MarketStatusSnapshot(exchange="NSE", phase=phase, raw_status=raw,
                                source="provider", estimated=estimated)


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
def test_window_uses_the_india_session():
    window = manager(at()).window()
    assert isinstance(window, SessionWindow)
    assert window.opens_at.strftime("%H:%M") == "09:15"
    assert window.closes_at.strftime("%H:%M") == "15:30"


def test_window_membership_is_inclusive_of_both_edges():
    session = manager(at(hour=9, minute=15))
    assert session.is_within_session(at(hour=9, minute=15)) is True
    assert session.is_within_session(at(hour=15, minute=30)) is True
    assert session.is_within_session(at(hour=9, minute=14)) is False


def test_seconds_until_open_is_zero_once_open():
    session = manager(at(hour=9, minute=0))
    assert session.seconds_until_open() == pytest.approx(15 * 60)
    assert session.seconds_until_open(at(hour=10)) == 0.0


# ---------------------------------------------------------------------------
# Trading days
# ---------------------------------------------------------------------------
def test_provider_status_is_authoritative_for_today():
    session = manager(at(), status=status(MarketPhase.OPEN))
    trading, reason, estimated = session.is_trading_day()
    assert trading is True
    assert estimated is False
    assert "NORMAL_OPEN" in reason


def test_locally_inferred_status_is_never_treated_as_authoritative():
    session = manager(at(), status=status(MarketPhase.OPEN, estimated=True))
    trading, reason, estimated = session.is_trading_day()
    assert estimated is True
    assert "weekday" in reason or "reference" in reason


def test_a_listed_session_is_a_trading_day():
    session = manager(at(), status=status(MarketPhase.UNKNOWN), trading_days={"2030-01-03"})
    trading, reason, estimated = session.is_trading_day()
    assert trading is True
    assert estimated is False
    assert "test-calendar" in reason


def test_an_absent_weekday_inside_the_reference_span_is_not_a_trading_day():
    session = manager(at(day=4), status=status(MarketPhase.UNKNOWN),
                      trading_days={"2030-01-03", "2030-01-07"})
    trading, reason, estimated = session.is_trading_day()
    assert trading is False
    assert estimated is False
    assert "absent" in reason


def test_outside_the_reference_span_the_weekday_is_used_and_flagged_estimated():
    session = manager(at(day=7), status=status(MarketPhase.UNKNOWN),
                      trading_days={"2030-01-03"})
    monday = date(2030, 1, 7)
    assert monday.weekday() == 0
    trading, reason, estimated = session.is_trading_day()
    assert trading is True
    assert estimated is True

    saturday = manager(at(day=5), status=status(MarketPhase.UNKNOWN),
                       trading_days={"2030-01-03"})
    assert date(2030, 1, 5).weekday() == 5
    trading, reason, estimated = saturday.is_trading_day()
    assert trading is False
    assert estimated is True


def test_a_failing_status_endpoint_falls_back_to_the_local_clock_marked_estimated():
    session = manager(at(hour=10), status_error=OSError("no network"))
    snapshot = session.status()
    assert snapshot.estimated is True
    assert snapshot.phase is MarketPhase.OPEN
    assert "local clock" in snapshot.raw_status.lower() or "LOCAL" in snapshot.raw_status


def test_today_uses_the_provider_but_other_dates_do_not():
    session = manager(at(), status=status(MarketPhase.OPEN), trading_days=set())
    today = session.is_trading_day(date(2030, 1, 3))
    assert today[0] is True and today[2] is False      # the provider answered for today

    # With no calendar loaded, any other date is only a weekday guess.
    past = session.is_trading_day(date(2029, 12, 31))
    assert past[2] is True
    assert date(2029, 12, 31).weekday() == 0
    assert past[0] is True


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------
def test_a_non_trading_day_stops_immediately():
    session = manager(at(day=5), status=status(MarketPhase.UNKNOWN),
                      trading_days={"2030-01-03", "2030-01-07"})
    stop, reason = session.should_stop()
    assert stop is True
    assert "not a trading day" in reason


def test_a_running_session_stops_after_the_close_plus_grace():
    session = manager(at(hour=15, minute=40), status=status(MarketPhase.CLOSED),
                      trading_days={"2030-01-03"})
    stop, reason = session.should_stop()
    assert stop is True
    assert "session closed" in reason


def test_a_session_inside_the_grace_window_keeps_running():
    session = manager(at(hour=15, minute=32), status=status(MarketPhase.CLOSED),
                      trading_days={"2030-01-03"})
    stop, reason = session.should_stop()
    assert stop is False
    assert reason == ""


def test_waiting_for_open_is_bounded():
    session = manager(at(hour=8, minute=0), status=status(MarketPhase.PRE_OPEN),
                      trading_days={"2030-01-03"})
    result = session.wait_for_open(poll_seconds=600, max_wait_seconds=60,
                                   sleeper=lambda _s: None)
    assert result["opened"] is False
    assert "max wait" in result["reason"]


def test_waiting_for_open_returns_immediately_when_the_session_is_open():
    session = manager(at(hour=10), status=status(MarketPhase.OPEN),
                      trading_days={"2030-01-03"})
    calls: list[float] = []
    result = session.wait_for_open(sleeper=calls.append)
    assert result["opened"] is True
    assert calls == []
