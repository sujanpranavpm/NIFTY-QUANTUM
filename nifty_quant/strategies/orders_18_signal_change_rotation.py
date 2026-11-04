"""Port of upstream ``example_orders_18_signal_change_rotation_orders.py``.

Idea (verbatim): an event-driven strategy with no calendar at all. Each name has
an SMA trend signal; the strategy trades only when a signal flips - buy on a
flat->long flip, sell on a long->flat flip. Between flips it does nothing.

Upstream calls this the lowest-churn way to run a trend strategy: turnover is
driven purely by how often trends change.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class SignalChangeRotationOrders(OrderStrategy):
    """Trade only on SMA trend-signal flips, in order mode."""

    SPEC = StrategySpec(
        strategy_id="orders_18",
        name="Signal-Change Rotation",
        upstream_file="strategies/example_orders_18_signal_change_rotation_orders.py",
        family="trend_following",
        timeframe="1d",
        order_types=("market",),
        entry_logic="SMA20 crosses above SMA50 on this bar (previous state flat): market buy at 18% of NAV.",
        exit_logic="SMA20 crosses below SMA50: market sell the whole position.",
        risk_logic="No stop-loss. Exit latency equals the SMA(20/50) cross, which can be many bars after the peak.",
        required_data=("daily OHLCV", "SMA20", "SMA50"),
        default_weight=0.18,
        published_max_position_size=0.20,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change, so the upstream quirk is preserved: unlike orders_01 it does not "
            "call cancel_all on the exit, which is harmless here because it never places a "
            "standing exit order. Crosses are detected from the persisted previous signal, so no "
            "bar is traded twice and no future bar is consulted."
        ),
        source_summary="Long/flat SMA(20/50) crossover, market orders only, traded on flips.",
    )
    DEFAULT_PARAMS = {"fast": 20, "slow": 50, "weight": 0.18}

    def reset_state(self) -> None:
        self._prev_signal: dict[str, int] = {}

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        fast = self.instruments_data.get_feature(f"SMA_{self.p['fast']}_close")
        slow = self.instruments_data.get_feature(f"SMA_{self.p['slow']}_close")
        if date not in fast.index:
            return

        for inst in self.instruments:
            f = fast.loc[date, inst]
            s = slow.loc[date, inst]
            if pd.isna(f) or pd.isna(s):
                continue

            signal = 1 if f > s else 0
            prev = self._prev_signal.get(inst, 0)
            pos = current_positions.get(inst, 0.0)

            # Event: flat -> long flip.
            if signal == 1 and prev == 0 and pos == 0:
                shares = int(nav * self.p["weight"] / bars[inst].close)
                if shares > 0:
                    self._engine().submit(Order(inst, OrderSide.BUY, shares, OrderType.MARKET))
            # Event: long -> flat flip.
            elif signal == 0 and prev == 1 and pos > 0:
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))

            self._prev_signal[inst] = signal

    def describe_state(self, date) -> dict:
        return {"previous_signal": dict(self._prev_signal)}
