"""Strategy base class.

The upstream strategies subclass ``Backtester`` and implement
``_compute_orders(date, bars, current_positions, nav)`` using a small set of
helpers (``fill_engine``, ``instruments_data.get_feature``,
``get_average_entry_price``). This base reproduces exactly that surface, so each
ported strategy file is line-by-line comparable with its upstream counterpart.

The only differences from upstream are additive:

* numeric literals are exposed in ``DEFAULT_PARAMS`` so parameter-sensitivity
  tests can perturb them without editing logic;
* ``SPEC`` carries the documentation metadata required by the research
  deliverables (order types, timeframe, applicability, adaptation notes).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd

from nifty_quant.engine.fill_engine import FillEngine
from nifty_quant.engine.orders import BarData, Order, OrderSide, OrderType


@dataclass(frozen=True)
class StrategySpec:
    """Documentation metadata for one strategy."""

    strategy_id: str  # "orders_01"
    name: str
    upstream_file: str
    family: str
    timeframe: str  # "1d" | "5m" | "30m"
    order_types: tuple[str, ...]
    entry_logic: str
    exit_logic: str
    risk_logic: str
    required_data: tuple[str, ...]
    default_weight: float
    published_max_position_size: float
    direction: str  # "long_only" | "long_short"
    run_mode: str  # "per_stock" | "portfolio"
    applicable_to_nse_equity: bool
    adaptation_notes: str
    source_summary: str
    params: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "upstream_file": self.upstream_file,
            "family": self.family,
            "timeframe": self.timeframe,
            "order_types": ", ".join(self.order_types),
            "direction": self.direction,
            "run_mode": self.run_mode,
            "default_weight": self.default_weight,
            "published_max_position_size": self.published_max_position_size,
            "applicable_to_nse_equity": self.applicable_to_nse_equity,
            "required_data": ", ".join(self.required_data),
            "entry_logic": self.entry_logic,
            "exit_logic": self.exit_logic,
            "risk_logic": self.risk_logic,
            "adaptation_notes": self.adaptation_notes,
            "source_summary": self.source_summary,
            "default_params": self.params,
        }


class InstrumentDataView:
    """Read-only view over the feature panels (dates x instruments).

    ``get_feature`` returns the engine's fast numpy-backed panel, which supports
    the ``panel.loc[date, instrument]`` / ``panel[instrument].iloc[a:b]`` access
    patterns the upstream strategies use. The two contract-based ports feed
    whole panels into a pandas rolling helper instead, so they call
    ``get_frame`` to obtain the materialised DataFrame.
    """

    def __init__(self, features: Mapping[str, pd.DataFrame]) -> None:
        self._features = features

    def get_feature(self, name: str):
        try:
            return self._features[name]
        except KeyError as exc:
            raise AttributeError(f"unknown feature {name!r}; available: {sorted(self._features)}") from exc

    def get_frame(self, name: str) -> pd.DataFrame:
        """The feature as a pandas DataFrame (slower; for pandas-only consumers)."""
        feature = self.get_feature(name)
        to_frame = getattr(feature, "to_frame", None)
        return to_frame() if callable(to_frame) else feature

    @property
    def names(self) -> list[str]:
        return sorted(self._features)


class OrderStrategy(ABC):
    """Base class for the ported order-mode strategies."""

    SPEC: StrategySpec
    DEFAULT_PARAMS: dict[str, Any] = {}

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        unknown = set(params or {}) - set(self.DEFAULT_PARAMS) - set(self.extra_params())
        if unknown:
            raise ValueError(f"{self.SPEC.strategy_id}: unknown parameter(s) {sorted(unknown)}")
        self.p: dict[str, Any] = {**self.DEFAULT_PARAMS, **self.extra_params(), **dict(params or {})}
        self.fill_engine: FillEngine | None = None
        self.instruments_data: InstrumentDataView | None = None
        self.instruments: list[str] = []
        self._positions: dict[str, float] | None = None
        self._avg_entry: dict[str, float] | None = None
        self._bars: dict[str, BarData] = {}
        self.reset_state()

    # ---- hooks for subclasses -------------------------------------------
    def extra_params(self) -> dict[str, Any]:
        """Parameters that are not meant to be perturbed but must be accepted."""
        return {}

    def reset_state(self) -> None:
        """Reset any per-run bookkeeping. Called from ``__init__``."""

    # ---- binding --------------------------------------------------------
    def bind(
        self,
        fill_engine: FillEngine,
        instruments_data: InstrumentDataView,
        instruments: list[str],
        positions: dict[str, float],
        avg_entry: dict[str, float],
    ) -> "OrderStrategy":
        self.fill_engine = fill_engine
        self.instruments_data = instruments_data
        self.instruments = list(instruments)
        self._positions = positions
        self._avg_entry = avg_entry
        return self

    # ---- upstream-compatible helpers ------------------------------------
    def _has_pending(self, instrument: str) -> bool:
        engine = self._engine()
        return any(o.instrument == instrument and o.is_active for o in engine.pending_orders)

    def get_average_entry_price(self, instrument: str) -> float | None:
        if self._avg_entry is None:
            return None
        return self._avg_entry.get(instrument)

    def bar(self, instrument: str) -> BarData:
        return self._bars[instrument]

    def position(self, instrument: str) -> float:
        if self._positions is None:
            return 0.0
        return float(self._positions.get(instrument, 0.0))

    def order_market(self, instrument: str, quantity: float, tag: str = "") -> None:
        """Submit a signed market order, as the upstream ``order_market`` helper does."""
        if quantity == 0 or not abs(quantity) > 0:
            return
        side = OrderSide.BUY if quantity > 0 else OrderSide.SELL
        self._engine().submit(
            Order(instrument, side, abs(quantity), OrderType.MARKET, tag=tag)
        )

    def _engine(self) -> FillEngine:
        if self.fill_engine is None:
            raise RuntimeError("strategy is not bound to a fill engine")
        return self.fill_engine

    # ---- runner entry point ---------------------------------------------
    def on_bar(
        self,
        date: pd.Timestamp,
        bars: dict[str, BarData],
        current_positions: dict[str, float],
        nav: float,
    ) -> None:
        """Called once per bar, after that bar's fills have been processed."""
        self._bars = bars
        self._positions = current_positions
        self._compute_orders(date, bars, current_positions, nav)

    @abstractmethod
    def _compute_orders(
        self,
        date: pd.Timestamp,
        bars: dict[str, BarData],
        current_positions: dict[str, float],
        nav: float,
    ) -> None:
        """Emit orders. Same signature and contract as the upstream examples."""

    # ---- introspection for the signal engine ----------------------------
    def describe_state(self, date: pd.Timestamp) -> dict[str, Any]:
        """Optional strategy-specific state used by the signal report."""
        return {}
