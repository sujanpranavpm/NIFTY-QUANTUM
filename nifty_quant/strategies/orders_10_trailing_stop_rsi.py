"""Port of upstream ``example_orders_10_trailing_stop_rsi.py``.

Idea (verbatim): buy oversold RSI readings, then let a 5% trailing stop handle
risk. This combines mean-reversion entries with trend-style exit management: if
the rebound continues, the trailing stop keeps moving upward.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class TrailingStopRSI(OrderStrategy):
    """RSI dip entry protected by a percent trailing stop."""

    SPEC = StrategySpec(
        strategy_id="orders_10",
        name="Trailing Stop RSI",
        upstream_file="strategies/example_orders_10_trailing_stop_rsi.py",
        family="mean_reversion",
        timeframe="1d",
        order_types=("market", "stop_trail"),
        entry_logic="RSI(14) below 38 while flat and no pending order: market buy at 15% of NAV.",
        exit_logic="5% trailing stop placed after entry; RSI(14) above 65 cancels it and exits at market.",
        risk_logic="Trail from the post-entry peak. The RSI>65 take-profit is checked only on bars where the trailing order is already live.",
        required_data=("daily OHLCV", "RSI14"),
        default_weight=0.15,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. RSI is computed locally: Wilder smoothing by default, with the "
            "alternative SMA-RSI variant selectable in config because the vendor indicator "
            "implementation is not published."
        ),
        source_summary="RSI(14) < 38 entry with a 5% trailing stop and RSI > 65 exit.",
    )
    DEFAULT_PARAMS = {
        "rsi_period": 14,
        "entry_rsi": 38.0,
        "exit_rsi": 65.0,
        "trail_percent": 0.05,
        "weight": 0.15,
    }

    def reset_state(self) -> None:
        self._has_trail: dict[str, bool] = {}

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        rsi = self.instruments_data.get_feature(f"RSI_{self.p['rsi_period']}_close")
        if date not in rsi.index:
            return

        for inst in self.instruments:
            value = rsi.loc[date, inst]
            if pd.isna(value):
                continue

            bar = bars[inst]
            pos = current_positions.get(inst, 0.0)

            if pos == 0 and self._has_trail.get(inst, False):
                self._has_trail[inst] = False

            if pos == 0 and value < self.p["entry_rsi"] and not self._has_pending(inst):
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(Order(inst, OrderSide.BUY, shares, OrderType.MARKET))
            elif pos > 0 and not self._has_trail.get(inst, False):
                self._engine().submit(
                    Order(
                        inst,
                        OrderSide.SELL,
                        pos,
                        OrderType.STOP_TRAIL,
                        trail_percent=self.p["trail_percent"],
                    )
                )
                self._has_trail[inst] = True
            elif pos > 0 and value > self.p["exit_rsi"]:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                self._has_trail[inst] = False

    def describe_state(self, date: pd.Timestamp) -> dict:
        return {"trail_percent": self.p["trail_percent"], "trail_live": dict(self._has_trail)}
