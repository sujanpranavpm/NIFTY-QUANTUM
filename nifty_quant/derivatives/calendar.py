"""Effective-dated NIFTY reference database: calendar, expiries, lots, ticks, strikes.

Why this layer exists
---------------------
A research pipeline that asks "what was the lot size / tick / expiry weekday on
2023-04-12?" must answer from records that were true *then*, not from today's
specification.  NSE changed the NIFTY board lot five times inside the archive
window and moved weekly expiry from Thursday to Tuesday on 2025-09-01
(NSE/FAOP/68747), so every lookup here is effective-dated: each record carries
``effective_from``, ``effective_to``, ``provenance`` and an honest ``estimated``
flag, and a query outside every window raises rather than guessing.

The store reads its facts from ``data/reference/*.csv`` - small, versioned,
deterministically rebuilt from the cached official records by
``scripts/_p2_build_reference.py``.  Nothing in this module touches the network;
if the cache grows, the CSVs are regenerated, never hand-edited.

Honesty rules encoded here
--------------------------
* The trading calendar is **derived** from the official F&O bhavcopy archive
  (a bhavcopy exists exactly on trading days from 2024-01-01 onward), not from a
  published holiday circular - the provenance on every trading-day record says so.
* Weekdays outside the verified archive span are ``UNKNOWN``, never assumed.
* Expiry weekday is resolved by effective-dated cadence rules; no code path
  assumes one weekday across years.
* An ambiguous contract mapping (two expiries that could own one trading day)
  raises :class:`AmbiguousExpiryError` instead of picking one.
* Lot, tick and strike-interval queries refuse (raise) when no record covers the
  day; the caller may pass an explicit fallback, which is always labelled
  ``estimated=True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from .reference import BASIS_DATE_REGIME, LotTable, expiry_from_iso

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Statuses a calendar day can honestly receive.
TRADING = "TRADING"
NOT_TRADING = "NOT_TRADING"
WEEKEND = "WEEKEND"
UNKNOWN = "UNKNOWN"


class AmbiguousExpiryError(ValueError):
    """Two listed expiries could map to one trading day - refuse to guess."""


class UnknownParameterError(LookupError):
    """No effective-dated record covers the requested day, and no fallback was given."""


@dataclass(frozen=True)
class EffectiveParameter:
    """One parameter value that held between two inclusive dates."""

    parameter: str
    effective_from: date
    effective_to: date
    value: object
    provenance: str
    estimated: bool = False
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    def covers(self, day: date) -> bool:
        return self.effective_from <= day <= self.effective_to

    @property
    def span_days(self) -> int:
        return (self.effective_to - self.effective_from).days + 1


@dataclass(frozen=True)
class ParameterResolution:
    """The winning record for one query, or the honest rejection of it."""

    parameter: str
    day: date
    value: object
    record: EffectiveParameter
    exact: bool = True       # answered from a record covering the day
    estimated: bool = False  # the winning record admits it is an estimate


def _coerce_value(text: str):
    """CSV cell -> int / float / str, so lot 65 stays an int and tick 0.05 a float."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


class ParameterStore:
    """All effective-dated records for one parameter, with tightest-window resolution."""

    def __init__(self, records: Iterable[EffectiveParameter] = ()) -> None:
        self.records: tuple[EffectiveParameter, ...] = tuple(records)

    def add(self, record: EffectiveParameter) -> None:
        self.records = self.records + (record,)

    def records_for(self, parameter: str) -> tuple[EffectiveParameter, ...]:
        return tuple(r for r in self.records if r.parameter == parameter)

    def covering(self, parameter: str, day: date) -> list[EffectiveParameter]:
        return [r for r in self.records_for(parameter) if r.covers(day)]

    def resolve(self, parameter: str, day: date,
                fallback: EffectiveParameter | None = None) -> ParameterResolution:
        """The record covering ``day`` with the tightest window wins; ties prefer a
        non-estimated record, then the earlier ``effective_from``.  With no covering
        record the caller-supplied ``fallback`` (always estimated) is returned, and
        without one the query raises - silently extending today's spec backwards is
        the failure mode this store exists to prevent."""
        matches = self.covering(parameter, day)
        if matches:
            best = min(matches, key=lambda r: (r.estimated, r.span_days,
                                               r.effective_from))
            return ParameterResolution(parameter, day, best.value, best,
                                       exact=True, estimated=best.estimated)
        if fallback is not None and fallback.parameter == parameter:
            return ParameterResolution(parameter, day, fallback.value, fallback,
                                       exact=False, estimated=True)
        raise UnknownParameterError(
            f"no {parameter!r} record covers {day.isoformat()} "
            f"({len(self.records_for(parameter))} records for this parameter)")

    @classmethod
    def from_csv(cls, path: Path) -> "ParameterStore":
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        records = []
        for row in frame.itertuples(index=False):
            records.append(EffectiveParameter(
                parameter=str(row.parameter),
                effective_from=date.fromisoformat(str(row.effective_from)),
                effective_to=(date.fromisoformat(str(row.effective_to))
                              if str(row.effective_to) else date.max),
                value=_coerce_value(str(row.value)),
                provenance=str(row.provenance),
                estimated=str(row.estimated).strip().lower() in {"true", "1", "yes"}))
        return cls(records)


@dataclass(frozen=True)
class DayStatus:
    day: date
    status: str
    estimated: bool
    provenance: str


class TradingCalendar:
    """Trading days derived from the official bhavcopy archive (``trading_days.csv``).

    ``is_trading_day`` is definite only inside the verified span: every exchange
    trading day publishes a bhavcopy, so a weekday inside the span with no file was
    not a trading day.  Outside the span (and every weekend, which the archive does
    not arbitrate) the answer is ``UNKNOWN`` - never a guessed holiday.
    """

    def __init__(self, days: Iterable[date], provenance: str = "derived_bhavcopy_archive",
                 floor: date | None = None, ceiling: date | None = None) -> None:
        self.days: frozenset[date] = frozenset(days)
        self.provenance = provenance
        self.floor = floor or (min(self.days) if self.days else None)
        self.ceiling = ceiling or (max(self.days) if self.days else None)

    @classmethod
    def from_csv(cls, path: Path) -> "TradingCalendar":
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        days = []
        # A weekday mislabelled in the CSV would silently corrupt every cadence
        # query built on the calendar; refuse the file instead.
        for text in frame["date"]:
            d = date.fromisoformat(text)
            recorded = str(frame.loc[frame["date"] == text, "weekday"].iloc[0])
            expected = WEEKDAY_NAMES[d.weekday()]
            if recorded and recorded != expected:
                raise ValueError(
                    f"{path.name}: {text} labelled {recorded} but is a {expected}")
            days.append(d)
        prov = str(frame["provenance"].iloc[0]) if "provenance" in frame and len(frame) \
            else "derived_bhavcopy_archive"
        return cls(days, provenance=prov)

    def is_trading_day(self, day: date) -> bool | None:
        status = self.status_of(day).status
        if status in (UNKNOWN, WEEKEND):
            return None   # the archive arbitrates neither
        return status == TRADING

    def status_of(self, day: date) -> DayStatus:
        if day in self.days:
            return DayStatus(day, TRADING, False, self.provenance)
        if day.weekday() >= 5:  # Sat/Sun: the bhavcopy archive does not arbitrate weekends
            return DayStatus(day, WEEKEND, True,
                             "weekend; archive arbiter covers weekdays only")
        if self.floor is not None and day < self.floor:
            return DayStatus(day, UNKNOWN, True,
                             f"before the bhavcopy archive ({self.floor.isoformat()})")
        if self.ceiling is not None and day > self.ceiling:
            return DayStatus(day, UNKNOWN, True,
                             f"after the bhavcopy archive ({self.ceiling.isoformat()})")
        # Weekday inside the verified span with no bhavcopy: derived non-trading day.
        return DayStatus(day, NOT_TRADING, False,
                         "no bhavcopy published on an in-span weekday (derived holiday)")

    def previous_trading_day(self, day: date) -> date | None:
        """Largest known trading day strictly before ``day``; None before the calendar."""
        probe = day - timedelta(days=1)
        horizon = self.floor or date(probe.year - 2, 1, 1)
        guard = 0
        while probe >= horizon and guard < 60:
            if probe in self.days:
                return probe
            probe -= timedelta(days=1)
            guard += 1
        return None

    def next_trading_day(self, day: date) -> date | None:
        probe = day + timedelta(days=1)
        horizon = self.ceiling or date(probe.year + 2, 12, 31)
        guard = 0
        while probe <= horizon and guard < 60:
            if probe in self.days:
                return probe
            probe += timedelta(days=1)
            guard += 1
        return None

    def last_trading_day_of_month(self, year: int, month: int) -> date | None:
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        probe = nxt - timedelta(days=1)
        while probe >= date(year, month, 1):
            if probe in self.days:
                return probe
            probe -= timedelta(days=1)
        return None


class ExpirySchedule:
    """Weekly-expiry weekday and monthly cadence as effective-dated rules, plus the
    set of expiries actually observed (cached files and official bhavcopy rows)."""

    def __init__(self, store: ParameterStore,
                 observed: Iterable[tuple[str, dict]] = ()) -> None:
        self.store = store
        self.observed: dict[str, dict] = {}
        for iso, extra in observed:
            self.observed[expiry_from_iso(iso)] = dict(extra or {})

    @classmethod
    def from_csvs(cls, cadence_csv: Path, observed_csv: Path) -> "ExpirySchedule":
        store = ParameterStore.from_csv(cadence_csv)
        frame = pd.read_csv(observed_csv, dtype=str, keep_default_na=False)
        date_col = next(c for c in frame.columns
                        if c in ("expiry", "date", "effective_from"))
        observed = []
        for row in frame.itertuples(index=False):
            extra = {col: str(val) for col, val in zip(frame.columns, row)
                     if col != date_col and str(val)}
            observed.append((str(getattr(row, date_col)), extra))
        return cls(store, observed)

    def weekday_rule(self, day: date) -> EffectiveParameter:
        return self.store.resolve("expiry_weekly_weekday", day)

    def weekday_of(self, day: date) -> str:
        return str(self.weekday_rule(day).value)

    def expected_weekly_expiries(self, start: date, end: date) -> list[date]:
        """Nominal regime weekdays - one per week per the rules in force, *before*
        the holiday shift to the immediately preceding trading day is applied."""
        out, d = [], start
        while d <= end:
            try:
                rule = self.weekday_rule(d)
            except UnknownParameterError:
                d += timedelta(days=1)
                continue
            if WEEKDAY_NAMES[d.weekday()] == str(rule.value):
                out.append(d)
            d += timedelta(days=1)
        return out

    def resolve_expiry_date(self, nominal: date,
                            calendar: TradingCalendar) -> tuple[date, bool]:
        """The exchange rule: an expiry falling on a non-trading day moves to the
        immediately preceding trading day.  Returns (resolved, shifted)."""
        status = calendar.status_of(nominal).status
        if status == TRADING:
            return nominal, False
        if status == UNKNOWN:
            raise UnknownParameterError(
                f"cannot resolve expiry {nominal}: calendar coverage unknown")
        moved = calendar.previous_trading_day(nominal)
        if moved is None:
            raise UnknownParameterError(f"no trading day before {nominal} in calendar")
        return moved, True

    def cadence_of(self, expiry_iso: str, calendar: TradingCalendar | None = None) -> str:
        """Classify one observed expiry.

        Monthly is tested first because the last weekly-expiry day of a month *is*
        the monthly contract, and only the trading calendar can prove it: the last
        trading day of the month may be a weekday after the regime weekday (a long
        weekend holiday run).  Without a calendar a regime weekday stays simply
        nominal - the monthly question is then honestly unresolved, not guessed.
        An expiry that is not on the regime weekday at all can only be a holiday
        artefact."""
        d = date.fromisoformat(expiry_iso)
        if calendar is not None and calendar.last_trading_day_of_month(d.year, d.month) == d:
            return "monthly_last_trading_day"
        weekly = WEEKDAY_NAMES[d.weekday()] == str(self.weekday_rule(d).value)
        if weekly:
            return "weekly_nominal"
        if calendar is not None:
            # A shifted weekly expiry: the nominal regime weekday a few days ahead
            # was a non-trading day, so expiry moved to the preceding trading day.
            nominal_weekly = None
            probe = d + timedelta(days=1)
            while probe <= d + timedelta(days=3) and nominal_weekly is None:
                try:
                    rule_value = str(self.weekday_rule(probe).value)
                except UnknownParameterError:
                    break  # beyond the cadence records: nothing to compare against
                if WEEKDAY_NAMES[probe.weekday()] == rule_value:
                    nominal_weekly = probe
                probe += timedelta(days=1)
            if nominal_weekly is not None:
                status = calendar.status_of(nominal_weekly).status
                if status in (NOT_TRADING, WEEKEND):
                    return "weekly_holiday_shift"
            if calendar.status_of(d).status == TRADING:
                # A trading day at or before the month's last, off the regime
                # weekday: only coherent as a monthly contract shortened by
                # late-month holidays.
                return "monthly_holiday_shortened"
        return "UNCLASSIFIED"

    def expiry_for_trading_day(self, day: date, calendar: TradingCalendar,
                               cadence: str = "weekly") -> list[str]:
        """Every observed expiry identity that trades on ``day`` under the weekly
        cadence - the day an expiring contract actually lives through.

        Ownership is asked of the calendar, not assumed from adjacency: a listed
        expiry owns ``day`` when ``day`` is the first trading day at or before its
        nominal date.  A holiday run can therefore pull two different listed
        expiries onto one day (nominal Thursday slid to Wednesday, and a second
        expiry whose whole run up to it was non-trading); when that happens the
        mapping is refused with :class:`AmbiguousExpiryError` - guessing a contract
        identity from the nearest file is exactly the failure this DB exists to
        prevent."""
        if cadence != "weekly":
            raise ValueError(f"unsupported cadence {cadence!r}")
        if calendar.status_of(day).status == UNKNOWN:
            raise UnknownParameterError(f"calendar unknown for {day.isoformat()}")
        owners: list[str] = []
        # A holiday-run collapse spans at most a long-weekend regime change plus
        # the exchange's shift tolerance; seven days either way bounds the search.
        for offset in range(-7, 8):
            nominal = day + timedelta(days=offset)
            try:
                rule_weekday = str(self.weekday_rule(nominal).value)
            except UnknownParameterError:
                continue
            if WEEKDAY_NAMES[nominal.weekday()] != rule_weekday:
                continue
            iso = nominal.isoformat()
            if iso in self.observed and calendar.previous_trading_day(
                    nominal + timedelta(days=1)) == day:
                owners.append(iso)
        unique = sorted(set(owners))
        if len(unique) > 1:
            raise AmbiguousExpiryError(
                f"trading day {day.isoformat()} maps to several expiries {unique}; "
                "refusing to guess a contract identity")
        return unique


class NiftyReference:
    """The whole effective-dated reference set: parameters, calendar, expiries."""

    def __init__(self, store: ParameterStore, calendar: TradingCalendar,
                 expiries: ExpirySchedule,
                 lot_table: LotTable | None = None) -> None:
        self.store = store
        self.calendar = calendar
        self.expiries = expiries
        self.lot_table = lot_table

    @classmethod
    def from_directory(cls, directory: Path | str = None) -> "NiftyReference":
        directory = Path(directory) if directory else reference_dir()
        store = ParameterStore.from_csv(directory / "nifty_reference_parameters.csv")
        calendar = TradingCalendar.from_csv(directory / "nifty_trading_days.csv")
        expiries = ExpirySchedule.from_csvs(
            directory / "nifty_expiry_cadence.csv",
            directory / "nifty_expiry_dates.csv")
        return cls(store, calendar, expiries, lot_table=LotTable())

    # -- effective-dated lookups ----------------------------------------------
    def _lot_fallback(self, day: date) -> EffectiveParameter | None:
        """The date-regime lot from ``reference.nifty_lot_regimes``, always labelled
        estimated - the regimes are modal windows, wrong for minority contracts on
        every mixed-lot date."""
        if self.lot_table is None:
            return None
        res = self.lot_table.lot_on(day)
        if not res.known or res.basis != BASIS_DATE_REGIME:
            return None
        return EffectiveParameter("board_lot", day, day, res.lot,
                                  res.provenance, estimated=True)

    def board_lot(self, day: date) -> ParameterResolution:
        return self.store.resolve("board_lot", day, fallback=self._lot_fallback(day))

    def tick_size(self, day: date) -> ParameterResolution:
        return self.store.resolve("price_tick", day)

    def strike_interval(self, day: date) -> ParameterResolution:
        return self.store.resolve("strike_interval", day)

    def parameter(self, name: str, day: date) -> ParameterResolution:
        return self.store.resolve(name, day)

    def regime_records(self, parameter: str) -> tuple[EffectiveParameter, ...]:
        """Every stored window for a parameter, ordered - the evidence table the
        acceptance report cites."""
        return tuple(sorted(self.store.records_for(parameter),
                             key=lambda r: r.effective_from))


def reference_dir() -> Path:
    """``data/reference`` resolved from this package's location (repo root parent)."""
    return Path(__file__).resolve().parents[2] / "data" / "reference"
