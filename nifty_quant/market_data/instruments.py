"""Instrument discovery: resolve today's tradeable universe from the provider.

Nothing about the *current* market is hardcoded here.  Expiries, strikes, option
instrument keys, futures contracts and lot sizes all come back from the provider's own
instrument reference and option-chain endpoints, because all of them change: the NIFTY
expiry weekday moved from Thursday to Tuesday on 2025-09-01
(``data/reference/nifty_expiry_cadence.csv``), the board lot has changed four times since
2021, and every weekly expiry issues a brand-new set of option keys.

What *is* fixed, and deliberately so, is the shape of the selection: the underlying, how
many strikes above and below the money to follow, and the subscription mode each
instrument class needs.  Those are configuration, not market facts.

Keys that merely identify an instrument (``NSE_INDEX|Nifty 50``, ``NSE_INDEX|India VIX``)
are discovered from the instrument master and only fall back to the documented default
when the master is unavailable; the fallback is reported as such in ``notes`` so an
operator can see which path was taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

import pandas as pd

from nifty_quant.market_data.providers.base import (
    InstrumentKind,
    MarketDataProvider,
    Subscription,
    SubscriptionMode,
)

#: Documented, stable index identifiers.  Used only when discovery is unavailable; a
#: changed key is reported by the provider as an empty feed, which the health monitor
#: catches, rather than silently resolving to the wrong instrument here.
DEFAULT_NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
DEFAULT_INDIA_VIX_KEY = "NSE_INDEX|India VIX"

#: Column order of the tidy option-chain frame every provider must return.
CHAIN_COLUMNS = (
    "instrument_key", "expiry", "strike", "option_type", "ltp", "close_price",
    "volume", "oi", "prev_oi", "bid_price", "bid_qty", "ask_price", "ask_qty",
    "delta", "theta", "gamma", "vega", "rho", "iv",
)


class DiscoveryError(RuntimeError):
    """The universe could not be resolved to something safe to subscribe."""


def normalise_expiry(value) -> str:
    """Expiry as an ISO date string, from epoch-ms, date/datetime, or text.

    Upstox reports expiries as epoch milliseconds in the instrument master and as ISO
    strings in the option-chain endpoints; both forms must land on the same identity.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # epoch milliseconds (the master's format)
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc).date().isoformat()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        return datetime.strptime(text[:11], "%d-%b-%Y").date().isoformat()
    except ValueError as exc:
        raise DiscoveryError(f"unparsable expiry {value!r}") from exc


def _row_symbol(row: dict) -> str:
    return str(row.get("trading_symbol") or row.get("tradingsymbol") or "").strip().upper()


def find_index_key(master: Iterable[dict], symbol: str = "NIFTY 50") -> str:
    """The provider key of an index, matched on the master's own fields."""
    wanted = symbol.strip().upper()
    aliases = {wanted, wanted.replace(" ", ""), wanted.replace(" ", "_")}
    for row in master:
        segment = str(row.get("segment", "")).upper()
        if "INDEX" not in segment:
            continue
        names = {_row_symbol(row)}
        if row.get("name"):
            names.add(str(row["name"]).strip().upper())
        if names & aliases:
            key = str(row.get("instrument_key") or "")
            if key:
                return key
    raise DiscoveryError(f"index {symbol!r} not found in the instrument master")


def find_futures(master: Iterable[dict], underlying: str = "NIFTY",
                 as_of: date | None = None) -> list[dict]:
    """Unexpired futures rows for an underlying, nearest expiry first.

    Matched on ``instrument_type == FUT`` plus the underlying identity, and filtered by
    expiry against ``as_of`` so an expired contract can never be subscribed (the feed
    would accept it and simply never send a tick, which looks like a dead instrument
    rather than a bad universe).
    """
    today = (as_of or date.today()).isoformat()
    rows: list[dict] = []
    for row in master:
        if str(row.get("instrument_type", "")).upper() != "FUT":
            continue
        if str(row.get("segment", "")).upper() != "NSE_FO":
            continue
        underlying_symbol = str(row.get("underlying_symbol") or row.get("name") or "").upper()
        if underlying.upper() not in underlying_symbol:
            continue
        expiry = normalise_expiry(row.get("expiry"))
        if not expiry or expiry < today:
            continue
        rows.append({**row, "expiry_iso": expiry})
    rows.sort(key=lambda r: r["expiry_iso"])
    return rows


def atm_strike(chain: pd.DataFrame, spot: float | None = None) -> int | None:
    """Nearest available strike to the underlying spot price.

    ``spot`` comes from the chain's own ``underlying_spot_price`` when the provider
    supplies it; when it does not, the strike with the largest combined open interest is
    used - an OI-weighted money line, not a guess at a price.
    """
    if chain is None or chain.empty:
        return None
    strikes = sorted({int(s) for s in chain["strike"].dropna().unique()})
    if not strikes:
        return None
    if spot is not None and spot == spot and spot > 0:
        return min(strikes, key=lambda s: (abs(s - float(spot)), s))
    if "oi" in chain.columns:
        by_strike = chain.groupby("strike")["oi"].sum(min_count=1)
        if by_strike.notna().any():
            return int(by_strike.idxmax())
    return strikes[len(strikes) // 2]


def select_strikes(available: Sequence[int], atm: int, above: int, below: int) -> tuple[int, ...]:
    """The ATM strike plus ``below`` strikes under it and ``above`` over it.

    Selection happens on the strikes the provider actually listed, so a strike that does
    not exist for this expiry is skipped rather than invented.  When the window runs off
    the end of the listed grid the selection is shorter and the caller reports the
    shortfall - it is never padded with a made-up strike.
    """
    if atm is None:
        return ()
    ordered = sorted({int(s) for s in available})
    if atm not in ordered:
        ordered = sorted(ordered + [int(atm)])
    index = ordered.index(int(atm))
    lower = ordered[max(0, index - below):index]
    upper = ordered[index + 1:index + 1 + above]
    return tuple([*lower, int(atm), *upper])


@dataclass(frozen=True)
class InstrumentUniverse:
    """The resolved subscription universe, with the evidence used to build it."""

    provider: str
    generated_at: str
    subscriptions: tuple[Subscription, ...]
    expiries: tuple[str, ...] = ()
    atm_strike: int | None = None
    strikes: tuple[int, ...] = ()
    index: Subscription | None = None
    vix: Subscription | None = None
    futures: tuple[Subscription, ...] = ()
    options: tuple[Subscription, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    #: Previous-day open interest per option key, taken from the chain snapshot.  The
    #: live feed reports current OI only, so the change has no other honest source.
    previous_open_interest: dict[str, float] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.subscriptions)

    def by_mode(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for sub in self.subscriptions:
            out[sub.mode.value] = out.get(sub.mode.value, 0) + 1
        return out

    def keys(self) -> tuple[str, ...]:
        return tuple(s.instrument_key for s in self.subscriptions)

    def to_frame(self) -> pd.DataFrame:
        rows = [s.as_row() for s in self.subscriptions]
        if not rows:
            return pd.DataFrame(columns=["instrument_key", "kind", "mode", "trading_symbol",
                                         "underlying", "expiry", "strike", "option_type"])
        return pd.DataFrame(rows)

    def report(self) -> dict:
        return {
            "provider": self.provider,
            "generated_at": self.generated_at,
            "subscriptions": len(self.subscriptions),
            "by_mode": self.by_mode(),
            "expiries": list(self.expiries),
            "atm_strike": self.atm_strike,
            "strikes": list(self.strikes),
            "index": self.index.instrument_key if self.index else None,
            "vix": self.vix.instrument_key if self.vix else None,
            "futures": [s.instrument_key for s in self.futures],
            "options": len(self.options),
            "previous_open_interest": len(self.previous_open_interest),
            "notes": list(self.notes),
        }


def _index_subscription(key: str, symbol: str, underlying: str, mode: str) -> Subscription:
    return Subscription(instrument_key=key, kind=InstrumentKind.INDEX,
                        mode=SubscriptionMode(mode), trading_symbol=symbol,
                        underlying=underlying)


def discover_universe(
    provider: MarketDataProvider,
    config,
    *,
    as_of: date | None = None,
    use_master: bool | None = None,
) -> InstrumentUniverse:
    """Resolve the universe the collector should subscribe to right now.

    Order of work: index keys (master first, documented default second), option expiries
    from the provider, the option chain for each requested expiry, the ATM strike from the
    chain's own spot price, then the strike window.  Futures are added when the master is
    available; a missing master is reported in ``notes`` and never blocks the option
    collection, because options are the research priority.
    """
    today = as_of or date.today()
    notes: list[str] = []
    include_master = config.use_instrument_master if use_master is None else use_master
    master: list[dict] = []
    if include_master:
        try:
            master = provider.instrument_master()
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            notes.append(f"instrument master unavailable ({type(exc).__name__}); "
                         f"index keys fell back to documented defaults")

    index_key = ""
    vix_key = ""
    if master:
        try:
            index_key = find_index_key(master, "NIFTY 50")
        except DiscoveryError as exc:
            notes.append(str(exc))
        try:
            vix_key = find_index_key(master, "INDIA VIX")
        except DiscoveryError as exc:
            notes.append(str(exc))
    if not index_key:
        index_key = config.index_instrument_key or DEFAULT_NIFTY_INDEX_KEY
    if not vix_key:
        vix_key = config.vix_instrument_key or DEFAULT_INDIA_VIX_KEY

    subscriptions: list[Subscription] = []
    index_sub = vix_sub = None
    if config.include_index:
        index_sub = _index_subscription(index_key, "NIFTY 50", config.underlying,
                                        config.index_mode)
        subscriptions.append(index_sub)
    if config.include_vix:
        vix_sub = _index_subscription(vix_key, "INDIA VIX", "INDIA VIX", config.index_mode)
        subscriptions.append(vix_sub)

    future_subs: list[Subscription] = []
    if config.include_futures:
        if master:
            rows = find_futures(master, config.underlying, as_of=today)
            if not rows:
                notes.append("no unexpired NIFTY futures listed in the instrument master")
            seen: set[str] = set()
            for row in rows:
                if row["expiry_iso"] in seen:
                    continue
                seen.add(row["expiry_iso"])
                future_subs.append(Subscription(
                    instrument_key=str(row["instrument_key"]),
                    kind=InstrumentKind.FUTURE,
                    mode=SubscriptionMode(config.option_mode),
                    trading_symbol=str(row.get("trading_symbol") or ""),
                    underlying=config.underlying,
                    expiry=row["expiry_iso"],
                ))
        else:
            notes.append("futures skipped: no instrument master to resolve contracts from")
    subscriptions.extend(future_subs)

    expiries = [e for e in provider.option_expiries(index_key) if e >= today.isoformat()]
    expiries.sort()
    wanted_expiries = tuple(
        expiries[offset] for offset in sorted(set(config.expiry_offsets))
        if offset < len(expiries))
    if not wanted_expiries:
        notes.append("provider published no unexpired option expiry for the underlying")

    option_subs: list[Subscription] = []
    previous_oi: dict[str, float] = {}
    strikes_selected: tuple[int, ...] = ()
    atm: int | None = None
    for expiry in wanted_expiries:
        chain = provider.option_chain(index_key, expiry)
        if chain is None or chain.empty:
            notes.append(f"option chain for {expiry} came back empty")
            continue
        spot = None
        if "underlying_spot_price" in chain.columns and chain["underlying_spot_price"].notna().any():
            spot = float(chain["underlying_spot_price"].dropna().iloc[0])
        else:
            notes.append(f"option chain for {expiry} carries no spot price; ATM strike "
                         f"resolved from open interest")
        atm = atm_strike(chain, spot)
        if atm is None:
            notes.append(f"option chain for {expiry} listed no strikes")
            continue
        available = sorted({int(s) for s in chain["strike"].dropna().unique()})
        selected = select_strikes(available, atm, config.strikes_above_atm,
                                  config.strikes_below_atm)
        if not strikes_selected:
            strikes_selected = selected
        if len(selected) < config.strikes_above_atm + config.strikes_below_atm + 1:
            notes.append(f"strike window for {expiry} truncated to {len(selected)} of "
                         f"{config.strikes_above_atm + config.strikes_below_atm + 1} "
                         f"listed strikes")
        window = chain[chain["strike"].astype(int).isin(selected)]
        for row in window.itertuples(index=False):
            prev = getattr(row, "prev_oi", None)
            if prev is not None and prev == prev:
                previous_oi[str(row.instrument_key)] = float(prev)
            option_subs.append(Subscription(
                instrument_key=str(row.instrument_key),
                kind=InstrumentKind.OPTION,
                mode=SubscriptionMode(config.option_mode),
                trading_symbol=str(getattr(row, "trading_symbol", "") or ""),
                underlying=config.underlying,
                expiry=normalise_expiry(row.expiry),
                strike=int(row.strike),
                option_type=str(row.option_type).upper(),
            ))
    subscriptions.extend(option_subs)

    unique: dict[str, Subscription] = {}
    for sub in subscriptions:
        unique.setdefault(sub.instrument_key, sub)
    if len(unique) != len(subscriptions):
        notes.append(f"dropped {len(subscriptions) - len(unique)} duplicate instrument keys")

    return InstrumentUniverse(
        provider=provider.capability.name,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        subscriptions=tuple(unique.values()),
        expiries=wanted_expiries,
        atm_strike=atm,
        strikes=strikes_selected,
        index=index_sub,
        vix=vix_sub,
        futures=tuple(future_subs),
        options=tuple(option_subs),
        notes=tuple(notes),
        previous_open_interest=previous_oi,
    )
