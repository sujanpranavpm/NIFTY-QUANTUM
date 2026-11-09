"""Trading-session management in Asia/Kolkata.

Two independent questions, answered from different evidence:

*Is the exchange open right now?*
    The provider's market-status endpoint is authoritative.  A local clock comparison is
    only a fallback, and it is flagged ``estimated=True`` when used - the engine never
    presents "it is 10:30 on a Wednesday" as "the market is open".

*Is today a trading day?*
    Not every weekday is.  When the provider status is unavailable the repository's own
    2024-onward session list (``data/reference/nifty_trading_days.csv``, derived from
    official bhavcopy presence) is consulted; outside its span the answer is honestly
    ``UNKNOWN`` rather than a weekday guess.

The manager never sleeps through a closed market without saying so: :meth:`should_stop`
is what the collector loop consults, and :meth:`wait_for_open` is bounded and reports how
long it waited.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable

from nifty_quant.market_data.providers.base import MarketPhase, MarketStatusSnapshot

try:  # pragma: no cover - tz database presence
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    IST = timezone.utc

#: Default NSE derivatives session (09:15-15:30 IST).
DEFAULT_OPEN = dtime(9, 15)
DEFAULT_CLOSE = dtime(15, 30)

TRADING_DAY_SOURCES = ("data/reference/nifty_trading_days.csv",)


def now_ist(moment: datetime | None = None) -> datetime:
    """``moment`` (or now) as a tz-aware Asia/Kolkata timestamp."""
    if moment is None:
        return datetime.now(timezone.utc).astimezone(IST)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


@dataclass(frozen=True)
class SessionWindow:
    """One trading day's window in IST."""

    trading_day: date
    opens_at: datetime
    closes_at: datetime

    def contains(self, moment: datetime) -> bool:
        return self.opens_at <= now_ist(moment) <= self.closes_at

    def seconds_until_open(self, moment: datetime) -> float:
        return max(0.0, (self.opens_at - now_ist(moment)).total_seconds())

    def seconds_until_close(self, moment: datetime) -> float:
        return max(0.0, (self.closes_at - now_ist(moment)).total_seconds())

    def as_dict(self) -> dict:
        return {
            "trading_day": self.trading_day.isoformat(),
            "opens_at_ist": self.opens_at.isoformat(timespec="seconds"),
            "closes_at_ist": self.closes_at.isoformat(timespec="seconds"),
        }


def load_trading_days(paths=TRADING_DAY_SOURCES, root: Path | None = None) -> tuple[set[str], str]:
    """Read the repository's session list, returning ``(iso_dates, provenance)``.

    The file is a derived reference (a trading day is one on which NSE published an F&O
    bhavcopy).  Its absence is not an error: the caller falls back to the provider's
    status and reports the reduced certainty.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    for relative in paths:
        candidate = root / relative
        if not candidate.is_file():
            continue
        days: set[str] = set()
        with candidate.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                value = (row.get("date") or "").strip()
                if len(value) >= 10:
                    days.add(value[:10])
        if days:
            return days, f"{candidate.name} ({len(days)} sessions)"
    return set(), "no local trading-day reference"


class MarketSessionManager:
    """Answers "may we collect now?" from authoritative status plus a local fallback."""

    def __init__(self, config, *, status_provider: Callable[[], MarketStatusSnapshot] | None = None,
                 trading_days: set[str] | None = None,
                 trading_days_source: str = "",
                 session_open: dtime = DEFAULT_OPEN,
                 session_close: dtime = DEFAULT_CLOSE,
                 status_cache_seconds: float = 60.0,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.config = config
        self.session_open = session_open
        self.session_close = session_close
        self._status_provider = status_provider
        self._cache_seconds = status_cache_seconds
        self._cached: tuple[float, MarketStatusSnapshot] | None = None
        self._monotonic = time.monotonic
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if trading_days is None:
            trading_days, source = load_trading_days()
            trading_days_source = trading_days_source or source
        self.trading_days = trading_days or set()
        self.trading_days_source = trading_days_source

    # -- clock -------------------------------------------------------------
    def now(self) -> datetime:
        return now_ist(self._clock())

    def window(self, day: date | None = None) -> SessionWindow:
        target = day or self.now().date()
        return SessionWindow(
            trading_day=target,
            opens_at=datetime.combine(target, self.session_open, tzinfo=IST),
            closes_at=datetime.combine(target, self.session_close, tzinfo=IST),
        )

    # -- status ------------------------------------------------------------
    def status(self) -> MarketStatusSnapshot:
        """Authoritative status, cached briefly; a failing provider yields UNKNOWN."""
        if self._status_provider is None:
            return self._estimated_status("no status endpoint configured")
        if self._cached is not None and (self._monotonic() - self._cached[0]) < self._cache_seconds:
            return self._cached[1]
        try:
            snapshot = self._status_provider()
        except Exception as exc:  # noqa: BLE001 - status failure must not crash collection
            snapshot = self._estimated_status(
                f"provider status unavailable ({type(exc).__name__}); using session clock")
        self._cached = (self._monotonic(), snapshot)
        return snapshot

    def _estimated_status(self, reason: str) -> MarketStatusSnapshot:
        window = self.window()
        moment = self.now()
        if window.contains(moment):
            phase = MarketPhase.OPEN
        elif moment < window.opens_at:
            phase = MarketPhase.PRE_OPEN
        else:
            phase = MarketPhase.CLOSED
        return MarketStatusSnapshot(exchange=self.config.exchange, phase=phase,
                                    raw_status="LOCAL_CLOCK", observed_at=moment,
                                    source=reason, estimated=True)

    # -- trading-day determination ----------------------------------------
    def is_trading_day(self, day: date | None = None) -> tuple[bool, str, bool]:
        """``(is_trading_day, reason, estimated)``.

        Order of authority: provider status for *today*, then the local session list, then
        a weekday check that is explicitly marked estimated.
        """
        target = day or self.now().date()
        snapshot = self.status()
        if (day is None or target == self.now().date()) and not snapshot.estimated:
            # Only an exchange-reported status may answer this, and only for today: a
            # locally inferred phase says nothing about whether the exchange is trading.
            if snapshot.phase is not MarketPhase.UNKNOWN:
                return True, f"provider status {snapshot.raw_status} ({snapshot.source})", False
        iso = target.isoformat()
        if self.trading_days:
            if iso in self.trading_days:
                return True, f"listed in {self.trading_days_source}", False
            span = (min(self.trading_days), max(self.trading_days))
            if span[0] <= iso <= span[1]:
                return False, (f"absent from {self.trading_days_source}, which covers "
                               f"{span[0]}..{span[1]}"), False
            return (target.weekday() < 5,
                    f"outside the {span[0]}..{span[1]} reference span; weekday "
                    f"assumption only", True)
        return (target.weekday() < 5,
                "no trading-day reference and no provider status; weekday assumption only",
                True)

    # -- control -----------------------------------------------------------
    def is_within_session(self, moment: datetime | None = None) -> bool:
        return self.window().contains(moment or self.now())

    def seconds_until_open(self, moment: datetime | None = None) -> float:
        return self.window().seconds_until_open(moment or self.now())

    def should_stop(self, moment: datetime | None = None,
                    grace_minutes: float = 5.0) -> tuple[bool, str]:
        """Should an in-progress session close itself?

        A collector started after the close (or on a non-trading day) stops immediately;
        one still running past the close plus the grace window stops too, so an
        unattended process cannot hold an idle socket overnight.
        """
        current = now_ist(moment or self.now())
        trading, reason, estimated = self.is_trading_day(current.date())
        if not trading:
            return True, f"not a trading day: {reason}" + (" (estimated)" if estimated else "")
        window = self.window(current.date())
        if current > window.closes_at + timedelta(minutes=grace_minutes):
            return True, f"session closed at {window.closes_at.strftime('%H:%M')} IST"
        return False, ""

    def wait_for_open(self, *, poll_seconds: float = 60.0, max_wait_seconds: float | None = None,
                      sleeper: Callable[[float], None] = time.sleep,
                      should_stop: Callable[[], bool] | None = None) -> dict:
        """Block until the session is open, bounded by ``max_wait_seconds``."""
        waited = 0.0
        polls = 0
        while True:
            if should_stop is not None and should_stop():
                return {"opened": False, "waited_seconds": waited, "polls": polls,
                        "reason": "stop requested"}
            if self.is_within_session():
                return {"opened": True, "waited_seconds": waited, "polls": polls,
                        "reason": "session open"}
            remaining = self.seconds_until_open()
            if remaining <= 0:
                return {"opened": True, "waited_seconds": waited, "polls": polls,
                        "reason": "session open"}
            if max_wait_seconds is not None and waited + min(poll_seconds, remaining) > max_wait_seconds:
                return {"opened": False, "waited_seconds": waited, "polls": polls,
                        "reason": f"max wait {max_wait_seconds}s reached "
                                  f"({remaining:.0f}s until open)"}
            pause = min(poll_seconds, remaining)
            sleeper(pause)
            waited += pause
            polls += 1
