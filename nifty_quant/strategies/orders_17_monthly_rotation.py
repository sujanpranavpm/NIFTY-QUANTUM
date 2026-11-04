"""Port of upstream ``example_orders_17_monthly_rotation_orders.py``.

Idea (verbatim): an event-driven monthly rotation executed with explicit orders.
On the first bar of each new month, rank the universe by 6-month momentum, then
sell names that dropped out of the top set and buy the new entrants at market.

This is the only one of the twenty examples that is inherently cross-sectional:
the target set depends on the whole universe, so it must run as one portfolio
rather than as independent per-stock sleeves.
"""

from __future__ import annotations

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class MonthlyRotationOrders(OrderStrategy):
    """Event-driven monthly momentum rotation in order mode."""

    SPEC = StrategySpec(
        strategy_id="orders_17",
        name="Monthly Rotation",
        upstream_file="strategies/example_orders_17_monthly_rotation_orders.py",
        family="cross_sectional_momentum",
        timeframe="1d",
        order_types=("market",),
        entry_logic="On the first bar of each new month, rank the universe by 126-bar momentum on adjusted close and buy the top 3 names at an equal share of NAV.",
        exit_logic="Same event: any held name that fell out of the top 3 is sold at market.",
        risk_logic="Concentration risk: only 3 names, re-ranked monthly, with no stop-loss at all.",
        required_data=("daily adjusted close", "point-in-time universe membership"),
        default_weight=1.0 / 3.0,
        published_max_position_size=0.40,
        direction="long_only",
        run_mode="portfolio",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "Logic unchanged, three documented differences in execution context. (1) It runs in "
            "portfolio mode because the ranking is cross-sectional. (2) Iteration over the target "
            "set is sorted: upstream iterates a Python set, whose order is not guaranteed across "
            "runs, which would break reproducible backtests. (3) The NIFTY 50 membership snapshot "
            "is current, not point-in-time, so a name promoted to the index recently is ranked "
            "over its pre-membership history. That survivorship path is reported, not corrected."
        ),
        source_summary="Monthly top-3 six-month momentum rotation over the whole universe, market orders.",
    )
    DEFAULT_PARAMS = {"lookback": 126, "top_n": 3}

    def reset_state(self) -> None:
        self._last_month: tuple[int, int] | None = None

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        # Rebalance event = first bar of a new month.
        month_key = (date.year, date.month)
        if month_key == self._last_month:
            return
        self._last_month = month_key

        close = self.instruments_data.get_feature("adj_close")
        if date not in close.index:
            return
        pos_idx = close.index.get_loc(date)
        if pos_idx < self.p["lookback"]:
            return

        momentum = close.iloc[pos_idx] / close.iloc[pos_idx - self.p["lookback"]] - 1.0
        target = set(momentum.dropna().nlargest(self.p["top_n"]).index)

        # Sell names that fell out of the target set.
        for inst in self.instruments:
            pos = current_positions.get(inst, 0.0)
            if pos > 0 and inst not in target:
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))

        # Buy new entrants at an equal share of NAV. Sorted for reproducibility.
        per_name_nav = nav / max(1, len(target))
        for inst in sorted(target):
            pos = current_positions.get(inst, 0.0)
            if pos == 0:
                shares = int(per_name_nav / bars[inst].close)
                if shares > 0:
                    self._engine().submit(Order(inst, OrderSide.BUY, shares, OrderType.MARKET))

    def describe_state(self, date) -> dict:
        return {"rebalance_month": self._last_month, "top_n": self.p["top_n"]}
