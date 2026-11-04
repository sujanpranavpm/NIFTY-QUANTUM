"""Port of upstream ``example_orders_06_stop_loss_protection.py``.

Idea (verbatim): enter on the SMA(20/50) trend and attach a 5% protective stop.
This is the classic two-step order workflow: enter first, then attach a stop once
the fill is visible in current positions.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class ProtectiveStopLoss(OrderStrategy):
    """SMA trend entry with one protective stop order per open position."""

    SPEC = StrategySpec(
        strategy_id="orders_06",
        name="Protective Stop Loss",
        upstream_file="strategies/example_orders_06_stop_loss_protection.py",
        family="trend_following",
        timeframe="1d",
        order_types=("market", "stop"),
        entry_logic="SMA20 crosses above SMA50 and flat: market buy.",
        exit_logic="SMA20 crosses back below SMA50: cancel working orders and sell at market. "
        "A protective stop at -5% from average entry also exits.",
        risk_logic="Fixed 5% protective stop on average entry; the stop is re-armed if it is "
        "cancelled while a position remains open.",
        required_data=("daily OHLCV", "SMA20", "SMA50"),
        default_weight=0.15,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. Because the stop is a plain STOP (not stop-limit), a gap down fills "
            "at the worse open — identical to upstream."
        ),
        source_summary="SMA(20/50) entry with a -5% protective stop and signal exit.",
    )
    DEFAULT_PARAMS = {"fast": 20, "slow": 50, "stop_pct": 0.05, "weight": 0.15}

    def reset_state(self) -> None:
        self._prev_signal: dict[str, int] = {}
        self._has_stop: dict[str, bool] = {}

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

            if pos == 0 and self._has_stop.get(inst, False):
                self._has_stop[inst] = False

            if signal == 1 and prev == 0 and pos == 0:
                shares = int(nav * self.p["weight"] / bar.close)
                if shares > 0:
                    self._engine().submit(Order(inst, OrderSide.BUY, shares, OrderType.MARKET))
            elif pos > 0 and not self._has_stop.get(inst, False):
                entry = self.get_average_entry_price(inst)
                if entry is not None:
                    self._engine().submit(
                        Order(
                            inst,
                            OrderSide.SELL,
                            pos,
                            OrderType.STOP,
                            stop_price=round(entry * (1.0 - self.p["stop_pct"]), 2),
                        )
                    )
                    self._has_stop[inst] = True
            elif signal == 0 and prev == 1 and pos > 0:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                self._has_stop[inst] = False

            self._prev_signal[inst] = signal
