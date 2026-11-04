"""Port of upstream ``example_orders_11_trailing_stop_limit.py``.

Idea (verbatim): trend entry with a trailing stop that converts to a limit
order. The stop follows price upward. When triggered, the order sells only at or
above the activated limit price, so gaps can leave the order open.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class TrailingStopLimitTrend(OrderStrategy):
    """SMA trend entry protected by a trailing stop-limit exit."""

    SPEC = StrategySpec(
        strategy_id="orders_11",
        name="Trailing Stop-Limit",
        upstream_file="strategies/example_orders_11_trailing_stop_limit.py",
        family="trend_following",
        timeframe="1d",
        order_types=("market", "stop_trail_limit"),
        entry_logic="SMA20 crosses above SMA50 and flat: market buy at 15% of NAV.",
        exit_logic="Trailing stop-limit: 4% of the close at placement as the trail distance, 0.5% of the close as the limit offset; reverse cross exits at market.",
        risk_logic="The limit leg refuses to sell below the activated limit, so a gap can leave the position open past the trail trigger.",
        required_data=("daily OHLCV", "SMA20", "SMA50"),
        default_weight=0.15,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. Trail distance and limit offset are recomputed in rupees from the "
            "bar close at placement, exactly as upstream; they are absolute amounts, not "
            "percentages, for the life of the order."
        ),
        source_summary="SMA(20/50) entry with a trailing stop-limit exit sized off the entry-bar close.",
    )
    DEFAULT_PARAMS = {
        "fast": 20,
        "slow": 50,
        "trail_pct_of_close": 0.04,
        "limit_pct_of_close": 0.005,
        "weight": 0.15,
    }

    def reset_state(self) -> None:
        self._prev_signal: dict[str, int] = {}
        self._has_trail_limit: dict[str, bool] = {}

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

            if pos == 0 and self._has_trail_limit.get(inst, False):
                self._has_trail_limit[inst] = False

            if signal == 1 and prev == 0 and pos == 0:
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(Order(inst, OrderSide.BUY, shares, OrderType.MARKET))
            elif pos > 0 and not self._has_trail_limit.get(inst, False):
                self._engine().submit(
                    Order(
                        inst,
                        OrderSide.SELL,
                        pos,
                        OrderType.STOP_TRAIL_LIMIT,
                        trail_amount=round(bar.close * self.p["trail_pct_of_close"], 2),
                        limit_offset=round(bar.close * self.p["limit_pct_of_close"], 2),
                    )
                )
                self._has_trail_limit[inst] = True
            elif signal == 0 and prev == 1 and pos > 0:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                self._has_trail_limit[inst] = False

            self._prev_signal[inst] = signal

    def describe_state(self, date: pd.Timestamp) -> dict:
        return {"trail_limit_live": dict(self._has_trail_limit)}
