"""Port of upstream ``example_orders_19_fx_momentum_lots.py``.

Idea (verbatim): trade four USD-quoted spot-FX pairs in the direction of
six-month momentum. Position size is an integer number of standard lots,
constrained by ATR risk and a per-pair notional cap.

This port exists so the library of twenty stays complete and auditable. It is
NOT run against NIFTY 50 equities: the sizing unit is a 100,000-base-currency
FX lot supplied by the upstream contract service, and the accounting upstream
uses deliberately ignores FX margin, swap/financing, leverage and bid/ask. Those
assumptions have no NSE cash-equity analogue, so replacing them with something
equity-shaped would be inventing a different strategy.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nifty_quant.strategies.base import OrderStrategy, StrategySpec
from nifty_quant.strategies.contracts import (
    ContractSpec,
    average_true_range,
    resolve_contract_spec,
    size_standard_lots,
)


class FXMomentumLots(OrderStrategy):
    """Momentum strategy using API-provided spot-FX lot specifications."""

    SPEC = StrategySpec(
        strategy_id="orders_19",
        name="FX Momentum (Standard Lots)",
        upstream_file="strategies/example_orders_19_fx_momentum_lots.py",
        family="cross_asset_momentum",
        timeframe="1d",
        order_types=("market",),
        entry_logic="Each bar: direction = sign(close - close 126 bars ago); target = direction x whole lots sized by ATR risk and a 20% notional cap.",
        exit_logic="No discrete exit; the position is reversed or resized whenever the target changes, via a signed market delta order.",
        risk_logic="0.5% of NAV at risk per lot-distance move in ATR, with a hard 20% notional ceiling per pair.",
        required_data=("daily FX OHLC", "per-instrument contract multiplier and lot size"),
        default_weight=0.20,
        published_max_position_size=0.20,
        direction="long_short",
        run_mode="portfolio",
        applicable_to_nse_equity=False,
        adaptation_notes=(
            "Not adapted. NIFTY 50 constituents are cash equities with no lot unit, no perpetual "
            "direction flip, and no FX-style contract multiplier; upstream also states its "
            "accounting ignores FX margin, swap and bid/ask, so any rupee translation would "
            "measure a different thing. Retained here for completeness and to document why it is "
            "excluded from the NSE results."
        ),
        source_summary="Six-month FX momentum, ATR/notional-capped whole lots, market orders.",
    )
    DEFAULT_PARAMS = {
        "lookback": 126,
        "atr_window": 20,
        "risk_fraction": 0.005,
        "notional_cap": 0.20,
    }

    def extra_params(self) -> dict:
        return {"contract_specs": {}}

    def reset_state(self) -> None:
        self._atr_cache: pd.DataFrame | None = None

    def _contract_spec(self, instrument: str) -> ContractSpec:
        return resolve_contract_spec(self.p["contract_specs"], instrument)

    def _compute_orders(self, date, bars, current_positions, nav) -> None:
        # pandas frames: the ATR helper below is a panel-wide rolling computation.
        close = self.instruments_data.get_frame("adj_close")
        if date not in close.index:
            return
        index = close.index.get_loc(date)
        if not isinstance(index, (int, np.integer)) or index < self.p["lookback"]:
            return

        high = self.instruments_data.get_frame("high")
        low = self.instruments_data.get_frame("low")
        if self._atr_cache is None or not self._atr_cache.index.equals(close.index):
            self._atr_cache = average_true_range(high, low, close, self.p["atr_window"])
        atr = self._atr_cache

        for instrument in self.instruments:
            if self._has_pending(instrument):
                continue
            current = float(current_positions.get(instrument, 0.0))
            current_price = float(bars[instrument].close)
            past_price = close[instrument].iloc[index - self.p["lookback"]]
            current_atr = atr.loc[date, instrument]
            if pd.isna(past_price) or past_price <= 0.0 or pd.isna(current_atr):
                continue

            direction = 1 if current_price > past_price else -1
            spec = self._contract_spec(instrument)
            lots = size_standard_lots(
                nav=float(nav),
                price=current_price,
                atr=float(current_atr),
                multiplier=float(spec.multiplier),
                lot_size=float(spec.lot_size),
                risk_fraction=self.p["risk_fraction"],
                notional_cap=self.p["notional_cap"],
            )
            target = float(direction * lots)
            delta = target - current
            if abs(delta) > 1e-12:
                self.order_market(instrument, delta, tag="fx_momentum_target")
