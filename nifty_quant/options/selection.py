"""Deterministic option-contract selection.

Given a directional daily signal (a ``CE``/``PE`` bias and an index spot) and the
entry session, this chooses exactly one contract the way a rules-based buyer would,
using **only information available at the decision time** (the effective strike
interval on the entry date and the index close on the signal date):

* direction -> option type: bullish buys a **CE**, bearish buys a **PE** (long only;
  no spreads, no writing);
* expiry   -> the **nearest** observed weekly expiry with ``DTE >= min_dte`` measured
  **at entry** (so expiry-day and single-session entries are excluded);
* strike   -> the **ATM** strike on the effective interval (``round(spot / interval)``,
  i.e. half-step ties go up), falling back to the nearest *available* strike that
  actually traded on the entry session, within ``max_strike_steps`` steps.

No Greeks, no IV, no future information. Every fallback is recorded so a reader can
see when the ideal ATM strike was not tradable and what was used instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from nifty_quant.derivatives.calendar import UnknownParameterError
from nifty_quant.options.data import MarketData
from nifty_quant.options.reference import OptionReference

CE, PE = "CE", "PE"


@dataclass
class ContractSelection:
    """The chosen contract, or the reason selection failed."""

    ok: bool
    direction: str = ""
    option_type: str = ""
    expiry: Optional[date] = None
    strike: float = 0.0
    strike_target: float = 0.0
    moneyness: str = ""            # "ATM" | "ITM" | "OTM"
    spot: float = 0.0
    dte_at_entry: int = 0
    lot_size: int = 0
    lot_estimated: bool = False
    tick_size: float = 0.0
    strike_interval: float = 0.0
    fallback_used: bool = False
    reason: Optional[str] = None
    notes: dict = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        if self.expiry is None:
            return ""
        return f"NIFTY{self.expiry.strftime('%d%b%Y').upper()}{int(self.strike)}{self.option_type}"


def _round_to_interval(spot: float, interval: float) -> float:
    if interval <= 0:
        return spot
    return float(int(spot / interval + 0.5) * interval)  # half-step ties go up


def _implied_interval(available: Sequence[float]) -> float:
    """Smallest positive gap between listed strikes - the grid the data itself shows.

    Used only when no effective-dated strike-interval record covers the entry date
    (e.g. pre-2024, before the bhavcopy arbiter); it is read from the traded strikes,
    never assumed from today's specification.
    """
    avail = sorted({float(s) for s in available})
    gaps = [b - a for a, b in zip(avail, avail[1:]) if b - a > 1e-9]
    return min(gaps) if gaps else 0.0


def classify_moneyness(strike: float, spot: float, option_type: str, interval: float) -> str:
    if interval > 0 and abs(strike - spot) <= interval / 2.0:
        return "ATM"
    if option_type == CE:
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def _nearest_available_strike(target: float, available: Sequence[float],
                              interval: float, max_steps: int) -> tuple[Optional[float], bool]:
    """Nearest traded strike to ``target`` within ``max_steps`` intervals.

    Returns ``(strike, fallback_used)`` - ``fallback_used`` is True when the exact
    target was not tradable and a neighbour was taken instead.
    """
    if not available:
        return None, False
    avail = sorted(float(s) for s in available)
    if target in avail or any(abs(s - target) < 1e-9 for s in avail):
        match = next(s for s in avail if abs(s - target) < 1e-9)
        return match, False
    nearest = min(avail, key=lambda s: abs(s - target))
    if interval > 0 and abs(nearest - target) > max_steps * interval + 1e-9:
        return None, True
    return nearest, True


def select_contract(entry_day: date, spot: float, direction: str, *,
                    ref: OptionReference, market: MarketData,
                    observed_expiries: Sequence[date],
                    min_dte: int = 2, max_strike_steps: int = 1,
                    resolution: str = "15m") -> ContractSelection:
    """Choose one CE/PE contract tradable on ``entry_day`` (see module docstring)."""
    option_type = CE if direction == "CE" else PE

    candidates = ref.expiry_candidates(entry_day, observed_expiries, min_dte=min_dte)
    if not candidates:
        return ContractSelection(ok=False, direction=direction, option_type=option_type,
                                 spot=spot, reason="DTE_BELOW_MIN" if direction else "NO_EXPIRY")

    # Effective-dated strike interval, if the reference covers the entry date. When it
    # does not (pre-2024, before the bhavcopy arbiter), fall back to the grid implied by
    # the traded strikes - never to a guessed specification.
    try:
        ref_interval = float(ref.strike_interval(entry_day).value)
        interval_covered = True
    except UnknownParameterError:
        ref_interval, interval_covered = 0.0, False

    last_reason = "NO_LIQUID_CONTRACT"
    for expiry in candidates:  # nearest first; fallback to the next expiry if unusable
        rows = market.contracts_on(expiry, entry_day, resolution=resolution)
        if rows is None or not len(rows):
            last_reason = "NO_OPTION_BARS_ON_ENTRY"
            continue
        rows = rows[rows["option_type"] == option_type]
        if not len(rows):
            last_reason = "NO_SUCH_OPTION_TYPE"
            continue
        available = sorted({float(s) for s in rows["strike"].unique()})
        interval = ref_interval if interval_covered and ref_interval > 0 else _implied_interval(available)
        target_strike = _round_to_interval(spot, interval)
        strike, fallback = _nearest_available_strike(target_strike, available, interval, max_strike_steps)
        if strike is None:
            last_reason = "STRIKE_UNAVAILABLE"
            continue

        try:
            lot = ref.board_lot(entry_day)  # effective board lot as of the entry session
        except UnknownParameterError:
            return ContractSelection(ok=False, direction=direction, option_type=option_type,
                                     spot=spot, expiry=expiry, strike=strike,
                                     strike_target=target_strike, reason="LOT_UNKNOWN")
        try:
            tick = ref.tick_size(entry_day)
        except UnknownParameterError:
            tick = None
        dte = ref.dte(entry_day, expiry)
        notes = {
            "lot_provenance": lot.provenance,
            "strike_interval_source": "reference" if interval_covered else "data_implied",
            "strike_interval_estimated": (not interval_covered),
        }
        if tick is not None:
            notes["tick_provenance"] = tick.provenance
        return ContractSelection(
            ok=True,
            direction=direction,
            option_type=option_type,
            expiry=expiry,
            strike=strike,
            strike_target=target_strike,
            moneyness=classify_moneyness(strike, spot, option_type, interval),
            spot=spot,
            dte_at_entry=dte,
            lot_size=int(lot.value),
            lot_estimated=lot.estimated,
            tick_size=float(tick.value) if tick is not None else 0.0,
            strike_interval=interval,
            fallback_used=fallback,
            notes=notes,
        )

    interval0 = ref_interval if interval_covered and ref_interval > 0 else 0.0
    return ContractSelection(ok=False, direction=direction, option_type=option_type,
                             spot=spot, strike_target=_round_to_interval(spot, interval0),
                             reason=last_reason)
