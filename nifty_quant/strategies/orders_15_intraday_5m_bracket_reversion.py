"""Port of upstream ``example_orders_15_intraday_5m_bracket_reversion.py``.

Idea (verbatim): on 5-minute bars, buy oversold RSI(14) dips and wrap each entry
in a tight intraday bracket (+0.6% take-profit / -0.4% stop-loss).

Upstream notes the 5m cadence is the sweet spot for intraday mean reversion:
long enough for RSI to be meaningful, short enough for several round-trips per
session, with the bracket making reward/risk explicit per trade.

Data caveat: free 5-minute history is short and provider-dependent. Upstream
itself only requests ~30 calendar days because yfinance caps 5m history. This
strategy is therefore reported on its own short intraday window, never mixed
into the multi-year daily results.
"""

from __future__ import annotations

import pandas as pd

from nifty_quant.engine.orders import BracketSpec, Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class IntradayBracketReversion5m(OrderStrategy):
    """5-minute oversold-RSI entries with a tight bracket exit."""

    SPEC = StrategySpec(
        strategy_id="orders_15",
        name="Intraday 5m Bracket Reversion",
        upstream_file="strategies/example_orders_15_intraday_5m_bracket_reversion.py",
        family="intraday_mean_reversion",
        timeframe="5m",
        order_types=("bracket",),
        entry_logic="5-minute RSI(14) below 30 while flat and no bracket already live: BRACKET buy at 20% of NAV.",
        exit_logic="Take-profit limit at +0.6% and stop-loss at -0.4% of the signal-bar close, as an OCO pair. There is no time exit; a position can carry overnight if neither leg triggers.",
        risk_logic="1.5:1 reward/risk per trade, very tight in absolute terms, so the NSE cost model dominates the edge.",
        required_data=("5-minute OHLCV", "RSI14 on 5-minute closes"),
        default_weight=0.20,
        published_max_position_size=0.25,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "Logic unchanged, but NSE intraday equity trades cannot carry overnight in a real "
            "account. The backtest deliberately does not add a session-close flat rule because "
            "that would change the strategy; the exit report flags every position carried past "
            "the session end so the mismatch is visible instead of hidden. Free 5m history is "
            "limited to roughly 30-60 days, so results are not comparable with daily strategies."
        ),
        source_summary="5-minute RSI(14) < 30 entry with a +0.6%/-0.4% bracket.",
    )
    DEFAULT_PARAMS = {
        "rsi_period": 14,
        "entry_rsi": 30.0,
        "take_profit_pct": 0.006,
        "stop_loss_pct": 0.004,
        "weight": 0.20,
    }

    def reset_state(self) -> None:
        self._has_bracket: dict[str, bool] = {}

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

            if pos == 0 and self._has_bracket.get(inst, False):
                self._has_bracket[inst] = False

            if value < self.p["entry_rsi"] and pos == 0 and not self._has_bracket.get(inst, False):
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
