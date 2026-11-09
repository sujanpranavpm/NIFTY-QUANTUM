"""Tick normalisation and per-instrument quality classification.

The provider sends *updates*, not trades.  A message can repeat the previous price, carry
no price at all (an open-interest-only update), arrive late, or report a cumulative
volume that has reset because the trading day rolled over.  Deciding which of those is
usable is the whole job of this module, and every decision is recorded on the tick rather
than applied silently:

``CLEAN``              a new, in-order, in-session, plausible observation
``DUPLICATE``          the same observation as the previous tick (repeated frame)
``OUT_OF_ORDER``       exchange time older than the last accepted tick
``STALE``              exchange time further behind local time than the threshold
``ABNORMAL_MOVE``      price moved more than ``abnormal_move_pct`` in one update
``MISSING_PRICE``      the update carried no last-traded price
``MISSING_TIMESTAMP``  the update carried no exchange timestamp
``UNKNOWN_INSTRUMENT`` a feed arrived for a key that is not in the subscribed universe
``OUT_OF_SESSION``     a print outside the configured session window

Only ``CLEAN`` and ``ABNORMAL_MOVE`` ticks feed candles (an abnormal move is real data -
it is flagged, not discarded).  Nothing is ever repaired, forward-filled or zeroed: a
missing field stays ``None`` so a consumer can tell "not reported" from "zero".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Iterable

from nifty_quant.market_data.providers.base import InstrumentKind, Subscription

#: Quality vocabulary (used verbatim in the Parquet ``quality`` column).
CLEAN = "CLEAN"
DUPLICATE = "DUPLICATE"
OUT_OF_ORDER = "OUT_OF_ORDER"
STALE = "STALE"
ABNORMAL_MOVE = "ABNORMAL_MOVE"
MISSING_PRICE = "MISSING_PRICE"
MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
OUT_OF_SESSION = "OUT_OF_SESSION"

#: Qualities whose prices may contribute to a candle.
USABLE_QUALITIES = frozenset({CLEAN, ABNORMAL_MOVE})
#: Qualities that are a genuine observation of the market (recorded, reported, not merged
#: into the candle grid because they are repeats or superseded by a newer tick).
OBSERVED_QUALITIES = frozenset({CLEAN, ABNORMAL_MOVE, DUPLICATE, STALE, OUT_OF_ORDER})


@dataclass(frozen=True)
class Tick:
    """One normalised market-data observation.

    ``volume`` is the provider's **cumulative** traded volume for the day, not the volume
    of this update: turning it into a per-interval volume is
    :class:`~nifty_quant.market_data.candle_builder.CandleAggregator`'s job, because only
    the aggregator knows which bucket an observation belongs to.
    """

    instrument_key: str
    kind: str
    mode: str
    exchange_ts_ms: int | None
    local_ts_ms: int
    ltp: float | None = None
    ltq: int | None = None
    close_price: float | None = None
    volume: int | None = None
    open_interest: float | None = None
    open_interest_previous: float | None = None
    iv: float | None = None
    bid_price: float | None = None
    bid_qty: int | None = None
    ask_price: float | None = None
    ask_qty: int | None = None
    depth: tuple[dict, ...] = ()
    delta: float | None = None
    theta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    rho: float | None = None
    trading_symbol: str = ""
    underlying: str = ""
    expiry: str = ""
    strike: int | None = None
    option_type: str = ""
    source_provider: str = ""
    quality: str = CLEAN
    quality_flags: tuple[str, ...] = ()
    sequence: int = 0

    @property
    def usable(self) -> bool:
        return self.quality in USABLE_QUALITIES

    @property
    def exchange_ts_ist(self) -> datetime | None:
        if self.exchange_ts_ms is None:
            return None
        return datetime.fromtimestamp(self.exchange_ts_ms / 1000.0, tz=timezone.utc).astimezone(
            _IST)

    def to_record(self) -> dict:
        """Flat record for Parquet.  Tuples become a JSON-free depth summary."""
        best_bid_qty, best_ask_qty = self.bid_qty, self.ask_qty
        return {
            "instrument_key": self.instrument_key,
            "trading_symbol": self.trading_symbol,
            "underlying": self.underlying,
            "kind": self.kind,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "exchange_ts_ms": self.exchange_ts_ms,
            "local_ts_ms": self.local_ts_ms,
            "ltp": self.ltp,
            "ltq": self.ltq,
            "close_price": self.close_price,
            "volume_cumulative": self.volume,
            "open_interest": self.open_interest,
            "open_interest_previous": self.open_interest_previous,
            "iv": self.iv,
            "bid_price": self.bid_price,
            "bid_qty": best_bid_qty,
            "ask_price": self.ask_price,
            "ask_qty": best_ask_qty,
            "depth_levels": len(self.depth),
            "delta": self.delta,
            "theta": self.theta,
            "gamma": self.gamma,
            "vega": self.vega,
            "rho": self.rho,
            "source_provider": self.source_provider,
            "subscription_mode": self.mode,
            "sequence": self.sequence,
            "quality": self.quality,
            "quality_flags": ",".join(self.quality_flags),
        }


try:  # zoneinfo is stdlib on 3.9+, but a Windows build can lack the tz database.
    from zoneinfo import ZoneInfo

    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - only when tzdata is missing
    _IST = timezone.utc


@dataclass
class _LastSeen:
    exchange_ts_ms: int | None = None
    ltp: float | None = None
    ltq: int | None = None
    volume: int | None = None
    open_interest: float | None = None


class TickNormalizer:
    """Turn decoded feed frames into :class:`Tick` observations.

    Stateful on purpose: duplicate, out-of-order and abnormal-move detection are all
    comparisons against the previous tick of the same instrument, and a stateless mapper
    cannot detect a repeat.
    """

    def __init__(self, universe, *, stale_after_seconds: float = 30.0,
                 abnormal_move_pct: float = 0.25,
                 session_open: time = time(9, 15), session_close: time = time(15, 30),
                 sequence_start: int = 0, provider_name: str = "") -> None:
        self.provider_name = provider_name
        self.stale_after_ms = float(stale_after_seconds) * 1000.0
        self.abnormal_move_pct = float(abnormal_move_pct)
        self.session_open = session_open
        self.session_close = session_close
        self._subs: dict[str, Subscription] = {
            sub.instrument_key: sub for sub in getattr(universe, "subscriptions", ())}
        self._last: dict[str, _LastSeen] = {}
        self._sequence = int(sequence_start)
        self._counts: dict[str, int] = {}

    # -- vocabulary --------------------------------------------------------
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def _count(self, quality: str) -> None:
        self._counts[quality] = self._counts.get(quality, 0) + 1

    def set_previous_open_interest(self, instrument_key: str, prev_oi: float | None) -> None:
        """Seed ``open_interest_previous`` from the option-chain snapshot.

        The feed reports current OI only; the previous day's OI is a reference value, so
        it comes from the chain rather than being inferred from our own first tick (which
        would report zero change for every instrument we started collecting mid-session).
        """
        state = self._last.setdefault(instrument_key, _LastSeen())
        if prev_oi is not None and prev_oi == prev_oi:
            setattr(state, "prev_oi", float(prev_oi))

    # -- normalisation -----------------------------------------------------
    def normalize_frame(self, frame: dict, *, received_at: datetime | None = None) -> list[Tick]:
        """Normalise one decoded ``FeedResponse`` dictionary."""
        received = received_at or datetime.now(timezone.utc)
        local_ms = int(received.timestamp() * 1000.0)
        out: list[Tick] = []
        for key, feed in (frame.get("feeds") or {}).items():
            tick = self.normalize_instrument(str(key), feed, received=received,
                                             local_ms=local_ms)
            if tick is not None:
                out.append(tick)
        return out

    def normalize_instrument(self, instrument_key: str, feed: dict, *,
                             received: datetime | None = None,
                             local_ms: int | None = None) -> Tick | None:
        if feed is None:
            return None
        received = received or datetime.now(timezone.utc)
        local_ms = int(received.timestamp() * 1000.0) if local_ms is None else local_ms
        sub = self._subs.get(instrument_key)
        mode = str(feed.get("mode") or "")
        payload = _payload(feed)
        ltpc = payload.get("ltpc") or {}
        flags: list[str] = []

        if sub is None:
            self._count(UNKNOWN_INSTRUMENT)
            tick = self._build(instrument_key, mode, payload, ltpc, local_ms, sub=None)
            return _replace(tick, quality=UNKNOWN_INSTRUMENT,
                            quality_flags=(*flags, "not_in_subscribed_universe"))

        exchange_ts = ltpc.get("ltt")
        ltp = ltpc.get("ltp")
        if exchange_ts is not None and (exchange_ts <= 0):
            exchange_ts = None

        tick = self._build(instrument_key, mode, payload, ltpc, local_ms, sub=sub)
        quality = CLEAN

        if exchange_ts is None:
            quality = MISSING_TIMESTAMP
            flags.append("no_exchange_timestamp")
        elif sub.kind is not InstrumentKind.INDEX and ltp is None:
            # An index can legitimately carry only a timestamp after the close; an option
            # or future cannot - it always has a last traded price once it has traded.
            quality = MISSING_PRICE
            flags.append("no_last_traded_price")

        state = self._last.get(instrument_key) or _LastSeen()
        session_ts = tick.exchange_ts_ist
        if session_ts is not None:
            clock = session_ts.time()
            if clock < self.session_open or clock > self.session_close:
                quality = OUT_OF_SESSION
                flags.append(f"clock_{clock.strftime('%H:%M')}_outside_session")

        if quality == CLEAN and state.exchange_ts_ms is not None and exchange_ts is not None:
            if exchange_ts < state.exchange_ts_ms:
                quality = OUT_OF_ORDER
                flags.append("exchange_time_regressed")
            elif (exchange_ts == state.exchange_ts_ms
                  and ltp == state.ltp
                  and (tick.volume is None or tick.volume == state.volume)
                  and (tick.open_interest is None or tick.open_interest == state.open_interest)):
                quality = DUPLICATE
                flags.append("repeat_of_previous_update")

        if quality == CLEAN and exchange_ts is not None:
            lag_ms = local_ms - exchange_ts
            if lag_ms > self.stale_after_ms:
                quality = STALE
                flags.append(f"lag_{int(lag_ms / 1000)}s_exceeds_threshold")

        if quality == CLEAN and state.ltp and ltp is not None and state.ltp > 0:
            move = abs(ltp - state.ltp) / state.ltp
            if move > self.abnormal_move_pct:
                quality = ABNORMAL_MOVE
                flags.append(f"move_{move * 100:.1f}pct")

        # Advance the comparison state only for genuine observations: a duplicate must not
        # reset the baseline, and a late tick must not move the clock backwards.
        if quality in OBSERVED_QUALITIES and exchange_ts is not None:
            state.exchange_ts_ms = exchange_ts
            state.ltp = ltp
            state.ltq = tick.ltq
            state.volume = tick.volume
            state.open_interest = tick.open_interest
            self._last[instrument_key] = state

        self._sequence += 1
        self._count(quality)
        return _replace(tick, quality=quality, quality_flags=tuple(flags),
                        sequence=self._sequence,
                        open_interest_previous=getattr(state, "prev_oi", None))

    def _build(self, instrument_key: str, mode: str, payload: dict, ltpc: dict,
               local_ms: int, *, sub: Subscription | None) -> Tick:
        depth = tuple(payload.get("depth") or ())
        best = depth[0] if depth else {}
        greeks = payload.get("greeks") or {}
        return Tick(
            instrument_key=instrument_key,
            kind=sub.kind.value if sub else "unknown",
            mode=mode or (sub.mode.value if sub else ""),
            exchange_ts_ms=int(ltpc["ltt"]) if ltpc.get("ltt") is not None else None,
            local_ts_ms=local_ms,
            ltp=_float(ltpc.get("ltp")),
            ltq=_int(ltpc.get("ltq")),
            close_price=_float(ltpc.get("cp")),
            volume=_int(payload.get("volume")),
            open_interest=_float(payload.get("oi")),
            open_interest_previous=None,
            iv=_float(payload.get("iv")),
            bid_price=_float(best.get("bid_price")),
            bid_qty=_int(best.get("bid_qty")),
            ask_price=_float(best.get("ask_price")),
            ask_qty=_int(best.get("ask_qty")),
            depth=depth,
            delta=_float(greeks.get("delta")),
            theta=_float(greeks.get("theta")),
            gamma=_float(greeks.get("gamma")),
            vega=_float(greeks.get("vega")),
            rho=_float(greeks.get("rho")),
            trading_symbol=sub.trading_symbol if sub else "",
            underlying=sub.underlying if sub else "",
            expiry=sub.expiry if sub else "",
            strike=sub.strike if sub else None,
            option_type=sub.option_type if sub else "",
            source_provider=self.provider_name,
        )


def _payload(feed: dict) -> dict:
    """The richest payload a feed entry carries, regardless of subscription mode."""
    if feed.get("market_ff"):
        return feed["market_ff"]
    if feed.get("index_ff"):
        return feed["index_ff"]
    if feed.get("first_level"):
        return feed["first_level"]
    return {"ltpc": feed.get("ltpc") or {}}


def _replace(tick: Tick, **changes) -> Tick:
    from dataclasses import replace

    return replace(tick, **changes)


def _float(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
