"""Port of upstream ``example_orders_20_futures_donchian_contracts.py``.

Idea (verbatim): a diversified 55-day Donchian breakout across index, energy and
metal futures. Orders are whole contracts sized by ATR risk, gross notional and
the reference initial margin carried in ContractSpec.

This port exists so the library of twenty stays complete and auditable. It is
NOT run against NIFTY 50 equities: upstream notes that the engine does not
select dated contracts, control rolls, round fills to ticks, post variation
margin or enforce broker buying power, and that cash accounting uses full
notional while margin is only a sizing cap. Those are futures-specific
mechanics, and a single volatile cash equity is not a substitute for them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nifty_quant.strategies.base import OrderStrategy, StrategySpec
from nifty_quant.strategies.contracts import (
    ContractSpec,
    average_true_range,
    resolve_contract_spec,
    size_futures_contracts,
)


class FuturesDonchianContracts(OrderStrategy):
    """Donchian breakout using API-provided continuous-futures specs."""

    SPEC = StrategySpec(
        strategy_id="orders_20",
        name="Futures Donchian (Contracts)",
        upstream_file="strategies/example_orders_20_futures_donchian_contracts.py",
        family="cross_asset_breakout",
        timeframe="1d",
        order_types=("market",),
        entry_logic="Long when close exceeds the prior 55-bar high, short when it breaks the prior 55-bar low, otherwise hold the current sign; target sized in whole contracts.",
        exit_logic="No stop; the position flips or resizes when price leaves the Donchian channel or the size limits change.",
        risk_logic="0.5% NAV ATR risk, 25% notional cap and 15% margin cap, all applied simultaneously.",
        required_data=("daily futures OHLC", "contract multiplier", "reference initial margin"),
        default_weight=0.25,
        published_max_position_size=0.25,
        direction="long_short",
        run_mode="portfolio",
        applicable_to_nse_equity=False,
        adaptation_notes=(
            "Not adapted. Roll handling, tick rounding, variation margin and continuous-contract "
            "construction are all absent from this engine and from upstream; substituting NIFTY 50 "
            "cash equities would silently drop the short leg (which needs an F&O account) and the "
            "contract multiplier. Retained here for completeness and to document the exclusion."
        ),
        source_summary="55-day Donchian breakout on continuous futures, contract-sized market orders.",
    )
    DEFAULT_PARAMS = {
        "breakout_window": 55,
        "atr_window": 20,
        "risk_fraction": 0.005,
        "notional_cap": 0.25,
        "margin_cap": 0.15,
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
        if not isinstance(index, (int, np.integer)) or index < self.p["breakout_window"]:
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
            prior_high = high[instrument].iloc[index - self.p["breakout_window"] : index].max()
            prior_low = low[instrument].iloc[index - self.p["breakout_window"] : index].min()
            current_atr = atr.loc[date, instrument]
            if pd.isna(prior_high) or pd.isna(prior_low) or pd.isna(current_atr):
                continue

            if current_price > prior_high:
                direction = 1
            elif current_price < prior_low:
                direction = -1
            else:
                direction = int(np.sign(current))

            spec = self._contract_spec(instrument)
            contracts = size_futures_contracts(
                nav=float(nav),
                price=current_price,
                atr=float(current_atr),
                multiplier=float(spec.multiplier),
                margin=float(spec.margin),
                risk_fraction=self.p["risk_fraction"],
                notional_cap=self.p["notional_cap"],
                margin_cap=self.p["margin_cap"],
            )
            target = float(direction * contracts)
            delta = target - current
            if abs(delta) > 1e-12:
                self.order_market(instrument, delta, tag="futures_donchian_target")
