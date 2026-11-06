"""Effective-dated NIFTY references + the session-based DTE ruler.

Wraps :class:`~nifty_quant.derivatives.calendar.NiftyReference` so the adapter asks
"what was the lot / tick / strike interval *on this date*?" and never today's value,
and adds the days-to-expiry ruler the reconciliation settled on: **DTE is counted in
index trading sessions** (available continuously 2021->2026), not in per-file print
days (which understate it) and not in the official bhavcopy calendar (which only
reaches 2024+).  Where the two rulers both reach (2024 onward) they agree exactly;
the index-session ruler is used everywhere so the whole window is measured on one
scale.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from nifty_quant.derivatives.calendar import NiftyReference


@dataclass(frozen=True)
class EffectiveLookup:
    """A resolved effective-dated parameter plus its honesty flag."""

    name: str
    on: date
    value: float
    estimated: bool
    provenance: str


class OptionReference:
    """Effective-dated parameter and expiry/DTE queries for the adapter."""

    def __init__(self, reference: Optional[NiftyReference] = None,
                 sessions: Optional[Sequence[date]] = None) -> None:
        self.ref = reference if reference is not None else NiftyReference.from_directory()
        self.sessions = sorted(sessions) if sessions is not None else sorted(self.ref.calendar.days)

    # ---- session ruler / DTE ----------------------------------------------
    def next_session(self, day: date) -> Optional[date]:
        i = bisect.bisect_right(self.sessions, day)
        return self.sessions[i] if i < len(self.sessions) else None

    def previous_session(self, day: date) -> Optional[date]:
        i = bisect.bisect_left(self.sessions, day)
        if i == 0:
            return None
        return self.sessions[i - 1]

    def is_session(self, day: date) -> bool:
        i = bisect.bisect_left(self.sessions, day)
        return i < len(self.sessions) and self.sessions[i] == day

    def dte(self, entry_day: date, expiry_day: date) -> int:
        """Index trading sessions strictly after ``entry_day`` up to ``expiry_day``.

        0 means ``entry_day`` is the expiry session itself (an expiry-day entry);
        1 means one session remains; the adapter enforces ``MIN_DTE = 2``.
        """
        lo = bisect.bisect_right(self.sessions, entry_day)
        hi = bisect.bisect_right(self.sessions, expiry_day)
        return max(0, hi - lo)

    # ---- effective-dated parameters ---------------------------------------
    def board_lot(self, on: date) -> EffectiveLookup:
        res = self.ref.board_lot(on)
        return EffectiveLookup("board_lot", on, float(res.value),
                               res.estimated, res.record.provenance)

    def tick_size(self, on: date) -> EffectiveLookup:
        res = self.ref.tick_size(on)
        return EffectiveLookup("price_tick", on, float(res.value),
                               res.estimated, res.record.provenance)

    def strike_interval(self, on: date) -> EffectiveLookup:
        res = self.ref.strike_interval(on)
        return EffectiveLookup("strike_interval", on, float(res.value),
                               res.estimated, res.record.provenance)

    # ---- expiry candidates -------------------------------------------------
    def expiry_candidates(self, entry_day: date, observed: Sequence[date],
                          min_dte: int = 2) -> list[date]:
        """Observed expiries at or beyond ``min_dte`` sessions from ``entry_day``,
        nearest first.  Expiry-day (DTE 0) and single-session (DTE 1) entries are
        excluded by construction."""
        out = [e for e in observed if e > entry_day and self.dte(entry_day, e) >= min_dte]
        return sorted(out)
