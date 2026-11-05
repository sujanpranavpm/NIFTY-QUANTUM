"""Options-specific transaction cost engine (P2 stage 5, directive Part 6).

A long index-option trade (buy CE or PE, exit by sell or expiry) is charged on the
*premium* turnover ``premium x lot_size x lots``, with the entry and exit legs costed
separately so a partial exit is prorated correctly.  The statutory components mirror the
equity model but the rules differ: STT on options falls on the premium **seller** (the
exit of a long), stamp duty on a derivatives transaction falls on the **buyer** (the
entry), there is no DP charge, and exchange transaction charge is assessed **per traded
lot** rather than on turnover.

Honesty contract (locked directive decision)
--------------------------------------------
No cached NSE circular or contract spec in this repository carries derivative charge
rates (``scripts/_p2_cost_schedule_audit.py`` proves that against the cached PDFs and
``contract_specs.html``).  Every statutory rate therefore enters the schedule flagged
``estimated=True`` with a provenance that says so, and :meth:`assess` reports
``strict_verifiable=False`` for any trade that used such a rate.  A cost figure is
produced for modelling, but it is never presented as a verified financial fact.

Brokerage is *configurable and assumed nothing*: the neutral default is zero, so no
provider is charged unless the user configures one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nifty_quant.config import IndiaOptionsCosts, OptionsChargeRegime
from nifty_quant.engine.orders import OrderSide

#: The statutory turnover-based components charged on premium (per_lot handled apart).
PREMIUM_PCT_COMPONENTS = ("stt", "sebi", "stamp_duty")


class UnknownChargeError(ValueError):
    """No effective-dated rate covers the requested component and date."""


def _iso(day) -> str:
    return str(day)[:10]


def resolve_rate(regimes, component: str, on_date) -> OptionsChargeRegime:
    """The regime for ``component`` in force on ``on_date`` (latest effective start wins)."""
    day = _iso(on_date)
    covering = [r for r in regimes
                if r.component == component
                and r.effective_from <= day
                and (r.effective_to is None or day <= r.effective_to)]
    if not covering:
        raise UnknownChargeError(
            f"no effective-dated {component!r} rate covers {day}")
    return max(covering, key=lambda r: r.effective_from)


@dataclass(frozen=True)
class OptionsCostResult:
    """A costed option leg: itemised components, total, and the strictness verdict."""

    side: str
    on_date: str
    transaction_value: float
    components: dict[str, float]
    total: float
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def estimated_components(self) -> tuple[str, ...]:
        return tuple(k for k, v in self.provenance.items() if v.startswith("estimated"))

    @property
    def strict_verifiable(self) -> bool:
        """True only when every statutory rate used was independently sourced."""
        return not any(v.startswith("estimated") for v in self.provenance.values())


class IndiaOptionsCommission:
    """Effective-dated, itemised cost model for long index-option trades."""

    scheme_name = "india_options"

    def __init__(self, config: IndiaOptionsCosts | None = None) -> None:
        self.config = config or IndiaOptionsCosts()
        self.history: list[OptionsCostResult] = []

    # -- brokerage ---------------------------------------------------------
    def _brokerage(self, turnover: float) -> float:
        b = self.config.brokerage
        if b.model == "none":
            return 0.0
        if b.model == "flat":
            return b.flat_per_order
        if b.model == "percentage":
            raw = turnover * b.percentage
            if b.cap_percentage:
                raw = min(raw, turnover * b.cap_percentage)
            return raw
        raise ValueError(f"unknown brokerage model {b.model!r}")

    # -- public API --------------------------------------------------------
    def breakdown(self, premium: float, lot_size: int, lots: int, side: OrderSide,
                  on_date) -> dict[str, float]:
        """Itemised charges for one leg.  Returns components only - never a ``total``.

        ``on_date`` selects the effective-dated statutory rates; it must be a trading
        date the schedule covers, or :class:`UnknownChargeError` is raised (fail-closed,
        no silent fallback to a different year's rate).
        """
        if lots <= 0 or lot_size <= 0:
            raise ValueError("lot_size and lots must be positive")
        turnover = abs(premium) * lot_size * lots
        is_buy = side == OrderSide.BUY
        day = _iso(on_date)

        brokerage = self._brokerage(turnover)
        exchange = resolve_rate(self.config.regimes, "exchange_txn", day).value * lots
        sebi_rate = resolve_rate(self.config.regimes, "sebi", day)
        sebi = turnover * sebi_rate.value

        stt_rate = resolve_rate(self.config.regimes, "stt", day)
        stt = turnover * stt_rate.value if stt_rate.side in ("both", "sell") and not is_buy else 0.0
        stamp_rate = resolve_rate(self.config.regimes, "stamp_duty", day)
        stamp = turnover * stamp_rate.value if stamp_rate.side in ("both", "buy") and is_buy else 0.0

        gst = (brokerage + exchange + sebi) * self.config.gst_pct
        slippage = turnover * self.config.slippage_pct
        return {
            "brokerage": brokerage,
            "stt": stt,
            "exchange": exchange,
            "sebi": sebi,
            "stamp_duty": stamp,
            "gst": gst,
            "slippage": slippage,
        }

    def _provenance(self, on_date) -> dict[str, str]:
        day = _iso(on_date)
        out: dict[str, str] = {}
        for component in ("stt", "exchange_txn", "sebi", "stamp_duty"):
            rate = resolve_rate(self.config.regimes, component, day)
            tag = "estimated" if rate.estimated else "sourced"
            out[component] = f"{tag}: {rate.provenance or 'no provenance recorded'}"
        b = self.config.brokerage
        out["brokerage"] = (f"configurable: brokerage model {b.model!r} (assumed, not a "
                            f"statutory rate)")
        return out

    def assess(self, premium: float, lot_size: int, lots: int, side: OrderSide,
               on_date) -> OptionsCostResult:
        """Full cost verdict for one leg: components, total, provenance and strictness."""
        components = self.breakdown(premium, lot_size, lots, side, on_date)
        total = sum(components.values())
        result = OptionsCostResult(
            side="buy" if side == OrderSide.BUY else "sell",
            on_date=_iso(on_date),
            transaction_value=abs(premium) * lot_size * lots,
            components=components,
            total=total,
            provenance=self._provenance(on_date),
        )
        self.history.append(result)
        return result

    def compute(self, premium: float, lot_size: int, lots: int, side: OrderSide,
                on_date) -> float:
        """Total charge for one leg (mirrors :class:`IndiaEquityCommission.compute`)."""
        return self.assess(premium, lot_size, lots, side, on_date).total

    def round_trip(self, entry_premium: float, exit_premium: float, lot_size: int,
                   lots: int, entry_date, exit_date) -> dict:
        """Cost an entry (buy) + exit (sell) and report the strict-verifiable verdict."""
        entry = self.assess(entry_premium, lot_size, lots, OrderSide.BUY, entry_date)
        exit_ = self.assess(exit_premium, lot_size, lots, OrderSide.SELL, exit_date)
        return {
            "entry": entry,
            "exit": exit_,
            "total_costs": entry.total + exit_.total,
            "strict_verifiable": entry.strict_verifiable and exit_.strict_verifiable,
        }
