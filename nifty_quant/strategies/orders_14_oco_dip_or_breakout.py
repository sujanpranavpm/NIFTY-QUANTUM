"""Port of upstream ``example_orders_14_oco_dip_or_breakout.py``.

Idea (verbatim): submit two competing entry orders: buy a dip or buy a breakout.
The limit-buy leg sits 2% below the close, the stop-buy leg 2% above. Whichever
fills first cancels the other leg.

The pair is only submitted every fifth bar, and each leg expires after five bars.
"""

from __future__ import annotations

from nifty_quant.engine.orders import Order, OrderSide, OrderType
from nifty_quant.strategies.base import OrderStrategy, StrategySpec


class OCODipOrBreakout(OrderStrategy):
    """OCO entry pair: mean-reversion dip versus momentum breakout."""

    SPEC = StrategySpec(
        strategy_id="orders_14",
        name="OCO Dip Or Breakout",
        upstream_file="strategies/example_orders_14_oco_dip_or_breakout.py",
        family="hybrid",
        timeframe="1d",
        order_types=("oco", "market"),
        entry_logic="Every 5th bar while flat and no live pair: submit a BUY LIMIT 2% below the close and a BUY STOP 2% above it under one OCO pair id, each GTD 5 bars.",
        exit_logic="Fixed 10-bar hold from the entry bar, then cancel everything and sell at market.",
        risk_logic="Direction-agnostic entry; the OCO guarantees only one leg can fill. No protective stop, so risk is the 10-bar holding window.",
        required_data=("daily OHLCV",),
        default_weight=0.10,
        published_max_position_size=0.20,
        direction="long_only",
        run_mode="per_stock",
        applicable_to_nse_equity=True,
        adaptation_notes=(
            "No logic change. Both legs are BUY legs sharing one OCO pair id, which is how "
            "upstream expresses the bracket; the fill engine cancels the sibling on the first "
            "fill. NSE daily price bands can leave the stop leg unfilled on limit-up sessions."
        ),
        source_summary="OCO dip/breakout entry every 5 bars, 10-bar time exit.",
    )
    DEFAULT_PARAMS = {
        "entry_every_n_bars": 5,
        "dip_pct": 0.02,
        "breakout_pct": 0.02,
        "hold_bars": 10,
        "expiry_bars": 5,
        "weight": 0.10,
    }

    def reset_state(self) -> None:
        self._bar_count: dict[str, int] = {}
        self._entry_bar: dict[str, int] = {}
        self._oco_live: dict[str, bool] = {}

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        for inst in self.instruments:
            self._bar_count[inst] = self._bar_count.get(inst, 0) + 1
            bar = bars[inst]
            pos = current_positions.get(inst, 0.0)

            if pos > 0 and self._oco_live.get(inst, False):
                self._oco_live[inst] = False
                self._entry_bar[inst] = self._bar_count[inst]

            if pos == 0:
                self._entry_bar.pop(inst, None)
                if self._oco_live.get(inst, False) and not self._has_pending(inst):
                    self._oco_live[inst] = False

            if pos > 0 and self._bar_count[inst] - self._entry_bar.get(inst, 0) >= self.p["hold_bars"]:
                self._engine().cancel_all(instrument=inst)
                self._engine().submit(Order(inst, OrderSide.SELL, pos, OrderType.MARKET))
                self._entry_bar.pop(inst, None)
                continue

            if pos == 0 and not self._oco_live.get(inst, False):
                if self._bar_count[inst] % self.p["entry_every_n_bars"] != 0:
                    continue

                shares = int(nav * self.p["weight"] / bar.close)
                if shares <= 0:
                    continue

                oco_id = f"{inst}_{date:%Y%m%d}_dip_or_breakout"
                self._engine().submit(
                    Order(
                        inst,
                        OrderSide.BUY,
                        shares,
                        OrderType.OCO,
                        limit_price=round(bar.close * (1.0 - self.p["dip_pct"]), 2),
                        oco_pair_id=oco_id,
                        expires_after_bars=self.p["expiry_bars"],
                    )
                )
                self._engine().submit(
                    Order(
                        inst,
                        OrderSide.BUY,
                        shares,
                        OrderType.OCO,
                        stop_price=round(bar.close * (1.0 + self.p["breakout_pct"]), 2),
                        oco_pair_id=oco_id,
                        expires_after_bars=self.p["expiry_bars"],
                    )
                )
                self._oco_live[inst] = True

    def describe_state(self, date) -> dict:
        return {"oco_live": dict(self._oco_live), "bars_elapsed": dict(self._bar_count)}
