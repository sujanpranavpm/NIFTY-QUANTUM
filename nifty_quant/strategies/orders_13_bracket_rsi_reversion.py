"""Port of upstream ``example_orders_13_bracket_rsi_reversion.py``.

Idea (verbatim): buy oversold RSI dips with a predefined reward/risk bracket.
Each RSI entry uses a bracket with +4% take-profit and -2% stop-loss. This is a
compact way to express a complete trade lifecycle in one order.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import BracketSpec, Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class BracketRSIReversion(OrderStrategy):
    """RSI dip-buying with bracket exits."""

    SPEC = StrategySpec(
        strategy_id="orders_13",
        name="Bracket RSI Reversion",
        upstream_file="strategies/example_orders_13_bracket_rsi_reversion.py",
        family="mean_reversion",
        timeframe="1d",
        order_types=("bracket",),
        entry_logic="RSI(14) below 35, flat, no pending order: BRACKET buy at 15% of NAV.",
        exit_logic="Take-profit limit at +4% and stop-loss at -2% of the signal-bar close, activated as an OCO pair; RSI(14) above 70 cancels both and exits at market.",
        risk_logic="Fixed 2:1 reward/risk. The RSI>70 exit overrides the bracket only when the position is still open.",
        required_data=("daily OHLCV", "RSI14"),
        default_weight=0.15,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. Wilder RSI is used by default (SMA-RSI selectable) because the "
            "vendor RSI implementation is not published; the documented impact is limited to "
            "threshold-crossing timing."
        ),
        source_summary="RSI(14) < 35 entry with +4%/-2% bracket and RSI > 70 discretionary exit.",
    )
    DEFAULT_PARAMS = {
        "rsi_period": 14,
        "entry_rsi": 35.0,
        "exit_rsi": 70.0,
        "take_profit_pct": 0.04,
        "stop_loss_pct": 0.02,
        "weight": 0.15,
    }

    def reset_state(self) -> None:
        pass

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

            if pos == 0 and value < self.p["entry_rsi"] and not self._has_pending(inst):
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
            elif pos > 0 and value > self.p["exit_rsi"]:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
