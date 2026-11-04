"""Port of upstream ``example_orders_16_intraday_30m_stop_breakout.py``.

Idea (verbatim): on 30-minute bars, place buy-stop orders above the recent
12-bar high so entries only trigger on confirmed intraday breakouts. Positions
are held for a fixed number of bars and then exited at market.

Upstream notes that at 30m a session has ~13 bars, so a 12-bar high is roughly a
prior-session breakout, and that a gap through the stop fills at the worse open.

Faithfulness note: upstream does *not* guard the entry with ``_has_pending``, so
while a stop order is live another identical order can be stacked on later bars
(it overwrites ``_entry_bar`` each time). That behaviour is reproduced rather
than corrected, and the resulting duplicate-fill exposure is documented in the
strategy dossier.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class IntradayStopBreakout30m(OrderStrategy):
    """30-minute breakout via buy-stop, fixed holding period."""

    SPEC = StrategySpec(
        strategy_id="orders_16",
        name="Intraday 30m Stop Breakout",
        upstream_file="strategies/example_orders_16_intraday_30m_stop_breakout.py",
        family="intraday_breakout",
        timeframe="30m",
        order_types=("stop", "market"),
        entry_logic="BUY STOP at 1.001 x the highest high of the prior 12 bars, GTD 3 bars, 20% of NAV.",
        exit_logic="After 8 bars in the position, cancel all orders and sell at market.",
        risk_logic="No stop-loss; the only risk control is the fixed 8-bar horizon. Stacked stop orders can enlarge the position beyond the 20% target.",
        required_data=("30-minute OHLCV",),
        default_weight=0.20,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "Logic unchanged, including the missing pending-order guard, which is documented "
            "rather than silently fixed. Two Indian-market caveats: intraday equity positions "
            "cannot legally be carried overnight, and NSE has no 30-minute bar native to the "
            "exchange - bars are resampled from 1-minute or 5-minute data, so the 12-bar high "
            "depends on the resampling convention. Positions open at the session end are flagged."
        ),
        source_summary="30-minute buy-stop breakout above the 12-bar high, 8-bar time exit.",
    )
    DEFAULT_PARAMS = {
        "lookback": 12,
        "hold_bars": 8,
        "breakout_buffer": 0.001,
        "expiry_bars": 3,
        "weight": 0.20,
    }

    def reset_state(self) -> None:
        self._bar_count: dict[str, int] = {}
        self._entry_bar: dict[str, int] = {}

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        if pd.isna(nav) or nav <= 0:
            return

        high = self.instruments_data.get_feature("high")
        if date not in high.index:
            return
        pos_idx = high.index.get_loc(date)
        if pos_idx < self.p["lookback"]:
            return

        for inst in self.instruments:
            self._bar_count[inst] = self._bar_count.get(inst, 0) + 1
            bar = bars[inst]
            close = float(bar.close) if not pd.isna(bar.close) else float("nan")
            if pd.isna(close) or close <= 0:
                continue

            pos = current_positions.get(inst, 0.0)

            # Time-based exit.
            if pos > 0:
                held = self._bar_count[inst] - self._entry_bar.get(inst, 0)
                if held >= self.p["hold_bars"]:
                    self._engine().cancel_all(instrument=inst)
                    self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                    self._entry_bar.pop(inst, None)
                continue

            prior_high = high[inst].iloc[pos_idx - self.p["lookback"] : pos_idx].max()
            if pd.isna(prior_high) or prior_high <= 0:
                continue

            shares = int(nav * self.p["weight"] / close)
            if shares > 0:
                self._engine().submit(
                    Order(
                        inst,
                        OrderSide.BUY,
                        shares,
                        OrderType.STOP,
                        stop_price=round(prior_high * (1.0 + self.p["breakout_buffer"]), 2),
                        expires_after_bars=self.p["expiry_bars"],
                    )
                )
                self._entry_bar[inst] = self._bar_count[inst]
