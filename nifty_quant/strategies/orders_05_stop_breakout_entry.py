"""Port of upstream ``example_orders_05_stop_breakout_entry.py``.

Idea (verbatim): place buy-stop orders above the recent 20-day high. A buy stop
waits for price confirmation; if the breakout triggers, the position is held for
10 bars and then exited at market.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class StopBreakoutEntry(OrderStrategy):
    """Buy-stop breakout strategy with a fixed 10-bar hold."""

    SPEC = StrategySpec(
        strategy_id="orders_05",
        name="Stop Breakout Entry",
        upstream_file="strategies/example_orders_05_stop_breakout_entry.py",
        family="breakout",
        timeframe="1d",
        order_types=("stop", "market"),
        entry_logic="Buy stop at 1.002 x the highest close of the prior 20 bars, GTD 5 bars.",
        exit_logic="Fixed time exit: market sell 10 bars after the position appears.",
        risk_logic="No stop-loss. The only risk control is the fixed holding period.",
        required_data=("daily adjusted close",),
        default_weight=0.10,
        published_max_position_size=0.20,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. 'Bars' are NSE trading sessions, so the 10-bar hold is 10 sessions "
            "rather than 10 calendar days as in the original ETF run."
        ),
        source_summary="Buy-stop above the 20-bar high, hold 10 bars, exit at market.",
    )
    DEFAULT_PARAMS = {"lookback": 20, "breakout_buffer": 0.002, "hold_bars": 10, "expiry_bars": 5, "weight": 0.10}

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
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(
                        Order(
                            inst,
                            OrderSide.BUY,
                            shares,
                            OrderType.STOP,
                            stop_price=stop_price,
                            expires_after_bars=self.p["expiry_bars"],
                        )
                    )
