"""Port of upstream ``example_orders_08_stop_limit_protection.py``.

Idea (verbatim): enter an SMA trend, then protect the downside with a stop-limit
sell. The stop-limit avoids selling below the limit price, but it can remain
unfilled after a gap — that fill risk is the point of the example.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class StopLimitProtection(OrderStrategy):
    """SMA trend entry with a protective stop-limit exit."""

    SPEC = StrategySpec(
        strategy_id="orders_08",
        name="Stop-Limit Protection",
        upstream_file="strategies/example_orders_08_stop_limit_protection.py",
        family="trend_following",
        timeframe="1d",
        order_types=("market", "stop_limit"),
        entry_logic="SMA20 crosses above SMA50 and flat: market buy.",
        exit_logic="Protective stop-limit at -4% / limit -4.5% from average entry; signal exit at market.",
        risk_logic="The limit can leave the position unprotected after a gap - unbounded downside "
        "until either the limit trades or the trend signal flips.",
        required_data=("daily OHLCV", "SMA20", "SMA50"),
        default_weight=0.15,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. NSE circuit limits and illiquidity make the unfilled-limit scenario "
            "more likely than in the original US ETF universe; the exit report records any position "
            "still open at the end of the sample."
        ),
        source_summary="SMA(20/50) entry with a -4%/-4.5% protective stop-limit.",
    )
    DEFAULT_PARAMS = {"fast": 20, "slow": 50, "stop_pct": 0.04, "limit_offset": 0.005, "weight": 0.15}

    def reset_state(self) -> None:
        self._prev_signal: dict[str, int] = {}
        self._has_stop_limit: dict[str, bool] = {}

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

            if pos == 0 and self._has_stop_limit.get(inst, False):
                self._has_stop_limit[inst] = False

            if signal == 1 and prev == 0 and pos == 0:
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(Order(inst, OrderSide.BUY, shares, OrderType.MARKET))
            elif pos > 0 and not self._has_stop_limit.get(inst, False):
                entry = self.get_average_entry_price(inst)
                if entry is not None:
                    self._engine().submit(
                        Order(
                            inst,
                            OrderSide.SELL,
                            pos,
                            OrderType.STOP_LIMIT,
                            stop_price=round(entry * (1.0 - self.p["stop_pct"]), 2),
                            limit_price=round(entry * (1.0 - self.p["stop_pct"] - self.p["limit_offset"]), 2),
                        )
                    )
                    self._has_stop_limit[inst] = True
            elif signal == 0 and prev == 1 and pos > 0:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                self._has_stop_limit[inst] = False

            self._prev_signal[inst] = signal
