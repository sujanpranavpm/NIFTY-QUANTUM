"""Port of upstream ``example_orders_03_limit_rsi_dip.py``.

Idea (verbatim): when RSI is weak, place a passive buy LIMIT below the close.
Entries are limit buys 1.5% below the signal close; exits are limit sells 3%
above average entry. Each order expires after three bars.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class LimitRSIDipBuyer(OrderStrategy):
    """Passive RSI dip buyer using limit orders for entry and profit-taking."""

    SPEC = StrategySpec(
        strategy_id="orders_03",
        name="Limit RSI Dip Buyer",
        upstream_file="strategies/example_orders_03_limit_rsi_dip.py",
        family="mean_reversion",
        timeframe="1d",
        order_types=("limit",),
        entry_logic="RSI(14) < 40 and flat: buy limit 1.5% below the signal bar close, GTD 3 bars.",
        exit_logic="While long: sell limit 3% above average entry, GTD 3 bars. No signal exit.",
        risk_logic="No stop. Fill risk is explicit: if the dip never trades, the order expires.",
        required_data=("daily OHLCV", "RSI(14)"),
        default_weight=0.15,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. Rounding uses ₹1 tick in the source (round(x, 2)); NSE equities "
            "also quote in ₹0.05 ticks, so round(x, 2) is retained unchanged."
        ),
        source_summary="Passive limit dip buy at -1.5%, limit take-profit at +3%, 3-bar expiry.",
    )
    DEFAULT_PARAMS = {
        "rsi_period": 14,
        "entry_rsi": 40.0,
        "entry_discount": 0.015,
        "exit_premium": 0.03,
        "expiry_bars": 3,
        "weight": 0.15,
    }

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
            pending = self._has_pending(inst)

            if pos == 0 and not pending and value < self.p["entry_rsi"]:
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(
                        Order(
                            instrument=inst,
                            side=OrderSide.BUY,
                            quantity=shares,
                            order_type=OrderType.LIMIT,
                            limit_price=round(bar.close * (1.0 - self.p["entry_discount"]), 2),
                            expires_after_bars=self.p["expiry_bars"],
                        )
                    )
            elif pos > 0 and not pending:
                entry = self.get_average_entry_price(inst) or bar.close
                self._engine().submit(
                    Order(
                        instrument=inst,
                        side=OrderSide.SELL,
                        quantity=pos,
                        order_type=OrderType.LIMIT,
                        limit_price=round(entry * (1.0 + self.p["exit_premium"]), 2),
                        expires_after_bars=self.p["expiry_bars"],
                    )
                )
