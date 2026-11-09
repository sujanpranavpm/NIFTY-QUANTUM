"""Local candle aggregation: 1-minute buckets, then 5- and 15-minute candles.

What makes this non-trivial
---------------------------
* **Volume is cumulative in the feed.**  Summing ``vtt`` across updates would report the
  volume of the day once per tick.  The aggregator differences consecutive observations
  and attributes only the increment to the bucket, remembering the baseline per
  instrument.  The first observation of a session only *establishes* a baseline: volume
  traded before the collector connected is genuinely unknown, so the first bucket is
  marked ``volume_complete=False`` rather than being handed the whole day's volume.
* **A reset is a new day, not negative volume.**  If ``vtt`` goes backwards the counter
  rolled over at a session boundary; the new value is used as-is and the event is flagged.
* **A tick is not a candle.**  Buckets are keyed by the left edge of the interval
  (``[T, T+1)``), which is the convention this repository resolved for its own archive
  (``nifty_quant.derivatives.timestamps``) - so live candles and historical candles are
  labelled identically and can be concatenated without a silent one-minute shift.
* **The 15:30 print belongs to the last candle.**  It is the closing print, not a minute
  of trade, so it is folded into the final in-session bucket exactly as
  ``nifty_quant.derivatives.candles.candle_index`` does.
* **Missing intervals are reported, never filled.**  A bucket with no tick simply does
  not exist, and :meth:`CandleAggregator.gaps` counts what was expected versus observed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from typing import Iterable

import pandas as pd

#: Timeframes the engine produces locally.
TIMEFRAMES = (1, 5, 15)

#: Minutes in a trading session: 09:15..15:29 inclusive (the 15:30 print is folded in).
SESSION_MINUTES = 375

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - only when the tz database is unavailable
    IST = timezone.utc

CANDLE_ORIGIN_LOCAL = "locally_aggregated"
VOLUME_BASIS_CUMULATIVE = "cumulative_delta"
VOLUME_BASIS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar produced from observed ticks."""

    instrument_key: str
    timeframe_minutes: int
    candle_open: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    n_ticks: int
    open_interest: float | None
    open_interest_previous: float | None
    volume_basis: str
    volume_complete: bool
    is_complete: bool
    first_tick_ts_ms: int | None
    last_tick_ts_ms: int | None
    trading_symbol: str = ""
    underlying: str = ""
    kind: str = ""
    expiry: str = ""
    strike: int | None = None
    option_type: str = ""
    source_provider: str = ""
    quality_flags: tuple[str, ...] = ()

    @property
    def candle_close(self) -> datetime:
        return self.candle_open + timedelta(minutes=self.timeframe_minutes)

    def to_record(self) -> dict:
        return {
            "instrument_key": self.instrument_key,
            "trading_symbol": self.trading_symbol,
            "underlying": self.underlying,
            "kind": self.kind,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "timeframe_minutes": self.timeframe_minutes,
            "candle_open": self.candle_open,
            "candle_close": self.candle_close,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "n_ticks": self.n_ticks,
            "open_interest": self.open_interest,
            "open_interest_previous": self.open_interest_previous,
            "oi_is_last_observation": True,
            "volume_basis": self.volume_basis,
            "volume_complete": self.volume_complete,
            "is_complete": self.is_complete,
            "first_tick_ts_ms": self.first_tick_ts_ms,
            "last_tick_ts_ms": self.last_tick_ts_ms,
            "source_provider": self.source_provider,
            "candle_origin": CANDLE_ORIGIN_LOCAL,
            "quality_flags": ",".join(self.quality_flags),
        }


CANDLE_COLUMNS = (
    "instrument_key", "trading_symbol", "underlying", "kind", "expiry", "strike",
    "option_type", "timeframe_minutes", "candle_open", "candle_close", "open", "high",
    "low", "close", "volume", "n_ticks", "open_interest", "open_interest_previous",
    "oi_is_last_observation", "volume_basis", "volume_complete", "is_complete",
    "first_tick_ts_ms", "last_tick_ts_ms", "source_provider", "candle_origin",
    "quality_flags",
)


def _to_ist(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def bucket_start(moment: datetime, minutes: int = 1, *,
                 session_open: time = time(9, 15), session_close: time = time(15, 30)) -> datetime | None:
    """Left edge of the bucket ``moment`` belongs to, or ``None`` outside the session.

    A print at or after the session close is folded into the final in-session bucket (it
    is the closing print); a print before the open has no bucket at all.
    """
    if minutes < 1 or SESSION_MINUTES % minutes:
        raise ValueError(f"{minutes}-minute candles do not tile the session evenly")
    local = _to_ist(moment)
    day = local.date()
    open_at = datetime.combine(day, session_open, tzinfo=local.tzinfo)
    close_at = datetime.combine(day, session_close, tzinfo=local.tzinfo)
    if local < open_at or local > close_at:
        return None
    index = int((local - open_at).total_seconds() // 60)
    if index >= SESSION_MINUTES:
        index = SESSION_MINUTES - 1
    return open_at + timedelta(minutes=(index // minutes) * minutes)


@dataclass
class _OpenBucket:
    instrument_key: str
    start: datetime
    minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    n_ticks: int
    open_interest: float | None
    open_interest_previous: float | None
    volume_complete: bool
    first_tick_ts_ms: int | None
    last_tick_ts_ms: int | None
    identity: dict
    flags: list[str]

    def absorb(self, tick, volume_delta: float | None, volume_known: bool,
               volume_reset: bool) -> None:
        self.high = max(self.high, tick.ltp)
        self.low = min(self.low, tick.ltp)
        self.close = tick.ltp
        self.n_ticks += 1
        self.last_tick_ts_ms = tick.exchange_ts_ms or self.last_tick_ts_ms
        if tick.open_interest is not None:
            self.open_interest = tick.open_interest
            if tick.open_interest_previous is not None:
                self.open_interest_previous = tick.open_interest_previous
        if volume_delta is None:
            self.volume_complete = False
        else:
            self.volume = (0.0 if self.volume is None else self.volume) + volume_delta
        if volume_reset:
            self.flags.append("volume_counter_reset")

    def close_out(self, *, complete: bool) -> Candle:
        return Candle(
            instrument_key=self.instrument_key,
            timeframe_minutes=self.minutes,
            candle_open=self.start,
            open=self.open, high=self.high, low=self.low, close=self.close,
            volume=self.volume,
            n_ticks=self.n_ticks,
            open_interest=self.open_interest,
            open_interest_previous=self.open_interest_previous,
            volume_basis=VOLUME_BASIS_CUMULATIVE if self.volume is not None
            else VOLUME_BASIS_UNAVAILABLE,
            volume_complete=self.volume_complete,
            is_complete=complete,
            first_tick_ts_ms=self.first_tick_ts_ms,
            last_tick_ts_ms=self.last_tick_ts_ms,
            **self.identity,
            quality_flags=tuple(dict.fromkeys(self.flags)),
        )


class CandleAggregator:
    """Accumulates usable ticks into 1-minute candles and reports gaps.

    Only ticks with a usable quality (``CLEAN`` / ``ABNORMAL_MOVE``) and a last-traded
    price contribute: a repeat or a superseded tick must not move an OHLC, and a
    price-less open-interest update cannot define an open or close.
    """

    def __init__(self, minutes: int = 1, *, session_open: time = time(9, 15),
                 session_close: time = time(15, 30)) -> None:
        if minutes not in TIMEFRAMES:
            raise ValueError(f"supported timeframes are {TIMEFRAMES}, got {minutes}")
        self.minutes = minutes
        self.session_open = session_open
        self.session_close = session_close
        self._buckets: dict[str, _OpenBucket] = {}
        self._last_cumulative: dict[str, int] = {}
        self._observed: dict[str, set[datetime]] = {}
        self._sequence: dict[str, int] = {}
        self.skipped = {"not_usable": 0, "no_price": 0, "outside_session": 0}

    # -- state -------------------------------------------------------------
    def open_buckets(self) -> dict[str, datetime]:
        return {key: bucket.start for key, bucket in self._buckets.items()}

    def cumulative_baseline(self, instrument_key: str) -> int | None:
        return self._last_cumulative.get(instrument_key)

    # -- ingest ------------------------------------------------------------
    def add(self, tick) -> list[Candle]:
        """Absorb one tick, returning any candles the tick closed."""
        if not getattr(tick, "usable", False):
            self.skipped["not_usable"] += 1
            return []
        if tick.ltp is None:
            self.skipped["no_price"] += 1
            return []
        moment = (tick.exchange_ts_ist
                  or datetime.fromtimestamp(tick.local_ts_ms / 1000.0, tz=timezone.utc))
        start = bucket_start(moment, self.minutes, session_open=self.session_open,
                             session_close=self.session_close)
        if start is None:
            self.skipped["outside_session"] += 1
            return []

        volume_delta, volume_known, volume_reset = self._volume_delta(tick)
        closed: list[Candle] = []
        current = self._buckets.get(tick.instrument_key)
        if current is not None and current.start != start:
            closed.append(self._close(tick.instrument_key, complete=start > current.start))
            current = None

        if current is None:
            current = _OpenBucket(
                instrument_key=tick.instrument_key,
                start=start,
                minutes=self.minutes,
                open=tick.ltp, high=tick.ltp, low=tick.ltp, close=tick.ltp,
                volume=None, n_ticks=0,
                open_interest=tick.open_interest,
                open_interest_previous=tick.open_interest_previous,
                volume_complete=True,
                first_tick_ts_ms=tick.exchange_ts_ms,
                last_tick_ts_ms=tick.exchange_ts_ms,
                identity={
                    "trading_symbol": tick.trading_symbol,
                    "underlying": tick.underlying,
                    "kind": tick.kind,
                    "expiry": tick.expiry,
                    "strike": tick.strike,
                    "option_type": tick.option_type,
                    "source_provider": tick.source_provider,
                },
                flags=[],
            )
            self._buckets[tick.instrument_key] = current
        current.absorb(tick, volume_delta, volume_known, volume_reset)
        return closed

    def _volume_delta(self, tick) -> tuple[float | None, bool, bool]:
        """Per-update traded volume from the provider's cumulative counter.

        Returns ``(delta, known, reset)``.  ``delta`` is ``None`` when the counter has no
        baseline yet (the first observation of a session) or when the provider does not
        report volume at all - never ``0``, which would claim "nothing traded".
        """
        if tick.volume is None:
            return None, False, False
        key = tick.instrument_key
        last = self._last_cumulative.get(key)
        if last is None:
            self._last_cumulative[key] = int(tick.volume)
            return None, False, False
        if tick.volume < last:
            self._last_cumulative[key] = int(tick.volume)
            return float(tick.volume), True, True
        delta = int(tick.volume) - last
        self._last_cumulative[key] = int(tick.volume)
        return float(delta), True, False

    # -- closing -----------------------------------------------------------
    def _close(self, instrument_key: str, *, complete: bool) -> Candle:
        bucket = self._buckets.pop(instrument_key)
        self._observed.setdefault(instrument_key, set()).add(bucket.start)
        return bucket.close_out(complete=complete)

    def flush(self, now: datetime | None = None) -> list[Candle]:
        """Close every bucket whose interval has already ended."""
        moment = _to_ist(now or datetime.now(timezone.utc))
        closed: list[Candle] = []
        for key, bucket in list(self._buckets.items()):
            end = bucket.start + timedelta(minutes=self.minutes)
            if moment >= end:
                closed.append(self._close(key, complete=True))
        return closed

    def finalize(self) -> list[Candle]:
        """Close everything that is still open, marking the candles incomplete."""
        return [self._close(key, complete=False) for key in list(self._buckets)]

    # -- diagnostics -------------------------------------------------------
    def gaps(self) -> dict[str, dict]:
        """Per-instrument missing-bucket count over the observed span.

        Only the instrument's own first..last observed bucket is examined, so a contract
        that simply stopped trading is not counted as having gaps it never had.  The
        count is over *closed* buckets; the bucket still open at call time is never
        reported as a gap.
        """
        out: dict[str, dict] = {}
        for key, observed in self._observed.items():
            if not observed:
                continue
            ordered = sorted(observed)
            span = int((ordered[-1] - ordered[0]).total_seconds() // 60 // self.minutes) + 1
            missing = span - len(ordered)
            if missing > 0:
                out[key] = {
                    "observed_buckets": len(observed),
                    "expected_buckets_in_span": span,
                    "missing_buckets": missing,
                    "first_bucket": ordered[0].isoformat(),
                    "last_bucket": ordered[-1].isoformat(),
                }
        return out


def expected_buckets(minutes: int) -> int:
    """Buckets in a full session for a timeframe (for completeness reporting)."""
    if minutes < 1 or SESSION_MINUTES % minutes:
        raise ValueError(f"{minutes} does not tile the session")
    return SESSION_MINUTES // minutes


def resample(one_minute: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate locally built 1-minute candles into ``minutes``-minute candles.

    The rules are the ones this repository already uses for its archive
    (``nifty_quant.derivatives.candles``): first open, max high, min low, last close,
    summed volume, last OI observation, no gap filling.  A coagulated candle is
    ``is_complete`` only when every constituent minute is itself complete.
    """
    if minutes == 1:
        return one_minute.copy() if one_minute is not None else pd.DataFrame(columns=CANDLE_COLUMNS)
    if one_minute is None or one_minute.empty:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    if minutes not in TIMEFRAMES:
        raise ValueError(f"supported timeframes are {TIMEFRAMES}, got {minutes}")

    frame = one_minute.copy()
    for column in ("open", "high", "low", "close", "volume", "open_interest",
                   "open_interest_previous", "n_ticks"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if not pd.api.types.is_datetime64_any_dtype(frame["candle_open"]):
        frame["candle_open"] = pd.to_datetime(frame["candle_open"])
    if frame["candle_open"].dt.tz is None:
        frame["candle_open"] = frame["candle_open"].dt.tz_localize(IST)
    # Bucket every minute onto the coarse grid, using the session open of its own day.
    session_open_minutes = frame["candle_open"].dt.hour * 60 + frame["candle_open"].dt.minute
    base = 9 * 60 + 15
    offset = (session_open_minutes - base).clip(lower=0)
    coarse = (offset // minutes) * minutes
    frame["_bucket"] = (frame["candle_open"].dt.normalize()
                        + pd.to_timedelta(base + coarse, unit="m"))

    keys = ["instrument_key", "trading_symbol", "underlying", "kind", "expiry", "strike",
            "option_type", "_bucket"]
    grouped = frame.groupby(keys, sort=True, dropna=False)
    out = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        n_ticks=("n_ticks", "sum"),
        n_minute_buckets=("candle_open", "nunique"),
        volume_complete=("volume_complete", "all"),
        is_complete=("is_complete", "all"),
        quality_flags=("quality_flags", lambda s: ",".join(
            sorted({flag for text in s.fillna("") for flag in str(text).split(",") if flag}))),
        source_provider=("source_provider", "first"),
        first_tick_ts_ms=("first_tick_ts_ms", "min"),
        last_tick_ts_ms=("last_tick_ts_ms", "max"),
    ).reset_index()

    volume = grouped["volume"].sum(min_count=1).reset_index(name="volume")
    oi = grouped["open_interest"].last().reset_index(name="open_interest")
    prev_oi = grouped["open_interest_previous"].last().reset_index(name="open_interest_previous")
    volume_basis = grouped["volume_basis"].agg(
        lambda s: VOLUME_BASIS_CUMULATIVE if (s == VOLUME_BASIS_CUMULATIVE).any()
        else VOLUME_BASIS_UNAVAILABLE).reset_index(name="volume_basis")
    for piece in (volume, oi, prev_oi, volume_basis):
        out = out.merge(piece, on=keys, how="left")

    out["timeframe_minutes"] = minutes
    out["candle_open"] = out["_bucket"]
    out["candle_close"] = out["candle_open"] + pd.Timedelta(minutes=minutes)
    out["oi_is_last_observation"] = True
    out["candle_origin"] = CANDLE_ORIGIN_LOCAL
    # list(...): pandas treats a bare tuple as a single (MultiIndex) key, not a column set.
    return out[list(CANDLE_COLUMNS)].sort_values(
        ["instrument_key", "candle_open"]).reset_index(drop=True)


def candle_quality_report(candles: pd.DataFrame) -> dict:
    """Counts that must accompany any candle set: completeness, gaps and volume basis."""
    if candles is None or candles.empty:
        return {"candles": 0, "incomplete": 0, "volume_partial": 0,
                "volume_unavailable": 0, "instruments": 0}
    return {
        "candles": int(len(candles)),
        "incomplete": int((~candles["is_complete"].astype(bool)).sum()),
        "volume_partial": int((~candles["volume_complete"].astype(bool)).sum()),
        "volume_unavailable": int((candles["volume_basis"] == VOLUME_BASIS_UNAVAILABLE).sum()),
        "instruments": int(candles["instrument_key"].nunique()),
        "ticks": int(pd.to_numeric(candles["n_ticks"], errors="coerce").sum()),
    }
