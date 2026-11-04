"""Port of upstream ``example_orders_07_stop_limit_breakout.py``.

Idea (verbatim): enter breakouts, but refuse to pay far above the stop trigger.
The stop activates above the recent 20-day high; the limit caps the maximum entry
price. This demonstrates fill risk: the order can trigger but not fill.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class StopLimitBreakout(OrderStrategy):
    """Breakout entries using stop-limit buy orders."""

    SPEC = StrategySpec(
        strategy_id="orders_07",
        name="Stop-Limit Breakout",
        upstream_file="strategies/example_orders_07_stop_limit_breakout.py",
        family="breakout",
        timeframe="1d",
        order_types=("stop_limit", "market"),
        entry_logic="Buy stop-limit: trigger 1.002 x prior 20-bar high, limit 0.4% above the trigger, GTD 5 bars.",
        exit_logic="Fixed time exit: market sell 12 bars after entry.",
        risk_logic="The limit caps slippage but creates non-fill risk after a fast breakout.",
        required_data=("daily adjusted close",),
        default_weight=0.10,
        published_max_position_size=0.20,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. Trigger-then-limit behaviour on the activation bar is preserved: the "
            "fill reference is floored at the stop price, never at the pre-trigger open."
        ),
        source_summary="Stop-limit breakout above the 20-bar high, 12-bar hold, market exit.",
    )
    DEFAULT_PARAMS = {
        "lookback": 20,
        "breakout_buffer": 0.002,
        "limit_buffer": 0.004,
        "hold_bars": 12,
        "expiry_bars": 5,
        "weight": 0.10,
    }

    def reset_state(self) -> None:
        self._bar_count: dict[str, int] = {}
        self._entry_bar: dict[str, int] = {}

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        close = self.instruments_data.get_feature("adj_close")
        if date not in close.index:
            return

        idx = close.index.get_loc(date)
        if idx < self.p["lookback"] + 1:
            return

        for inst in self.instruments:
            self._bar_count[inst] = self._bar_count.get(inst, 0) + 1
            bar = bars[inst]
            pos = current_positions.get(inst, 0.0)

            if pos > 0 and inst not in self._entry_bar:
                self._entry_bar[inst] = self._bar_count[inst]
            if pos == 0:
                self._entry_bar.pop(inst, None)

            if pos > 0 and self._bar_count[inst] - self._entry_bar.get(inst, 0) >= self.p["hold_bars"]:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                self._entry_bar.pop(inst, None)
                continue

            if pos == 0 and not self._has_pending(inst):
                prior_high = close[inst].iloc[idx - self.p["lookback"] : idx].max()
                if pd.isna(prior_high):
                    continue
                stop_price = round(prior_high * (1.0 + self.p["breakout_buffer"]), 2)
                limit_price = round(stop_price * (1.0 + self.p["limit_buffer"]), 2)
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(
                        Order(
                            inst,
                            OrderSide.BUY,
                            shares,
                            OrderType.STOP_LIMIT,
                            stop_price=stop_price,
                            limit_price=limit_price,
                            expires_after_bars=self.p["expiry_bars"],
                        )
                    )
