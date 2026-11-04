"""Development / out-of-sample splitting with a purge.

A single holdout split is enough to answer "does this survive data it was not
fitted on?", provided two things are true:

1. nothing in the development window can leak into the out-of-sample window;
2. the strategies are not re-tuned between the two.

Point (2) is enforced by the fact that the published parameters are used
unchanged; point (1) is why an embargo of ``purge_bars`` bars is dropped between
the two windows. Trades can span the boundary (a position opened in development
may still be open on day one of the out-of-sample window), so the embargo is the
only mechanism that keeps overlapping trades out of the held-out sample.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Split:
    """Three contiguous, non-overlapping windows over one trading index."""

    dev_start: pd.Timestamp
    dev_end: pd.Timestamp
    purge_start: pd.Timestamp | None
    purge_end: pd.Timestamp | None
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    dev_bars: int
    purge_bars: int
    oos_bars: int

    def as_row(self) -> dict:
        return {
            "dev_start": self.dev_start,
            "dev_end": self.dev_end,
            "dev_bars": self.dev_bars,
            "purge_start": self.purge_start,
            "purge_end": self.purge_end,
            "purge_bars": self.purge_bars,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "oos_bars": self.oos_bars,
        }


def dev_oos_split(
    index: pd.DatetimeIndex,
    *,
    dev_fraction: float = 0.70,
    purge_bars: int = 25,
    min_oos_bars: int = 120,
) -> Split:
    """Split ``index`` into development, embargo and out-of-sample windows.

    Args:
        index: the trading index of the run (sorted, unique).
        dev_fraction: share of bars kept for development.
        purge_bars: bars dropped between the two windows.
        min_oos_bars: refuse to build a split with a shorter held-out sample,
            because a two-month holdout says nothing about an edge.
    """
    index = pd.DatetimeIndex(index).sort_values()
    n = len(index)
    if n < 3:
        raise ValueError("need at least three bars to split")
    if not 0.0 < dev_fraction < 1.0:
        raise ValueError("dev_fraction must be strictly between 0 and 1")

    dev_bars = int(round(n * dev_fraction))
    purge = max(0, int(purge_bars))
    oos_bars = n - dev_bars - purge
    if oos_bars < min_oos_bars:
        # Shrink the embargo rather than silently returning a useless holdout.
        purge = max(0, n - dev_bars - min_oos_bars)
        oos_bars = n - dev_bars - purge
    if oos_bars < min_oos_bars:
        raise ValueError(
            f"index of {n} bars is too short for a {dev_fraction:.0%} split with "
            f"{min_oos_bars} out-of-sample bars"
        )

    dev_slice = index[:dev_bars]
    purge_slice = index[dev_bars : dev_bars + purge]
    oos_slice = index[dev_bars + purge :]
    return Split(
        dev_start=dev_slice[0],
        dev_end=dev_slice[-1],
        purge_start=purge_slice[0] if len(purge_slice) else None,
        purge_end=purge_slice[-1] if len(purge_slice) else None,
        oos_start=oos_slice[0],
        oos_end=oos_slice[-1],
        dev_bars=len(dev_slice),
        purge_bars=len(purge_slice),
        oos_bars=len(oos_slice),
    )
