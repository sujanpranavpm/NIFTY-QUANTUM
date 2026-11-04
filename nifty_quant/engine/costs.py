"""Slippage and commission models.

The three slippage models are ports of the upstream implementations. The
commission layer adds an NSE cash-market model whose components are itemised, so
every rupee of cost in the exports can be traced to a specific statutory charge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from nifty_quant.config import IndiaEquityCosts, SlippageConfig
from nifty_quant.engine.orders import BarData, OrderSide


# --------------------------------------------------------------------------
# Slippage
# --------------------------------------------------------------------------
@runtime_checkable
class SlippageModel(Protocol):
    def compute(self, price: float, quantity: float, side: OrderSide, bar: BarData) -> float:
        """Return the slipped fill price."""


@dataclass(frozen=True)
class NoSlippage:
    def compute(self, price: float, quantity: float, side: OrderSide, bar: BarData) -> float:
        return price


@dataclass(frozen=True)
class FixedBpsSlippage:
    """Constant half-spread in basis points, paid on every fill."""

    bps: float = 5.0

    def compute(self, price: float, quantity: float, side: OrderSide, bar: BarData) -> float:
        spread = abs(price) * self.bps / 10_000.0
        return price + spread if side == OrderSide.BUY else price - spread


@dataclass(frozen=True)
class VolatilitySlippage:
    """Half-spread proportional to the observed bar range."""

    vol_factor: float = 0.1

    def compute(self, price: float, quantity: float, side: OrderSide, bar: BarData) -> float:
        if bar.close == 0 or not np.isfinite(bar.close):
            return price
        bar_range = abs(bar.high - bar.low) / abs(bar.close)
        spread = abs(price) * self.vol_factor * bar_range
        return price + spread if side == OrderSide.BUY else price - spread


def build_slippage(config: SlippageConfig) -> SlippageModel:
    if config.model == "none":
        return NoSlippage()
    if config.model == "fixed_bps":
        return FixedBpsSlippage(bps=config.bps)
    if config.model == "volatility":
        return VolatilitySlippage(vol_factor=config.vol_factor)
    raise ValueError(f"unknown slippage model {config.model!r}")


# --------------------------------------------------------------------------
# Commission
# --------------------------------------------------------------------------
@runtime_checkable
class CommissionModel(Protocol):
    def compute(
        self,
        price: float,
        quantity: float,
        notional: float,
        side: OrderSide | None = None,
    ) -> float:
        """Return the total fee for a fill."""


@dataclass(frozen=True)
class ZeroCommission:
    def compute(self, price: float, quantity: float, notional: float, side: OrderSide | None = None) -> float:
        return 0.0


@dataclass(frozen=True)
class PerShareCommission:
    """Upstream per-share scheme (kept for parity with the original examples)."""

    cost_per_share: float = 0.005
    min_per_order: float = 1.0
    max_pct: float = 0.005

    def compute(self, price: float, quantity: float, notional: float, side: OrderSide | None = None) -> float:
        raw = abs(quantity) * self.cost_per_share
        capped = min(raw, abs(notional) * self.max_pct) if self.max_pct else raw
        return max(capped, self.min_per_order)


@dataclass(frozen=True)
class FixedBpsCommission:
    bps: float = 1.0
    min_per_order: float = 0.0

    def compute(self, price: float, quantity: float, notional: float, side: OrderSide | None = None) -> float:
        return max(abs(notional) * self.bps / 10_000.0, self.min_per_order)


class IndiaEquityCommission:
    """Itemised NSE cash-market cost model.

    Components, all configurable via :class:`IndiaEquityCosts`:

    * brokerage — percentage of turnover, capped at a flat per-order amount
    * STT — delivery: 0.1% on both legs; intraday: 0.025% on the sell leg
    * exchange transaction charge + SEBI turnover fee
    * stamp duty — buy leg only
    * GST — 18% on brokerage + exchange + SEBI
    * DP charge — flat, sell leg only (delivery)

    ``compute`` returns the total, and ``breakdown`` returns the components so
    reports can show exactly where the money went.
    """

    scheme_name = "india_equity"

    def __init__(self, config: IndiaEquityCosts) -> None:
        self.config = config
        self.history: list[dict[str, float]] = []

    def breakdown(
        self,
        price: float,
        quantity: float,
        notional: float,
        side: OrderSide | None,
    ) -> dict[str, float]:
        cfg = self.config
        turnover = abs(notional)
        is_buy = side == OrderSide.BUY
        delivery = cfg.mode == "delivery"

        brokerage = min(turnover * cfg.brokerage_pct, cfg.brokerage_flat)
        exchange = turnover * cfg.exchange_txn_pct
        sebi = turnover * cfg.sebi_turnover_pct

        if delivery:
            stt = turnover * cfg.stt_delivery_pct
            stamp = turnover * cfg.stamp_duty_buy_pct if is_buy else 0.0
            dp = 0.0 if is_buy or quantity <= 0 else cfg.dp_charge_per_sell
        else:
            stt = 0.0 if is_buy else turnover * cfg.stt_intraday_sell_pct
            stamp = turnover * cfg.stamp_duty_intraday_buy_pct if is_buy else 0.0
            dp = 0.0

        gst = (brokerage + exchange + sebi) * cfg.gst_pct
        total = brokerage + stt + exchange + sebi + stamp + gst + dp
        return {
            "brokerage": brokerage,
            "stt": stt,
            "exchange": exchange,
            "sebi": sebi,
            "stamp_duty": stamp,
            "gst": gst,
            "dp_charge": dp,
            "total": total,
        }

    def compute(self, price: float, quantity: float, notional: float, side: OrderSide | None = None) -> float:
        parts = self.breakdown(price, quantity, notional, side)
        self.history.append(parts)
        return parts["total"]


def build_commission(costs: IndiaEquityCosts | None = None, include_costs: bool = True) -> CommissionModel:
    """Build the default commission model for NSE equities."""
    if not include_costs:
        return ZeroCommission()
    return IndiaEquityCommission(costs or IndiaEquityCosts())
