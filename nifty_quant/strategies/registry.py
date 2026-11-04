"""Registry of all twenty ported order-based strategies.

The order of :data:`STRATEGIES` is the upstream file order, ``orders_01`` through
``orders_20``; the research reports keep that order so any row can be traced back
to its source file in one step.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import pandas as pd

from nifty_quant.strategies.base import OrderStrategy, StrategySpec
from nifty_quant.strategies.orders_01_market_sma_cross import MarketSMACrossover
from nifty_quant.strategies.orders_02_market_rsi_reversion import MarketRSIReversion
from nifty_quant.strategies.orders_03_limit_rsi_dip import LimitRSIDipBuyer
from nifty_quant.strategies.orders_04_limit_trend_pullback import LimitTrendPullback
from nifty_quant.strategies.orders_05_stop_breakout_entry import StopBreakoutEntry
from nifty_quant.strategies.orders_06_stop_loss_protection import ProtectiveStopLoss
from nifty_quant.strategies.orders_07_stop_limit_breakout import StopLimitBreakout
from nifty_quant.strategies.orders_08_stop_limit_protection import StopLimitProtection
from nifty_quant.strategies.orders_09_trailing_stop_trend import TrailingStopTrend
from nifty_quant.strategies.orders_10_trailing_stop_rsi import TrailingStopRSI
from nifty_quant.strategies.orders_11_trailing_stop_limit import TrailingStopLimitTrend
from nifty_quant.strategies.orders_12_bracket_trend import BracketTrend
from nifty_quant.strategies.orders_13_bracket_rsi_reversion import BracketRSIReversion
from nifty_quant.strategies.orders_14_oco_dip_or_breakout import OCODipOrBreakout
from nifty_quant.strategies.orders_15_intraday_5m_bracket_reversion import (
    IntradayBracketReversion5m,
)
from nifty_quant.strategies.orders_16_intraday_30m_stop_breakout import (
    IntradayStopBreakout30m,
)
from nifty_quant.strategies.orders_17_monthly_rotation import MonthlyRotationOrders
from nifty_quant.strategies.orders_18_signal_change_rotation import (
    SignalChangeRotationOrders,
)
from nifty_quant.strategies.orders_19_fx_momentum_lots import FXMomentumLots
from nifty_quant.strategies.orders_20_futures_donchian_contracts import (
    FuturesDonchianContracts,
)

STRATEGIES: tuple[type[OrderStrategy], ...] = (
    MarketSMACrossover,
    MarketRSIReversion,
    LimitRSIDipBuyer,
    LimitTrendPullback,
    StopBreakoutEntry,
    ProtectiveStopLoss,
    StopLimitBreakout,
    StopLimitProtection,
    TrailingStopTrend,
    TrailingStopRSI,
    TrailingStopLimitTrend,
    BracketTrend,
    BracketRSIReversion,
    OCODipOrBreakout,
    IntradayBracketReversion5m,
    IntradayStopBreakout30m,
    MonthlyRotationOrders,
    SignalChangeRotationOrders,
    FXMomentumLots,
    FuturesDonchianContracts,
)

STRATEGY_IDS: tuple[str, ...] = tuple(cls.SPEC.strategy_id for cls in STRATEGIES)
_BY_ID: dict[str, type[OrderStrategy]] = {cls.SPEC.strategy_id: cls for cls in STRATEGIES}


def _check_registry() -> None:
    if len(STRATEGIES) != 20:
        raise AssertionError(f"expected 20 strategies, found {len(STRATEGIES)}")
    expected = tuple(f"orders_{i:02d}" for i in range(1, 21))
    if STRATEGY_IDS != expected:
        raise AssertionError(f"strategy ids out of order: {STRATEGY_IDS}")
    if len(_BY_ID) != len(STRATEGIES):
        raise AssertionError("duplicate strategy ids in registry")


_check_registry()


def get_strategy(strategy_id: str, params: Mapping[str, Any] | None = None) -> OrderStrategy:
    """Instantiate one strategy by id, optionally overriding parameters."""
    try:
        cls = _BY_ID[strategy_id]
    except KeyError as exc:
        raise KeyError(f"unknown strategy {strategy_id!r}; known: {list(_BY_ID)}") from exc
    return cls(params)


def strategy_class(strategy_id: str) -> type[OrderStrategy]:
    return _BY_ID[strategy_id]


def specs() -> list[StrategySpec]:
    return [cls.SPEC for cls in STRATEGIES]


def spec(strategy_id: str) -> StrategySpec:
    return strategy_class(strategy_id).SPEC


def default_params(strategy_id: str) -> dict[str, Any]:
    return dict(strategy_class(strategy_id).DEFAULT_PARAMS)


def applicable_ids(equity_only: bool = True) -> list[str]:
    """Strategy ids that can run on NSE cash equity with no logic change."""
    return [
        cls.SPEC.strategy_id
        for cls in STRATEGIES
        if cls.SPEC.applicable_to_nse_equity or not equity_only
    ]


def excluded_ids() -> list[str]:
    return [cls.SPEC.strategy_id for cls in STRATEGIES if not cls.SPEC.applicable_to_nse_equity]


def by_timeframe(timeframe: str) -> list[str]:
    return [cls.SPEC.strategy_id for cls in STRATEGIES if cls.SPEC.timeframe == timeframe]


def spec_table() -> pd.DataFrame:
    """One row per strategy, for the documentation and dashboard exports."""
    rows = []
    for cls in STRATEGIES:
        row = cls.SPEC.as_row()
        row["default_params"] = cls.SPEC.params or dict(cls.DEFAULT_PARAMS)
        rows.append(row)
    return pd.DataFrame(rows)


def selection(strategy_ids: Iterable[str] | None = None, equity_only: bool = True) -> list[str]:
    """Resolve a user-supplied id list against the registry, preserving order."""
    if strategy_ids is None:
        return applicable_ids(equity_only=equity_only)
    resolved: list[str] = []
    for strategy_id in strategy_ids:
        if strategy_id not in _BY_ID:
            raise KeyError(f"unknown strategy {strategy_id!r}")
        if equity_only and not _BY_ID[strategy_id].SPEC.applicable_to_nse_equity:
            raise ValueError(
                f"{strategy_id} is not applicable to NSE cash equity; "
                "pass equity_only=False to inspect it explicitly"
            )
        resolved.append(strategy_id)
    return resolved
