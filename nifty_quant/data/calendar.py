"""Trading-calendar helpers.

The authoritative session list is the one implied by the benchmark index bars
(``^NSEI``), because a stock can be suspended while the market is open but the
index never is. If the benchmark is unavailable, the union of stock sessions is
used and the difference is reported.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class TradingCalendar:
    """An immutable, sorted set of trading sessions."""

    sessions: pd.DatetimeIndex
    source: str = "union"

    @classmethod
    def from_frames(cls, frames: dict[str, pd.DataFrame], source: str = "union") -> "TradingCalendar":
        indexes = [frame.index for frame in frames.values() if not frame.empty]
        if not indexes:
            raise ValueError("cannot build a calendar from empty frames")
        union = indexes[0]
        for index in indexes[1:]:
            union = union.union(index)
        return cls(pd.DatetimeIndex(union).sort_values().unique(), source=source)

    @classmethod
    def from_sessions(cls, sessions: pd.DatetimeIndex, source: str = "reference") -> "TradingCalendar":
        return cls(pd.DatetimeIndex(sessions).sort_values().unique(), source=source)

    def __len__(self) -> int:
        return len(self.sessions)

    @property
    def start(self) -> pd.Timestamp:
        return self.sessions[0]

    @property
    def end(self) -> pd.Timestamp:
        return self.sessions[-1]

    def between(self, start: pd.Timestamp | str, end: pd.Timestamp | str) -> pd.DatetimeIndex:
        mask = (self.sessions >= pd.Timestamp(start)) & (self.sessions <= pd.Timestamp(end))
        return self.sessions[mask]

    def is_session(self, day: pd.Timestamp | str) -> bool:
        return pd.Timestamp(day) in set(self.sessions)

    def missing_sessions(self, frame: pd.DataFrame) -> int:
        """Sessions the market traded but this symbol has no bar for."""
        if frame.empty:
            return 0
        span = self.between(frame.index[0], frame.index[-1])
        return int(len(span.difference(pd.DatetimeIndex(frame.index))))

    def reference_slice(self, frame: pd.DataFrame) -> pd.DatetimeIndex:
        if frame.empty:
            return self.sessions
        return self.between(frame.index[0], frame.index[-1])
