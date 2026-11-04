"""Port of upstream ``example_orders_04_limit_trend_pullback.py``.

Idea (verbatim): in an uptrend, wait for a 1% pullback before entering. If close
is above SMA(50), place a limit buy below the close. Once long, place a 4% limit
take-profit and exit at market if the trend fails.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class LimitTrendPullback(OrderStrategy):
    """Trend-following entry that waits for a passive pullback fill."""

    SPEC = StrategySpec(
        strategy_id="orders_04",
        name="Limit Trend Pullback",
        upstream_file="strategies/example_orders_04_limit_trend_pullback.py",
        family="trend_following",
        timeframe="1d",
        order_types=("limit", "market"),
        entry_logic="Close above SMA(50) and flat: buy limit 1% below close, GTD 2 bars.",
        exit_logic="Close below SMA(50) -> cancel and sell at MARKET. Otherwise take-profit limit at "
        "+4% above average entry, GTD 5 bars.",
        risk_logic="Trend break acts as the stop (no explicit stop price).",
        required_data=("daily OHLCV", "SMA(50)"),
        default_weight=0.12,
        published_max_position_size=0.20,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. The branch order matters and is preserved: trend failure is checked "
            "before the take-profit re-arm, exactly as upstream."
        ),
        source_summary="Pullback limit entry in an uptrend, +4% limit target, market exit on SMA breach.",
    )
    DEFAULT_PARAMS = {
        "sma_period": 50,
        "entry_discount": 0.01,
        "take_profit": 0.04,
        "entry_expiry": 2,
        "exit_expiry": 5,
        "weight": 0.12,
    }

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        sma = self.instruments_data.get_feature(f"SMA_{self.p['sma_period']}_close")
        if date not in sma.index:
            return

        for inst in self.instruments:
            trend = sma.loc[date, inst]
            if pd.isna(trend):
                continue

            bar = bars[inst]
            pos = current_positions.get(inst, 0.0)
            pending = self._has_pending(inst)

            if pos == 0 and not pending and bar.close > trend:
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(
                        Order(
                            inst,
                            OrderSide.BUY,
                            shares,
                            OrderType.LIMIT,
                            limit_price=round(bar.close * (1.0 - self.p["entry_discount"]), 2),
                            expires_after_bars=self.p["entry_expiry"],
                        )
                    )
            elif pos > 0 and bar.close < trend:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
            elif pos > 0 and not pending:
                entry = self.get_average_entry_price(inst) or bar.close
                self._engine().submit(
                    Order(
                        inst,
                        OrderSide.SELL,
                        pos,
                        OrderType.LIMIT,
                        limit_price=round(entry * (1.0 + self.p["take_profit"]), 2),
                        expires_after_bars=self.p["exit_expiry"],
                    )
                )
