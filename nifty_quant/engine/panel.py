"""Fast read-only panel access for the strategy loop.

The ported strategies index feature panels per bar as
``sma_fast.loc[date, instrument]`` and, in a few cases,
``close[instrument].iloc[a:b].max()``. Pandas resolves each of those through
several layers, which costs roughly 2.5 ms per bar once 50 instruments are in
play. These wrappers expose exactly the same expressions over a dense float
matrix, so the strategy code stays byte-comparable with upstream while the
backtest runs several times faster.

Only *reading* is supported. Any attempt to mutate raises ``TypeError`` so a
strategy cannot accidentally modify shared feature data.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


def _reduce(values: np.ndarray, func, *, ddof: int = 0) -> float:
    """Reduce like pandas does: skip NaN, return NaN for an empty/all-NaN window."""
    data = np.asarray(values, dtype=float)
    if data.size == 0:
        return float("nan")
    usable = data[~np.isnan(data)]
    if usable.size == 0:
        return float("nan")
    if ddof and usable.size <= ddof:
        return float("nan")
    return float(func(usable) if not ddof else func(usable, ddof=ddof))


class _Slice:
    """A positional slice of one instrument's series.

    ``Series.iloc[a:b].max()`` skips NaN and yields NaN for an all-NaN window;
    numpy's bare reductions do not, so the difference is handled here.
    """

    __slots__ = ("_values",)

    def __init__(self, values: np.ndarray) -> None:
        self._values = values

    @property
    def values(self) -> np.ndarray:
        return self._values

    def to_numpy(self) -> np.ndarray:
        return self._values

    def max(self) -> float:
        return _reduce(self._values, np.max)

    def min(self) -> float:
        return _reduce(self._values, np.min)

    def mean(self) -> float:
        return _reduce(self._values, np.mean)

    def sum(self) -> float:
        return _reduce(self._values, np.sum)

    def std(self) -> float:
        return _reduce(self._values, np.std, ddof=1)

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, key):
        return self._values[key]

    def __array__(self, dtype=None, copy=None):  # pragma: no cover - numpy protocol
        return self._values if dtype is None else self._values.astype(dtype)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"_Slice(len={len(self._values)})"


class _IlocColumn:
    """``column.iloc[...]`` over a numpy view."""

    __slots__ = ("_values",)

    def __init__(self, values: np.ndarray) -> None:
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, slice):
            return _Slice(self._values[key])
        if isinstance(key, (list, tuple, np.ndarray)):
            return _Slice(self._values[key])
        return self._values[key]


class Column:
    """One instrument's feature series, supporting ``.iloc`` and ``.values``."""

    __slots__ = ("_values", "name")

    def __init__(self, values: np.ndarray, name: str = "") -> None:
        self._values = values
        self.name = name

    @property
    def iloc(self) -> _IlocColumn:
        return _IlocColumn(self._values)

    @property
    def values(self) -> np.ndarray:
        return self._values

    def to_numpy(self) -> np.ndarray:
        return self._values

    def __len__(self) -> int:
        return len(self._values)

    def __array__(self, dtype=None, copy=None):  # pragma: no cover - numpy protocol
        return self._values if dtype is None else self._values.astype(dtype)


class PanelIndex:
    """``DatetimeIndex`` lookalike with O(1) membership and position lookup.

    Every strategy guards its per-bar work with ``if date not in panel.index``,
    and pandas answers that in ~87 us because ``Index.__contains__`` goes through
    timedelta/tz normalisation. Behind a dict the same question costs ~0.1 us.
    Unknown attributes and operations are delegated to the real index, so code
    that wants an actual ``DatetimeIndex`` still gets one.
    """

    __slots__ = ("_index", "_rows")

    def __init__(self, index: pd.DatetimeIndex, rows: dict) -> None:
        self._index = index
        self._rows = rows

    def __contains__(self, key) -> bool:
        return key in self._rows

    def get_loc(self, key) -> int:
        try:
            return self._rows[key]
        except KeyError:
            raise KeyError(key) from None

    def equals(self, other) -> bool:
        if isinstance(other, PanelIndex):
            return self._index.equals(other._index)
        return self._index.equals(other)

    def __len__(self) -> int:
        return len(self._index)

    def __iter__(self):
        return iter(self._index)

    def __getitem__(self, key):
        return self._index[key]

    def __getattr__(self, name):
        return getattr(self._index, name)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"PanelIndex(len={len(self._index)})"


class _Loc:
    """``panel.loc[date, instrument]`` and ``panel.loc[date]``."""

    __slots__ = ("_panel",)

    def __init__(self, panel: "PanelView") -> None:
        self._panel = panel

    def __getitem__(self, key):
        panel = self._panel
        if isinstance(key, tuple):
            row_key, column_key = key
            row = panel._row_of(row_key)
            if isinstance(column_key, (list, tuple, slice, pd.Index)):
                raise TypeError("multi-column .loc access is not supported on PanelView")
            return panel._values[row, panel._column_of(column_key)]
        return pd.Series(panel._values[panel._row_of(key)], index=panel.columns, copy=False)


class _IlocRows:
    """``panel.iloc[int]`` -> one row as a Series indexed by instrument."""

    __slots__ = ("_panel",)

    def __init__(self, panel: "PanelView") -> None:
        self._panel = panel

    def __getitem__(self, key: int) -> pd.Series:
        panel = self._panel
        return pd.Series(panel._values[key], index=panel.columns, copy=False)


class PanelView:
    """Read-only, position-indexed view over a ``dates x instruments`` frame."""

    __slots__ = ("index", "columns", "_index", "_values", "_rows", "_cols", "_frame")

    def __init__(self, frame: pd.DataFrame, rows: dict | None = None) -> None:
        rows = rows if rows is not None else row_map(frame.index)
        self._index = pd.DatetimeIndex(frame.index)
        self.index = PanelIndex(self._index, rows)
        self.columns = list(frame.columns)
        self._values = (
            frame.to_numpy(dtype=float, copy=True)
            if len(frame) and len(self.columns)
            else np.empty((len(frame), len(self.columns)))
        )
        self._rows = rows
        self._cols = {name: j for j, name in enumerate(self.columns)}
        self._frame: pd.DataFrame | None = None

    # ---- internals ------------------------------------------------------
    def _row_of(self, timestamp) -> int:
        try:
            return self._rows[timestamp]
        except KeyError as exc:
            raise KeyError(f"{timestamp!r} is not in the panel index") from exc

    def _column_of(self, instrument) -> int:
        try:
            return self._cols[instrument]
        except KeyError as exc:
            raise KeyError(f"{instrument!r} is not a panel column") from exc

    # ---- public surface -------------------------------------------------
    @property
    def loc(self) -> _Loc:
        return _Loc(self)

    @property
    def iloc(self) -> _IlocRows:
        return _IlocRows(self)

    def __getitem__(self, instrument) -> Column:
        return Column(self._values[:, self._column_of(instrument)], name=str(instrument))

    def __contains__(self, instrument) -> bool:
        return instrument in self._cols

    def to_frame(self) -> pd.DataFrame:
        """The underlying pandas frame, materialised once and cached.

        Only the two contract-strategy ports need this, because they feed whole
        panels into a pandas rolling helper; every hot path uses ``.loc``.
        """
        if self._frame is None:
            self._frame = pd.DataFrame(
                self._values.copy(), index=self._index, columns=self.columns
            )
        return self._frame

    def __len__(self) -> int:
        return len(self.index)

    @property
    def shape(self) -> tuple[int, int]:
        return self._values.shape

    def __setitem__(self, key, value):
        raise TypeError("PanelView is read-only")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"PanelView(rows={len(self.index)}, columns={len(self.columns)})"


def row_map(index: pd.Index) -> dict:
    """``timestamp -> row position`` for one index.

    Iterating a ``DatetimeIndex`` yields boxed timestamps at roughly 3 us each,
    so the map is built once per run and shared by every panel that uses the same
    index (they all do, after ``to_views`` reindexes them onto it).
    """
    return dict(zip(index, range(len(index))))


def to_views(
    panels: dict[str, pd.DataFrame],
    index: pd.DatetimeIndex,
    rows: dict | None = None,
) -> dict[str, PanelView]:
    """Trim every panel to ``index`` and wrap it for fast per-bar reads."""
    if not panels:
        return {}
    rows = rows if rows is not None else row_map(index)
    return {
        name: PanelView(frame.reindex(index), rows=rows)
        for name, frame in panels.items()
    }


def columns_of(panels: Iterable[PanelView]) -> list[str]:
    first = next(iter(panels), None)
    return list(first.columns) if first is not None else []
