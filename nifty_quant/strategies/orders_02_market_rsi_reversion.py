"""Port of upstream ``example_orders_02_market_rsi_reversion.py``.

Idea (verbatim): buy oversold liquid ETFs when RSI(14) is below 35, sell when
RSI is above 60. Both legs are MARKET orders.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class MarketRSIReversion(OrderStrategy):
    """RSI entry/exit strategy implemented with market orders."""

    SPEC = StrategySpec(
        strategy_id="orders_02",
        name="Market RSI Mean Reversion",
        upstream_file="strategies/example_orders_02_market_rsi_reversion.py",
        family="mean_reversion",
        timeframe="1d",
        order_types=("market",),
        entry_logic="RSI(14) below 35 with no open position.",
        exit_logic="RSI(14) above 60 while long.",
        risk_logic="None in the source (no stop or target). Exit is the RSI threshold, so the "
        "maximum adverse excursion is unbounded until the oscillator recovers.",
        required_data=("daily OHLCV", "RSI(14)"),
        default_weight=0.12,
        published_max_position_size=0.20,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. The upstream RSI(14) is produced server-side and is not published; "
            "this port uses Wilder RSI and exposes an SMA-RSI variant for sensitivity testing."
        ),
        source_summary="Buy RSI(14) < 35, sell RSI(14) > 60 at market.",
    )
    DEFAULT_PARAMS = {"rsi_period": 14, "entry_rsi": 35.0, "exit_rsi": 60.0, "weight": 0.12}

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
                    self._engine().submit(Order(inst, OrderSide.BUY, shares, OrderType.MARKET))
            elif pos > 0 and value > self.p["exit_rsi"]:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
