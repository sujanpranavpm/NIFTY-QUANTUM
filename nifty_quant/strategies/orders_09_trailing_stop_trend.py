"""Port of upstream ``example_orders_09_trailing_stop_trend.py``.

Idea (verbatim): enter on SMA trend and let a 4% trailing stop manage the exit.
The stop ratchets upward as price makes new highs; the strategy never updates it
by hand because the fill engine owns the trailing anchor.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class TrailingStopTrend(OrderStrategy):
    """SMA trend entry with a trailing stop exit."""

    SPEC = StrategySpec(
        strategy_id="orders_09",
        name="Trailing Stop Trend",
        upstream_file="strategies/example_orders_09_trailing_stop_trend.py",
        family="trend_following",
        timeframe="1d",
        order_types=("market", "stop_trail"),
        entry_logic="SMA20 crosses above SMA50 and flat: market buy at 15% of NAV.",
        exit_logic="4% trailing stop placed once the position exists; reverse SMA cross cancels it and exits at market.",
        risk_logic="Trailing stop follows the highest high since placement, so realised loss is bounded by the 4% trail from the peak, not from entry.",
        required_data=("daily OHLCV", "SMA20", "SMA50"),
        default_weight=0.15,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. The trail anchors on intraday highs, so daily NSE candles are "
            "sufficient; intraday data is not required for this strategy."
        ),
        source_summary="SMA(20/50) entry, 4% trailing-stop exit, reverse cross as hard exit.",
    )
    DEFAULT_PARAMS = {"fast": 20, "slow": 50, "trail_percent": 0.04, "weight": 0.15}

    def reset_state(self) -> None:
        self._prev_signal: dict[str, int] = {}
        self._has_trail: dict[str, bool] = {}

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        sma_fast = self.instruments_data.get_feature(f"SMA_{self.p['fast']}_close")
        sma_slow = self.instruments_data.get_feature(f"SMA_{self.p['slow']}_close")
        if date not in sma_fast.index:
            return

        for inst in self.instruments:
            fast = sma_fast.loc[date, inst]
            slow = sma_slow.loc[date, inst]
            if pd.isna(fast) or pd.isna(slow):
                continue

            bar = bars[inst]
            pos = current_positions.get(inst, 0.0)
            signal = 1 if fast > slow else 0
            prev = self._prev_signal.get(inst, 0)

            if pos == 0 and self._has_trail.get(inst, False):
                self._has_trail[inst] = False

            if signal == 1 and prev == 0 and pos == 0:
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
            elif signal == 0 and prev == 1 and pos > 0:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                self._has_trail[inst] = False

            self._prev_signal[inst] = signal

    def describe_state(self, date: pd.Timestamp) -> dict:
        return {"trail_percent": self.p["trail_percent"], "trail_live": dict(self._has_trail)}
