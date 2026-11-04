"""Completeness classification: distinguishing missing data from absent trades.

The archive is not self-describing, so a gap can mean several very different things.
Treating them alike - especially by silently dropping a contract - hides exactly the
defects that matter.  Four causes are separated here, each with its own evidence
requirement:

``MISSING_FILE``
    The provider has no parquet for an expiry the exchange listed.  Verified by an
    anonymous HTTP read of the dataset tree: ``options/NIFTY/2024-12-31.parquet``
    answers 404 while the official bhavcopy for 2024-12-30/31 lists that expiry with
    trades, so the file is absent upstream rather than a holiday or a cache gap.

``TRUNCATED_TAIL``
    The file exists but stops part-way through the contract's documented life.
    ``options/NIFTY/2026-08-04.parquet`` holds 2,646 rows over six days
    (2026-06-24 .. 2026-07-02) and nothing after - not even its own expiry day - while
    comparable expiries run up to expiry.  A truncated download, not a quiet contract.

``NO_TRADES``
    The exchange lists the contract on the day with ``TtlTradgVol == 0``.  No bars is the
    correct representation, because the archive only carries minutes that traded, and it
    stays correct when positions were still carried overnight: open interest says nothing
    about whether a trade happened.  It must be *reported* as an absence of trades rather
    than absorbed into missing data.

``MISSING_DAYS``
    The exchange reports traded volume for a contract on a day for which the provider
    file carries no bars at all.  This is genuine lost data and cannot be recovered by
    interpolation.

``TRUNCATED_SESSION``
    Bars exist but the file-day stops before the close, so the tail of the session is
    gone for every contract in that block.  Measured on 2025-09-26 (expiry 2025-09-30),
    whose bars stop at 15:00: 346 of 376 minute labels, and 6.765 billion provider
    share-equivalents against 7.493 billion official - 9.7% of the day's volume absent.

``MISSING_TRADES``
    The whole session is present but the provider's day total is materially below the
    official traded volume, i.e. individual trades are missing rather than minutes.

``INACTIVE``
    The expiry has already passed relative to the days examined, or the contract was
    not yet listed, so no observation is expected.

Open interest is classified separately from prices: a bar can exist with ``open_interest
== 0``, which means the provider carried no OI observation for that minute, not that the
contract had no positions.  Neither case may be turned into a number by filling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

#: File-level verdicts.
FILE_PRESENT = "PRESENT"
FILE_ABSENT = "MISSING_FILE"
FILE_TRUNCATED = "TRUNCATED_TAIL"
FILE_NOT_IN_PROVIDER = "NOT_PUBLISHED_BY_PROVIDER"

#: Day-and-contract level verdicts.
DAY_PRESENT = "PRESENT"
DAY_NO_TRADES = "NO_TRADES"
DAY_MISSING = "MISSING_DAYS"
DAY_INACTIVE = "INACTIVE"
DAY_NO_OI = "PRESENT_WITHOUT_OI"
DAY_TRUNCATED = "TRUNCATED_SESSION"
DAY_MISSING_TRADES = "MISSING_TRADES"

#: A file-day whose last minute label is earlier than this has lost part of the session.
#: 15:25 leaves room for legitimately quiet closing minutes while still catching the
#: measured 15:00 truncation.
SESSION_TAIL_FLOOR = "15:25"
#: Day totals this far below the official traded volume count as missing trades.
MIN_VOLUME_COVERAGE = 0.99

#: A file whose last observed day is this many trading days before its expiry (while the
#: expiry is already in the past) is treated as truncated rather than merely thin.
TRUNCATION_TOLERANCE_DAYS = 3


@dataclass(frozen=True)
class FileReport:
    """One provider expiry file, or one expected expiry with no file at all."""

    symbol: str
    expiry: str
    status: str
    rows: int
    days_observed: int
    first_day: str
    last_day: str
    official_days_expected: int
    details: str = ""

    @property
    def days_missing(self) -> int:
        return max(self.official_days_expected - self.days_observed, 0)


@dataclass(frozen=True)
class DayReport:
    """One (trading day x contract) cell of the cross-reference."""

    trading_day: str
    expiry: str
    strike: int
    option_type: str
    status: str
    official_volume: float | None
    official_oi: float | None
    archive_bars: int
    archive_last_oi: float | None
    reason: str = ""
    block_last_label: str = ""
    volume_coverage: float | None = None


def _as_iso(value) -> str:
    """An ISO *date* string from a date, datetime, Timestamp or string.

    ``pandas.Timestamp`` subclasses ``datetime`` (and therefore ``date``), so the
    datetime case must be resolved before the date case or a full timestamp would be
    returned where a date is required.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def classify_file(*, symbol: str, expiry: str, provider_file_present: bool,
                  rows: int, days_observed: int, first_day: str, last_day: str,
                  official_days_expected: int, reference_date: date,
                  expiry_date: date) -> FileReport:
    """Decide whether a file is present, absent, or truncated - and say why.

    Truncation is only claimed for expiries that have already passed relative to
    ``reference_date``; a live contract legitimately has no data past today.
    """
    expiry_iso = _as_iso(expiry)
    if not provider_file_present:
        if official_days_expected > 0:
            return FileReport(symbol, expiry_iso, FILE_ABSENT, 0, 0, "", "",
                              official_days_expected,
                              f"exchange reports {official_days_expected} traded days "
                              f"for this expiry and the provider returns no file")
        return FileReport(symbol, expiry_iso, FILE_NOT_IN_PROVIDER, 0, 0, "", "", 0,
                          "no expiry file and no official evidence of traded days")
    if (expiry_date < reference_date and last_day
            and _days_between(_as_iso(last_day), expiry_iso) > TRUNCATION_TOLERANCE_DAYS):
        return FileReport(symbol, expiry_iso, FILE_TRUNCATED, rows, days_observed,
                          first_day, last_day, official_days_expected,
                          f"expired on {expiry_iso} but the file stops on {last_day}; "
                          f"{max(official_days_expected - days_observed, 0)} expected "
                          f"traded days absent from the file")
    return FileReport(symbol, expiry_iso, FILE_PRESENT, rows, days_observed, first_day,
                      last_day, official_days_expected,
                      f"{days_observed} observed of {official_days_expected} expected days")


def _days_between(start_iso: str, end_iso: str) -> int:
    """Calendar-day distance; used only as a coarse truncation signal."""
    return (date.fromisoformat(end_iso) - date.fromisoformat(start_iso)).days


def classify_day(*, trading_day: str, expiry: str, strike: int, option_type: str,
                 listed: bool, official_volume: float | None, official_oi: float | None,
                 archive_bars: int, archive_last_oi: float | None,
                 expiry_date: date, reference_date: date,
                 block_last_label: str = "", volume_coverage: float | None = None) -> DayReport:
    """Cross-reference one official exchange row against the provider archive.

    ``listed`` is whether the official report contains the contract at all on that day;
    an unlisted contract on a past date is inactive, never "missing".

    ``block_last_label`` is the latest minute label anywhere in that file-day (a *block*
    property: a session that stops at 15:00 has lost its tail for every contract in the
    file, not only for the ones that traded late).  ``volume_coverage`` is the provider's
    day total divided by the official day total in the same unit.
    """
    day_iso = _as_iso(trading_day)
    volume = 0.0 if official_volume is None else float(official_volume)
    oi = None if official_oi is None else float(official_oi)
    if archive_bars > 0:
        if block_last_label and block_last_label < SESSION_TAIL_FLOOR:
            status = DAY_TRUNCATED
            reason = (f"file-day stops at {block_last_label}, before the "
                      f"{SESSION_TAIL_FLOOR} tail floor; the closing session is absent "
                      f"for every contract in this block")
        elif volume_coverage is not None and volume_coverage < MIN_VOLUME_COVERAGE:
            status = DAY_MISSING_TRADES
            reason = (f"provider day total is {100 * volume_coverage:.2f}% of the "
                      f"official traded volume ({MIN_VOLUME_COVERAGE:.0%} floor)")
        elif archive_last_oi in (None, 0.0):
            status = DAY_NO_OI
            reason = (f"provider bars carry no open-interest observation "
                      f"(last OI {archive_last_oi}); official OI {oi}")
        else:
            status, reason = DAY_PRESENT, ""
        return DayReport(day_iso, _as_iso(expiry), int(strike), str(option_type), status,
                         volume, oi, archive_bars, archive_last_oi, reason,
                         block_last_label, volume_coverage)
    if not listed:
        return DayReport(day_iso, _as_iso(expiry), int(strike), str(option_type),
                         DAY_INACTIVE, None, None, 0, None,
                         "exchange lists no row for this contract on this day")
    if volume <= 0:
        return DayReport(day_iso, _as_iso(expiry), int(strike), str(option_type),
                         DAY_NO_TRADES, volume, oi, 0, None,
                         "official traded volume is zero: absence of bars is correct "
                         f"(open interest {oi} says nothing about trades)")
    if expiry_date < reference_date:
        return DayReport(day_iso, _as_iso(expiry), int(strike), str(option_type),
                         DAY_MISSING, volume, oi, 0, None,
                         f"exchange reports {volume:g} contracts traded with no "
                         f"provider bars on this day")
    return DayReport(day_iso, _as_iso(expiry), int(strike), str(option_type), DAY_INACTIVE,
                     volume, oi, 0, None, "expiry not yet reached on the reference date")


def summarise(reports: list[DayReport]) -> dict[str, int]:
    """Count of each verdict - the numbers that must appear in any completeness claim."""
    counts: dict[str, int] = {}
    for report in reports:
        counts[report.status] = counts.get(report.status, 0) + 1
    return counts
