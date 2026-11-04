"""NSE index-derivative reference data: contract identity and historical board lots.

Why this layer exists
---------------------
The provider archive stores option volume and open interest in *share equivalents*.
Turning those into traded contracts therefore requires the board lot that applied to
a specific contract on a specific day - and NSE has changed the NIFTY lot several
times inside the archive window.  At every change the newly listed expiries carried
the new lot while expiries already listed kept the old one, so the official bhavcopy
reports two different lot sizes on the same calendar day: measured on **61 of the 318**
cached official dates (``logs/lot_schedule.txt``), every date from 2024-11-22 to
2025-01-30 carries both 25 and 75, and every date from 2025-10-30 to 2026-01-02 carries
both 75 and 65.

Consequently a lot table keyed only by date is a fallback, never the primary source:
``LotTable.lot_for`` prefers the per-row official value and marks everything else as
inferred.  The date regimes below are the *modal* lot per date measured from the cached
exchange reports, so inside a transition window the fallback is right for the majority
of contracts and wrong for the rest - which is exactly why the fallback carries
``estimated=True``.  For 2021-2023 there is no official report at all (the free archive
begins 2024-01-02), so the lot there is inferred from the provider's own smallest
positive traded volume - a statistic that reproduces the known lot on every official-era
file tested, except on the two files that contain the ``lot+1`` opening-minute artifact.
Nothing in this module reads from disk or the network; the audit scripts supply the
observations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

# (symbol, expiry ISO date, strike, option type) - the identity used across providers.
CONTRACT_KEYS = ("symbol", "expiry", "strike", "option_type")

# Basis labels, ordered from most to least authoritative.
BASIS_OFFICIAL_ROW = "official_bhavcopy_row"
BASIS_DATE_REGIME = "date_regime_fallback"
BASIS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class LotRegime:
    """A board lot that applied to contracts listed between ``observed_first`` and
    ``observed_last``.  The true effective date is bracketed, not known."""

    lot: int
    observed_first: date
    observed_last: date
    provenance: str

    def covers(self, day: date) -> bool:
        return self.observed_first <= day <= self.observed_last


@dataclass(frozen=True)
class ContractLot:
    """One authoritative per-row lot observation from the official exchange report."""

    day: date
    symbol: str
    expiry: str
    strike: int
    option_type: str
    lot: int
    provenance: str = BASIS_OFFICIAL_ROW

    @property
    def key(self) -> tuple[date, str, str, int, str]:
        """Normalised lookup key, so a row stored from a bhavcopy and a query built from
        a provider frame meet on the same representation."""
        return (self.day, str(self.symbol), expiry_from_iso(self.expiry), int(self.strike),
                str(self.option_type).upper())


@dataclass(frozen=True)
class LotResolution:
    """The outcome of one lookup: the lot, how it was obtained, and how far the
    answer may be trusted."""

    lot: int | None
    basis: str
    estimated: bool
    provenance: str

    @property
    def known(self) -> bool:
        return self.lot is not None


def nifty_lot_regimes() -> tuple[LotRegime, ...]:
    """Date regimes measured from cached NSE F&O bhavcopies (``NewBrdLotQty``).

    Each window is the period in which that lot was the *modal* lot on NIFTY option
    rows; the provenance records where another lot was listed alongside it.  Re-derive
    these from ``scripts/_m2_lot_schedule.py`` whenever the cache grows - the boundaries
    are only as tight as the official dates present locally.
    """
    return (
        LotRegime(75, date(2000, 1, 1), date(2021, 11, 30),
                  "NO OFFICIAL ARBITER (bhavcopy archive starts 2024-01-02); inferred "
                  "from the provider's own minimum positive traded volume, which is 75 "
                  "throughout the 2021-05-27 file (logs/lot_schedule.txt)"),
        LotRegime(50, date(2021, 12, 1), date(2024, 4, 29),
                  "official lot 50 on every date from 2024-01-01 to 2024-04-25; before "
                  "that inferred from the provider's minimum positive traded volume, 50 "
                  "in the 2021-12-30, 2022-11-24, 2023-06-22 and 2023-12-28 files"),
        LotRegime(25, date(2024, 4, 30), date(2024, 12, 19),
                  "official lot 25 sole 2024-04-30..2024-11-21, then modal while 75 was "
                  "listed for newly issued expiries from 2024-11-22"),
        LotRegime(75, date(2024, 12, 20), date(2025, 12, 23),
                  "official 75 becomes modal 2024-12-20, sole lot 2025-01-31..2025-10-29, "
                  "then modal alongside 65 from 2025-10-30"),
        LotRegime(65, date(2025, 12, 24), date(2199, 12, 31),
                  "official 65 first listed 2025-10-30, modal from 2025-12-24, sole lot "
                  "from 2026-01-05 and still current on 2026-11-03; also the current lot "
                  "in NSE fo_mktlots.csv"),
    )


class LotTable:
    """Historical board-lot lookup: per-contract official rows first, date regimes
    as a labelled fallback."""

    def __init__(self, regimes: Iterable[LotRegime] | None = None,
                 contract_lots: Iterable[ContractLot] = ()) -> None:
        self.regimes: tuple[LotRegime, ...] = tuple(
            regimes if regimes is not None else nifty_lot_regimes())
        self._by_contract: dict[tuple[date, str, str, int, str], ContractLot] = {}
        for obs in contract_lots:
            self._by_contract[obs.key] = obs

    # -- construction helpers -------------------------------------------------
    def add(self, observation: ContractLot) -> None:
        self._by_contract[observation.key] = observation

    @classmethod
    def from_official_rows(cls, frame, day: date, symbol: str = "NIFTY") -> "LotTable":
        """Build a table from one official NSE F&O bhavcopy frame.

        ``frame`` must carry ``OptnTp``, ``StrkPric``, ``XpryDt`` and ``NewBrdLotQty``.
        Rows whose lot is blank or non-positive are skipped rather than guessed.
        """
        table = cls(contract_lots=())
        for row in frame.itertuples(index=False):
            lot = getattr(row, "NewBrdLotQty", None)
            try:
                lot_int = int(float(lot))
            except (TypeError, ValueError):
                continue
            if lot_int <= 0:
                continue
            table.add(ContractLot(
                day=day, symbol=symbol,
                expiry=expiry_from_iso(getattr(row, "XpryDt")),
                strike=int(round(float(getattr(row, "StrkPric")))),
                option_type=str(getattr(row, "OptnTp")).upper(),
                lot=lot_int,
                provenance=f"{BASIS_OFFICIAL_ROW} {day.isoformat()}"))
        return table

    # -- queries --------------------------------------------------------------
    def lot_on(self, day: date) -> LotResolution:
        """Date-only resolution: the regime that claims the day, if any."""
        matches = [r for r in self.regimes if r.covers(day)]
        if not matches:
            return LotResolution(None, BASIS_UNKNOWN, True,
                                 f"no lot regime covers {day.isoformat()}")
        # Overlapping regimes only ever happen at a bracketed boundary; prefer the
        # regime whose observed window contains the day most tightly.
        best = min(matches, key=lambda r: (r.observed_last - r.observed_first).days)
        return LotResolution(best.lot, BASIS_DATE_REGIME, True, best.provenance)

    def lot_for(self, day: date, symbol: str = "NIFTY", expiry: str | None = None,
                strike: int | None = None, option_type: str | None = None) -> LotResolution:
        """Best available lot for a contract on a day.

        A complete contract identity with an official row is the only non-estimated
        answer; anything else is returned with ``estimated=True`` so downstream
        conversion can record the fact instead of implying certainty.
        """
        if None not in (expiry, strike, option_type):
            obs = self._by_contract.get((day, symbol, expiry_from_iso(expiry),
                                         int(strike), str(option_type).upper()))
            if obs is not None:
                return LotResolution(obs.lot, obs.provenance, False, obs.provenance)
        return self.lot_on(day)

    def observed_lots_on(self, day: date, symbol: str = "NIFTY") -> tuple[int, ...]:
        """Distinct lots seen in official rows for one day - non-empty means the day
        has mixed lots and a date-only conversion would be wrong for some contracts."""
        return tuple(sorted({o.lot for o in self._by_contract.values()
                              if o.day == day and o.symbol == symbol}))


def contract_key(symbol: str, expiry: str, strike: int, option_type: str) -> tuple:
    """Normalised contract identity, independent of provider column order."""
    return (str(symbol), str(expiry)[:10], int(strike), str(option_type).upper())


def expiry_from_iso(value) -> str:
    """Expiry as an ISO *date* string, accepting date, datetime, Timestamp or string.

    ``pandas.Timestamp`` is a subclass of ``datetime``, so ``isinstance(value, date)``
    alone would let ``2025-12-24T00:00:00+05:30`` through as an "ISO date"; the datetime
    case is resolved to its date first.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    # NSE reports render expiry as DD-Mon-YYYY; normalise the few forms seen.
    try:
        return datetime.strptime(text[:11], "%d-%b-%Y").date().isoformat()
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"unparsable expiry {value!r}") from exc
