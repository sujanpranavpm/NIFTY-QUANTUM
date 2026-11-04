"""Port of upstream ``example_orders_12_bracket_trend.py``.

Idea (verbatim): enter on SMA trend and attach take-profit plus stop-loss exits.
The bracket parent enters at market; the engine then activates a limit
take-profit and a stop-loss as an OCO pair.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import BracketSpec, Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class BracketTrend(OrderStrategy):
    """SMA trend entry with a +6% take-profit and -3% stop-loss bracket."""

    SPEC = StrategySpec(
        strategy_id="orders_12",
        name="Bracket Trend",
        upstream_file="strategies/example_orders_12_bracket_trend.py",
        family="trend_following",
        timeframe="1d",
        order_types=("bracket",),
        entry_logic="SMA20 crosses above SMA50 and flat: BRACKET buy at 15% of NAV.",
        exit_logic="Take-profit limit at +6% of the entry-bar close and stop-loss at -3%, activated as a one-cancels-other pair after the entry fills; reverse cross cancels both and exits at market.",
        risk_logic="Fixed 2:1 reward/risk per trade. The bracket, not the trend signal, normally closes the trade.",
        required_data=("daily OHLCV", "SMA20", "SMA50"),
        default_weight=0.15,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. Bracket prices are derived from the signal bar close, so the "
            "take-profit and stop-loss are fixed before the next-bar entry fill. The distance "
            "from the actual fill price therefore differs slightly, exactly as upstream."
        ),
        source_summary="SMA(20/50) entry with a +6%/-3% bracket exit.",
    )
    DEFAULT_PARAMS = {
        "fast": 20,
        "slow": 50,
        "take_profit_pct": 0.06,
        "stop_loss_pct": 0.03,
        "weight": 0.15,
    }

    def reset_state(self) -> None:
        self._prev_signal: dict[str, int] = {}
        self._has_bracket: dict[str, bool] = {}

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

            if pos == 0 and self._has_bracket.get(inst, False):
                self._has_bracket[inst] = False

            if signal == 1 and prev == 0 and pos == 0:
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    bracket = BracketSpec(
                        take_profit_price=round(bar.close * (1.0 + self.p["take_profit_pct"]), 2),
                        stop_loss_price=round(bar.close * (1.0 - self.p["stop_loss_pct"]), 2),
                    )
                    self._engine().submit(
                        Order(
                            inst,
                            OrderSide.BUY,
                            shares,
                            OrderType.BRACKET,
                            bracket=bracket,
                        )
                    )
                    self._has_bracket[inst] = True
            elif signal == 0 and prev == 1 and pos > 0:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                self._has_bracket[inst] = False

            self._prev_signal[inst] = signal

    def describe_state(self, date: pd.Timestamp) -> dict:
        return {"bracket_live": dict(self._has_bracket)}
