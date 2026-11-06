"""Options-specific transaction cost engine (P2 stage 5; P2.1 / P2.1.2 accuracy audits).

A long index-option trade (buy CE or PE, exit by sell or expiry) is charged on the
*premium* turnover ``premium x lot_size x lots``, with the entry and exit legs costed
separately so a partial exit is prorated correctly.  The statutory rules differ from the
equity model: STT on options falls on the premium **seller** (the exit of a long), stamp
duty on a derivatives trade falls on the **buyer** (the entry), there is no DP charge,
and the exchange transaction charge and the NSE IPFT contribution are each levied on the
**traded premium, each side** - never as a fabricated flat per-lot fee.

P2.1 correction
---------------
The earlier model charged a made-up ``Rs 20 per lot`` exchange fee.  That was wrong: NSE
levies equity-option transaction charges (and the IPFT contribution) *per crore of traded
premium, each side* (NSE circular FA73061, wef 2026-03-01: Rs 3,552.99 + Rs 0.01).  The
schedule in :func:`nifty_quant.config.default_options_charge_regimes` now carries these
premium-turnover rates, effective-dated and each read from an official document.

P2.1.2 correction
-----------------
FA73061 cites NSE/FA/64323 for the Sept-27-2024 uniform-rate revision, but that PDF
returns HTTP 404; that is a circular-reference mismatch, likely a typographical error.
The official primary document located is **NSE/FA/64232** (dated September 27, 2024),
matching the subject and effective charges referenced by FA73061.  With the real
circular now cached at Rs 0, the 2024-10-01 exchange
transaction row is *verified* (Rs 35.03 each side per lakh of premium, wef Oct 1 2024).  The
pre-Apr-2023 slab VALUES and effective date are now also sourced (NSE/FA/46730), but slab
-tiers remain a modelling assumption (the applicable marginal tier depends on member monthly
turnover, which a research book does not have), so the two slab-era rows stay estimated.

Honesty contract
----------------
A rate that could not be read from an official source is flagged ``estimated=True`` in
the schedule, and :meth:`assess` reports ``statutory_verified=False`` for any trade that
used such a rate (fail-closed).  The result distinguishes two verification levels that
must not be conflated: ``statutory_verified`` (every statutory rate sourced, brokerage
irrelevant) and ``all_in_verified`` (statutory_verified *and* brokerage explicitly
attested against a broker tariff) - a *brokerage-excluded* trade is **never** all-in
verified because the broker fee is a real trading cost that has simply been omitted.
``strict_verifiable`` is retained for backward compatibility and means "safe to publish
as a verified financial fact in this configuration": statutory_verified *and* either
brokerage excluded or brokerage verified.  GST is a configurable tax knob applied to
the taxable fee base, not a strictness-gating statutory rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nifty_quant.config import IndiaOptionsCosts, OptionsChargeRegime
from nifty_quant.engine.orders import OrderSide

#: The statutory turnover-based components charged on premium.
PREMIUM_PCT_COMPONENTS = ("stt", "sebi", "stamp_duty", "ipft", "exchange_txn")


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
    brokerage_excluded: bool = False   # True when brokerage model is 'none'
    brokerage_verified: bool = False   # True when the configured broker fee is attested
    assumptions: tuple[str, ...] = ()  # human-readable modelling assumptions for this leg

    @property
    def estimated_components(self) -> tuple[str, ...]:
        """Statutory components whose rate was not read from an official source."""
        return tuple(k for k, v in self.provenance.items()
                     if k != "brokerage" and v.startswith("estimated"))

    @property
    def statutory_verified(self) -> bool:
        """True only when every statutory rate used was independently sourced.

        Does **not** require brokerage to be verified or excluded - this is the pure
        statutory-levy verification level (P2.1.2 directive §3).
        """
        return not any(v.startswith("estimated")
                       for k, v in self.provenance.items() if k != "brokerage")

    @property
    def all_in_verified(self) -> bool:
        """True only when *statutory_verified* **and** the broker fee is attested.

        Brokerage exclusion (``model='none'``) is deliberately NOT all-in verification -
        the broker fee is a real cost that has been omitted, not verified.  Use this
        flag when reporting a *fully net* trading cost; a False result with
        ``statutory_verified=True`` means the statutory side is sourced but the
        brokerage is either excluded or unverified.
        """
        return self.statutory_verified and self.brokerage_verified

    @property
    def cost_basis(self) -> str:
        """Human-readable reporting label - never says 'net' when brokerage is excluded."""
        if not self.statutory_verified:
            return "modelling only (some statutory rates estimated)"
        if self.brokerage_verified:
            return "all-in verified (statutory sourced + brokerage attested)"
        if self.brokerage_excluded:
            return ("statutory-only (brokerage excluded; NOT fully all-in net - "
                    "do not label as net profit)")
        return "statutory sourced; brokerage configured but unverified"

    @property
    def strict_verifiable(self) -> bool:
        """Backward-compat verdict: safe to publish as a verified financial fact in
        this configuration (statutory sourced AND brokerage either excluded or verified).
        Distinct from :attr:`all_in_verified`, which additionally requires the broker
        fee to be attested (a brokerage-excluded trade is never fully all-in)."""
        return self.statutory_verified and (self.brokerage_excluded or self.brokerage_verified)


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
    def _gst_pct(self, day: str) -> float:
        """GST rate: the schedule's effective-dated ``gst`` row if present, else config."""
        try:
            return resolve_rate(self.config.regimes, "gst", day).value
        except UnknownChargeError:
            return self.config.gst_pct

    def breakdown(self, premium: float, lot_size: int, lots: int, side: OrderSide,
                  on_date) -> dict[str, float]:
        """Itemised charges for one leg.  Returns components only - never a ``total``.

        ``on_date`` selects the effective-dated statutory rates; it must be a trading
        date the schedule covers, or :class:`UnknownChargeError` is raised (fail-closed,
        no silent fallback to a different year's rate).  The exchange transaction charge
        and the NSE IPFT contribution are assessed on the *premium turnover*, each side.
        """
        if lots <= 0 or lot_size <= 0:
            raise ValueError("lot_size and lots must be positive")
        turnover = abs(premium) * lot_size * lots
        is_buy = side == OrderSide.BUY
        day = _iso(on_date)

        brokerage = self._brokerage(turnover)

        # Exchange transaction charge on premium turnover; a legacy per-lot path is kept
        # only so an outdated schedule cannot silently masquerade as turnover-based.
        exch_rate = resolve_rate(self.config.regimes, "exchange_txn", day)
        exchange = (exch_rate.value * lots) if exch_rate.rate_type == "per_lot" \
            else turnover * exch_rate.value

        # NSE IPFT contribution on premium turnover, each side.
        ipft_rate = resolve_rate(self.config.regimes, "ipft", day)
        ipft = turnover * ipft_rate.value

        sebi_rate = resolve_rate(self.config.regimes, "sebi", day)
        sebi = turnover * sebi_rate.value

        stt_rate = resolve_rate(self.config.regimes, "stt", day)
        stt = turnover * stt_rate.value if stt_rate.side in ("both", "sell") and not is_buy else 0.0
        stamp_rate = resolve_rate(self.config.regimes, "stamp_duty", day)
        stamp = turnover * stamp_rate.value if stamp_rate.side in ("both", "buy") and is_buy else 0.0

        # GST on the taxable fee base: brokerage + exchange txn + IPFT + SEBI.
        gst = (brokerage + exchange + ipft + sebi) * self._gst_pct(day)
        slippage = turnover * self.config.slippage_pct
        return {
            "brokerage": brokerage,
            "stt": stt,
            "exchange": exchange,
            "ipft": ipft,
            "sebi": sebi,
            "stamp_duty": stamp,
            "gst": gst,
            "slippage": slippage,
        }

    def _provenance(self, on_date) -> dict[str, str]:
        day = _iso(on_date)
        out: dict[str, str] = {}
        for component in ("stt", "exchange_txn", "ipft", "sebi", "stamp_duty"):
            rate = resolve_rate(self.config.regimes, component, day)
            tag = "estimated" if rate.estimated else "sourced"
            out[component] = f"{tag}: {rate.provenance or 'no provenance recorded'}"
        b = self.config.brokerage
        if b.model == "none":
            out["brokerage"] = ("configurable: brokerage EXCLUDED (model='none'); no "
                                "broker fee attributed")
        else:
            status = "verified" if b.verified else "UNVERIFIED"
            out["brokerage"] = (f"configurable: brokerage model {b.model!r} ({status}); "
                                f"broker-specific fee, not a statutory rate")
        return out

    def assess(self, premium: float, lot_size: int, lots: int, side: OrderSide,
               on_date) -> OptionsCostResult:
        """Full cost verdict for one leg: components, total, provenance and strictness."""
        components = self.breakdown(premium, lot_size, lots, side, on_date)
        total = sum(components.values())
        day = _iso(on_date)
        b = self.config.brokerage
        excluded = b.model == "none"
        verified = bool(b.verified) and not excluded
        assumptions: list[str] = []
        if excluded:
            assumptions.append("brokerage excluded (model='none'); no broker fee attributed")
        elif verified:
            assumptions.append("brokerage configured and user-verified against the broker tariff")
        else:
            assumptions.append("brokerage configured but NOT verified -> excluded from strict validation")
        assumptions.append(
            f"GST applied at {self._gst_pct(day):.6g} on the taxable fee base (config tax knob)")
        if self.config.slippage_pct:
            assumptions.append(
                f"slippage haircut {self.config.slippage_pct:.6g} of turnover applied (config)")
        result = OptionsCostResult(
            side="buy" if side == OrderSide.BUY else "sell",
            on_date=day,
            transaction_value=abs(premium) * lot_size * lots,
            components=components,
            total=total,
            provenance=self._provenance(on_date),
            brokerage_excluded=excluded,
            brokerage_verified=verified,
            assumptions=tuple(assumptions),
        )
        self.history.append(result)
        return result

    def compute(self, premium: float, lot_size: int, lots: int, side: OrderSide,
                on_date) -> float:
        """Total charge for one leg (mirrors :class:`IndiaEquityCommission.compute`)."""
        return self.assess(premium, lot_size, lots, side, on_date).total

    def exercise_stt(self, intrinsic_value: float, on_date) -> float:
        """Reference-only STT on an *exercised* option (purchaser, on intrinsic value).

        The execution engine exits a long at the premium sale and has no exercise path, so
        this is provided for completeness and auditability (the schedule carries verified
        ``stt_exercised`` rows) and is deliberately NOT folded into :meth:`breakdown` or
        :meth:`assess`.  Intrinsic value = settlement price - strike price.
        """
        rate = resolve_rate(self.config.regimes, "stt_exercised", _iso(on_date))
        return abs(intrinsic_value) * rate.value

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
            "statutory_verified": entry.statutory_verified and exit_.statutory_verified,
            "all_in_verified": entry.all_in_verified and exit_.all_in_verified,
            "brokerage_excluded": entry.brokerage_excluded and exit_.brokerage_excluded,
            "cost_basis": entry.cost_basis if entry.cost_basis == exit_.cost_basis
                          else f"mixed: entry [{entry.cost_basis}] / exit [{exit_.cost_basis}]",
        }
