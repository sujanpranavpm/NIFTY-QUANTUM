"""Port of upstream ``example_orders_01_market_sma_cross.py``.

Idea (verbatim): buy on SMA(20) crossing above SMA(50), sell on the reverse
cross, using explicit MARKET orders. Long-only, daily, three ETFs upstream.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class MarketSMACrossover(OrderStrategy):
    """SMA(20/50) crossover implemented with market orders."""

    SPEC = StrategySpec(
        strategy_id="orders_01",
        name="Market SMA Crossover",
        upstream_file="strategies/example_orders_01_market_sma_cross.py",
        family="trend_following",
        timeframe="1d",
        order_types=("market",),
        entry_logic="SMA20 crosses above SMA50 (flat->long) and no position is open.",
        exit_logic="SMA20 crosses back below SMA50 while long.",
        risk_logic="None in the source: no stop, no target. Position size is a fixed fraction of NAV.",
        required_data=("daily OHLCV", "adjusted close", "SMA20", "SMA50"),
        default_weight=0.18,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. Upstream trades SPY/QQQ/IWM; here it runs on each NSE constituent "
            "in its own capital sleeve so the 18% sizing rule keeps its original meaning."
        ),
        source_summary="Buy SMA(20) above SMA(50), sell on the reverse cross.",
    )
    DEFAULT_PARAMS = {"fast": 20, "slow": 50, "weight": 0.18}

    def reset_state(self) -> None:
        self._prev_signal: dict[str, int] = {}

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

            if signal == 1 and prev == 0 and pos == 0 and not self._has_pending(inst):
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(Order(inst, OrderSide.BUY, shares, OrderType.MARKET))
            elif signal == 0 and prev == 1 and pos > 0:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))

            self._prev_signal[inst] = signal
