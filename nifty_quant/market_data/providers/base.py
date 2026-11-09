"""The provider-independent market-data interface.

The research engine must never learn a provider's URL, protobuf schema or credential
flow.  Everything a provider can vary is stated here as data:

* :class:`ProviderCapabilities` declares what the source can actually serve, so a
  missing capability is a queryable fact rather than a runtime surprise;
* :class:`Subscription` is the normalised identity of one streamed instrument - the
  provider key is *preserved verbatim* next to the contract identity, because the key is
  what a future provider or the same provider's REST candles are queried with;
* :func:`enforce_subscription_limits` refuses an over-large universe instead of
  silently truncating it (a truncated subscription is invisible data loss).

A provider that cannot satisfy the interface raises :class:`ProviderUnsupported` rather
than returning an empty result that looks like "no data".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Sequence

import pandas as pd

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
class SubscriptionMode(str, Enum):
    """WebSocket subscription modes, ordered from lightest to heaviest.

    A mode carries a hard limit, so the mode is chosen per instrument by what the
    collection actually needs - subscribing everything to the heaviest mode would waste
    the smallest budget in the table.
    """

    LTPC = "ltpc"
    OPTION_GREEKS = "option_greeks"
    FULL = "full"
    FULL_D30 = "full_d30"


class InstrumentKind(str, Enum):
    INDEX = "index"
    FUTURE = "future"
    OPTION = "option"


class ConnectionState(str, Enum):
    """The states the engine may report.  There is no implicit "probably fine"."""

    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    AUTH_FAILED = "AUTH_FAILED"
    SESSION_CLOSED = "SESSION_CLOSED"


class MarketPhase(str, Enum):
    """Coarse market phase derived from authoritative status, never from the clock."""

    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class MarketDataError(RuntimeError):
    """Base class: a provider-side condition the engine must record, not swallow."""


class ProviderUnsupported(MarketDataError):
    """The provider cannot serve this request (capability absent)."""


class AuthenticationError(MarketDataError):
    """The credential was rejected, is missing, or has expired."""

    def __init__(self, message: str, state: ConnectionState = ConnectionState.AUTH_FAILED) -> None:
        super().__init__(message)
        self.state = state


class RateLimitError(MarketDataError):
    """The provider's rate limit was exceeded and retries did not clear it."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderAPIError(MarketDataError):
    """A non-retryable provider response (schema change, bad request, 5xx exhausted)."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class SubscriptionLimitError(MarketDataError):
    """The requested universe exceeds a documented provider limit."""


# ---------------------------------------------------------------------------
# Capabilities and identity
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModeLimit:
    """One documented subscription limit for a mode."""

    individual: int
    combined: int


#: Upstox V3 feed limits, as published on the Market Data Feed V3 page.  ``full_d30``
#: is a Plus-only mode and is included so the engine can refuse it honestly on a free
#: analytics token rather than discovering the refusal mid-session.
DEFAULT_MODE_LIMITS: dict[SubscriptionMode, ModeLimit] = {
    SubscriptionMode.LTPC: ModeLimit(individual=5000, combined=2000),
    SubscriptionMode.OPTION_GREEKS: ModeLimit(individual=3000, combined=2000),
    SubscriptionMode.FULL: ModeLimit(individual=2000, combined=1500),
    SubscriptionMode.FULL_D30: ModeLimit(individual=50, combined=1500),
}

#: Connections a single user may hold open.  The collector uses exactly one and the
#: REST fallback uses none, so this is a ceiling to assert against, not a target.
DEFAULT_MAX_CONNECTIONS = 2


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can and cannot do, stated explicitly."""

    name: str
    websocket: bool = True
    rest_snapshots: bool = True
    option_chain: bool = False
    option_greeks: bool = False
    historical_intraday: bool = False
    historical_expired_contracts: bool = False
    market_status: bool = False
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    mode_limits: dict[SubscriptionMode, ModeLimit] = field(
        default_factory=lambda: dict(DEFAULT_MODE_LIMITS))
    notes: str = ""


@dataclass(frozen=True)
class Subscription:
    """One instrument in the collection universe.

    ``instrument_key`` is the provider's own identifier and is preserved exactly as the
    provider returned it: it is the string every later REST candle call needs, and
    rewriting it (normalising separators, upper-casing) is how key lookups silently stop
    matching.
    """

    instrument_key: str
    kind: InstrumentKind
    mode: SubscriptionMode
    trading_symbol: str = ""
    underlying: str = ""
    expiry: str = ""          # ISO date, "" for an index
    strike: int | None = None
    option_type: str = ""     # "CE" | "PE" | ""

    def __post_init__(self) -> None:
        if not self.instrument_key:
            raise ValueError("subscription needs a provider instrument key")
        if self.kind is InstrumentKind.OPTION:
            if self.strike is None or self.option_type not in {"CE", "PE"}:
                raise ValueError(f"option subscription {self.instrument_key} needs "
                                 f"a strike and CE/PE, got {self.strike!r}/{self.option_type!r}")
            if not self.expiry:
                raise ValueError(f"option subscription {self.instrument_key} needs an expiry")
        if self.option_type and self.option_type not in {"CE", "PE", ""}:
            raise ValueError(f"unknown option type {self.option_type!r}")

    @property
    def contract_key(self) -> tuple[str, str, int, str] | None:
        """``(symbol, expiry, strike, option_type)`` - the repo's own contract identity."""
        if self.kind is not InstrumentKind.OPTION:
            return None
        return (self.underlying, self.expiry, int(self.strike), self.option_type)

    def as_row(self) -> dict:
        return {
            "instrument_key": self.instrument_key,
            "kind": self.kind.value,
            "mode": self.mode.value,
            "trading_symbol": self.trading_symbol,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
        }


def enforce_subscription_limits(subscriptions: Sequence[Subscription],
                                capabilities: ProviderCapabilities) -> dict:
    """Validate a universe against the provider's published limits.

    Returns a small report (per-mode counts and the binding limit) so the caller can log
    what it actually subscribed to.  Raises :class:`SubscriptionLimitError` when any
    individual or combined limit would be exceeded - the engine must never quietly drop
    instruments to fit.
    """
    counts: dict[str, int] = {}
    for sub in subscriptions:
        counts[sub.mode.value] = counts.get(sub.mode.value, 0) + 1

    for mode_value, count in counts.items():
        try:
            mode = SubscriptionMode(mode_value)
        except ValueError as exc:  # pragma: no cover - guarded by the enum
            raise SubscriptionLimitError(f"unknown subscription mode {mode_value!r}") from exc
        limit = capabilities.mode_limits.get(mode)
        if limit is None:
            raise SubscriptionLimitError(
                f"provider {capabilities.name!r} does not document a limit for "
                f"mode {mode_value!r}; refusing to subscribe blind")
        if count > limit.individual:
            raise SubscriptionLimitError(
                f"{count} instruments requested in {mode_value!r} mode exceeds the "
                f"individual limit of {limit.individual}")
        if count > limit.combined:
            raise SubscriptionLimitError(
                f"{count} instruments requested in {mode_value!r} mode exceeds the "
                f"combined per-user limit of {limit.combined}")

    keys = [s.instrument_key for s in subscriptions]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise SubscriptionLimitError(
            f"duplicate instrument keys in the universe: {duplicates[:5]}")

    return {
        "total": len(subscriptions),
        "by_mode": counts,
        "modes_used": sorted(counts),
        "max_individual_limit": max(
            (capabilities.mode_limits[SubscriptionMode(m)].individual for m in counts),
            default=0),
    }


# ---------------------------------------------------------------------------
# Market status
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarketStatusSnapshot:
    """One authoritative market-status observation.

    ``source`` distinguishes an exchange-reported status from a locally inferred one:
    anything derived from configured session times carries ``estimated=True`` and must
    not be presented as an official open/closed decision.
    """

    exchange: str
    phase: MarketPhase
    raw_status: str = ""
    observed_at: datetime | None = None
    source: str = "provider"
    estimated: bool = False
    segments: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.phase is MarketPhase.OPEN


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------
class MarketDataProvider(ABC):
    """The interface a market-data source implements.

    Every method either returns a fully-formed result or raises; none returns a
    placeholder that could be mistaken for real data.
    """

    capability: ProviderCapabilities

    # -- discovery ---------------------------------------------------------
    @abstractmethod
    def instrument_master(self) -> list[dict]:
        """The provider's instrument reference rows (used to resolve indexes/futures)."""

    @abstractmethod
    def option_expiries(self, underlying_key: str) -> list[str]:
        """Unexpired expiry dates (ISO) for an underlying, nearest first."""

    @abstractmethod
    def option_chain(self, underlying_key: str, expiry: str) -> pd.DataFrame:
        """The option chain for one expiry as a tidy frame.

        One row per (strike, option type) with the provider's instrument key, last price,
        OI, volume, bid/ask and greeks where available.
        """

    # -- live feed ---------------------------------------------------------
    @abstractmethod
    def authorize_feed(self) -> str:
        """A single-use WebSocket URL for the market-data feed."""

    # -- reference / snapshots --------------------------------------------
    @abstractmethod
    def market_status(self) -> MarketStatusSnapshot:
        """Authoritative exchange status."""

    # -- history -----------------------------------------------------------
    def historical_candles(self, instrument_key: str, unit: str = "minutes",
                           interval: str = "1", from_date: str = "", to_date: str = "",
                           ) -> pd.DataFrame:
        """Provider historical candles for an eligible instrument."""
        raise ProviderUnsupported(
            f"{self.capability.name!r} does not implement historical candles")

    def intraday_candles(self, instrument_key: str, unit: str = "minutes",
                        interval: str = "1") -> pd.DataFrame:
        """Today's provider candles (a cross-check for locally aggregated ones)."""
        raise ProviderUnsupported(
            f"{self.capability.name!r} does not implement intraday candles")

    # -- housekeeping ------------------------------------------------------
    def close(self) -> None:
        """Release transport resources.  Safe to call more than once."""

    def describe(self) -> dict:
        cap = self.capability
        return {
            "provider": cap.name,
            "websocket": cap.websocket,
            "option_chain": cap.option_chain,
            "option_greeks": cap.option_greeks,
            "historical_intraday": cap.historical_intraday,
            "historical_expired_contracts": cap.historical_expired_contracts,
            "max_connections": cap.max_connections,
            "mode_limits": {m.value: {"individual": l.individual, "combined": l.combined}
                            for m, l in cap.mode_limits.items()},
            "notes": cap.notes,
        }


# ---------------------------------------------------------------------------
# Registry of provider constructors (mirrors nifty_quant.data.providers)
# ---------------------------------------------------------------------------
def build_provider(name: str, **kwargs) -> MarketDataProvider:
    """Construct a live provider by name, without the research layer importing it."""
    if name == "upstox":
        from nifty_quant.market_data.providers.upstox import UpstoxProvider
        return UpstoxProvider(**kwargs)
    raise ValueError(f"unknown live market-data provider {name!r}; available: ['upstox']")
