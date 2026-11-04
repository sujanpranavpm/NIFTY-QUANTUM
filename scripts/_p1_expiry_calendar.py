"""P1 step 6b - expiry calendar forensics against circular-verified regimes.

First pass tested the archive against a regime table written from memory, and the
memory was wrong.  This version takes every expected regime from a document:

  * NSE/FAOP/54334 (2022-11-07)  NIFTY weekly cycle reduced 7 -> 4 contracts from
        Dec 09 2022.  Expiry *day* is untouched, so NIFTY stays on Thursday.
  * NSE/FAOP/64506 (2024-10-10)  from Nov 20 2024 weekly expiries allowed on only
        one benchmark index per exchange; BANKNIFTY weeklies discontinued, which
        is why BANKNIFTY has exactly one expiry file per month.
  * NSE/FAOP/68747 (2025-06-25)  NIFTY weekly and monthly expiry moves to Tuesday
        from Sep 01 2025; Jul/Aug 2025 rows still Thursday; holiday shifts are
        written into the annexure (24-Dec-25 since 25-Dec-25 is a holiday).

Two questions are answered per symbol:
  1. weekday cadence - does every expiry file land on the regime weekday, and is
     every deviation explainable by the exchange rule "if the expiry day is a
     trading holiday, expiry moves to the immediately preceding trading day"?
  2. completeness - which regime weekdays inside the covered span have NO expiry
     file.  A missing expiry is missing data, not a market holiday, unless the
     official bhavcopy for that date is also absent.

The holiday test uses the official NSE F&O bhavcopy as the arbiter: a trading day
always has one.  That arbiter only exists from 2024-01-02, so anything earlier is
reported as UNVERIFIABLE rather than assumed.

Usage:
    python scripts/_p1_expiry_calendar.py
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
COVERAGE = Path("logs/p1_hf_coverage.json")
BHAV_DIR = Path("data/raw/p1_nse")
PROBE_CACHE = Path("data/raw/p1_nse/nse_trading_day_probe.json")
OUT = Path("logs/p1_expiry_calendar.json")

# (from, to, allowed weekdays, cadence, provenance).  Inclusive.  cadence is
# "weekly" when every such weekday in the month carries an expiry, and "monthly"
# when only the last one does.
REGIMES: dict[str, list[tuple[date, date, set[str], str, str]]] = {
    "options/NIFTY": [
        (date(2021, 1, 1), date(2025, 8, 31), {"Thu"}, "weekly",
         "NSE/FAOP/54334 + FAOP68747 (Thu till 29Aug25)"),
        (date(2025, 9, 1), date(2027, 12, 31), {"Tue"}, "weekly",
         "NSE/FAOP/68747 (Tue from 01Sep25)"),
    ],
    # BANKNIFTY weeklies were discontinued on 2024-11-20 by NSE/FAOP/64506, so
    # inside this archive BANKNIFTY is monthly-only: one expiry per month.
    "options/BANKNIFTY": [
        (date(2021, 1, 1), date(2024, 3, 31), {"Thu"}, "monthly", "monthly-only last Thursday"),
        (date(2024, 4, 1), date(2024, 11, 30), {"Wed"}, "monthly", "last Wednesday regime"),
        (date(2024, 12, 1), date(2025, 8, 31), {"Thu"}, "monthly", "back to last Thursday"),
        (date(2025, 9, 1), date(2027, 12, 31), {"Tue"}, "monthly", "NSE/FAOP/68747 (Tue from 01Sep25)"),
    ],
    # SENSEX is BSE, not NSE: the windows below come from published coverage of
    # the BSE expiry-day changes, not from an NSE circular held in this repo.
    "options/SENSEX": [
        (date(2021, 1, 1), date(2024, 12, 31), {"Fri"}, "weekly",
         "BSE last Friday [externally reported]"),
        (date(2025, 1, 1), date(2025, 8, 31), {"Tue"}, "weekly",
         "BSE Tuesday regime [externally reported]"),
        (date(2025, 9, 1), date(2027, 12, 31), {"Thu"}, "weekly",
         "BSE stayed Thursday when NSE moved [externally reported]"),
    ],
}


def allowed(d: date, symbol: str) -> tuple[set[str], str]:
    for start, end, days, _cadence, source in REGIMES[symbol]:
        if start <= d <= end:
            return days, source
    return set(), "no regime defined"


def weekdays_between(a: date, b: date, names: set[str], cadence: str) -> list[date]:
    """Expected expiry dates.  Monthly cadence means the last such weekday of
    each calendar month, which is what the exchange actually lists."""
    if cadence == "weekly":
        out, d = [], a
        while d <= b:
            if WEEKDAYS[d.weekday()] in names:
                out.append(d)
            d += timedelta(days=1)
        return out
    out, first = [], date(a.year, a.month, 1)
    while first <= b:
        last = date(first.year, first.month, 1) + timedelta(days=45)
        last = date(last.year, last.month, 1) - timedelta(days=1)
        candidates = [last - timedelta(days=k) for k in range(7)]
        for d in candidates:
            if WEEKDAYS[d.weekday()] in names:
                if a <= d <= b:
                    out.append(d)
                break
        first = last + timedelta(days=1)
    return sorted(out)


def bhavcopy_exists(d: date) -> bool | None:
    """True/False from cache or probe; None when the date is outside the window
    where the official archive is reachable at all (before 2024-01-02)."""
    if d < date(2024, 1, 2):
        return None
    key = f"{d:%Y%m%d}"
    if (BHAV_DIR / f"BhavCopy_NSE_FO_{key}.zip").exists():
        return True
    return PROBES.get(key)


class Prober:
    """One HEAD-style GET per candidate date, memoised to disk.  A missing
    bhavcopy is the exchange's own signal that the day was not a trading day."""

    def __init__(self) -> None:
        self._map: dict[str, bool] = {}
        if PROBE_CACHE.exists():
            self._map = json.loads(PROBE_CACHE.read_text(encoding="utf-8"))
        self._session = None

    def _ensure(self) -> None:
        if self._session is None:
            from curl_cffi import requests as curl

            self._session = curl.Session(impersonate="chrome124")
            self._session.get("https://www.nseindia.com", timeout=30)

    def __getitem__(self, key: str) -> bool | None:
        if key in self._map:
            return self._map[key]
        try:
            self._ensure()
            url = (f"https://nsearchives.nseindia.com/content/fo/"
                   f"BhavCopy_NSE_FO_0_0_0_{key}_F_0000.csv.zip")
            # Ask for 4 bytes only: a real file starts with the ZIP magic.  A day
            # with no bhavcopy was not an NSE trading day.
            response = self._session.get(url, timeout=40, headers={"Range": "bytes=0-3"})
            ok = response.status_code in (200, 206) and response.content[:2] == b"PK"
        except Exception:  # noqa: BLE001 - unreachable is not evidence of a holiday
            return None
        self._map[key] = ok
        PROBE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PROBE_CACHE.write_text(json.dumps(self._map, indent=2), encoding="utf-8")
        time.sleep(0.15)
        return ok

    get = __getitem__


PROBES = Prober()


def main() -> int:
    coverage = json.loads(COVERAGE.read_text(encoding="utf-8"))
    report: dict[str, dict] = {}

    for symbol in REGIMES:
        block = coverage.get(symbol)
        if not block:
            continue
        expiries = sorted(date.fromisoformat(e["date"]) for e in block["files"] if e["date"])
        span_start, span_end = expiries[0], expiries[-1]
        have = set(expiries)

        print("=" * 100)
        print(f"{symbol}: {len(expiries)} expiry files, {span_start} .. {span_end}")
        print("=" * 100)

        off_regime, unverifiable, holiday_shift = [], [], []
        for d in expiries:
            names, source = allowed(d, symbol)
            wd = WEEKDAYS[d.weekday()]
            if wd in names:
                continue
            # Deviation: is it the published preceding-trading-day shift?
            nominal = d
            shifted_from = None
            for probe in (d + timedelta(days=1), d + timedelta(days=2), d + timedelta(days=3)):
                if WEEKDAYS[probe.weekday()] in names:
                    shifted_from, nominal = probe, probe
                    break
            row = {"date": str(d), "weekday": wd, "regime": sorted(names), "nominal": str(nominal)}
            if shifted_from is None:
                row["verdict"] = "not explainable as a holiday shift"
                off_regime.append(row)
            else:
                exists = bhavcopy_exists(nominal)
                if exists is None:
                    row["verdict"] = "holiday shift plausible, UNVERIFIABLE (no official archive pre-2024)"
                    unverifiable.append(row)
                elif not exists:
                    row["verdict"] = "consistent with holiday shift (official bhavcopy for nominal day absent)"
                    holiday_shift.append(row)
                else:
                    row["verdict"] = "DEVIATION: nominal regime day was a trading day"
                    off_regime.append(row)

        by_span: dict[str, list[date]] = defaultdict(list)
        for start, end, names, cadence, _ in REGIMES[symbol]:
            lo, hi = max(start, span_start), min(end, span_end)
            if lo <= hi:
                for d in weekdays_between(lo, hi, names, cadence):
                    by_span[f"{start}:{end}:{cadence}"].append(d)
        missing = sorted(d for days in by_span.values() for d in days
                        if not any(d - timedelta(days=k) in have for k in range(4)))
        missing_rows = []
        for d in missing:
            exists = bhavcopy_exists(d)
            missing_rows.append({"date": str(d), "weekday": WEEKDAYS[d.weekday()],
                                "official_traded": {True: "yes - MISSING EXPIRY", False: "no - market holiday",
                                                    None: "unverifiable"}[exists]})

        months = Counter(f"{d:%Y-%m}" for d in expiries)
        print(f"weekday census                : {dict(Counter(WEEKDAYS[d.weekday()] for d in expiries))}")
        print(f"regime windows                : "
              f"{[(str(s), str(e), sorted(n), c) for s, e, n, c, _ in REGIMES[symbol]]}")
        print(f"expected expiries in span     : {sum(len(v) for v in by_span.values())}")
        print(f"expiries on regime weekday    : {len(expiries) - len(off_regime) - len(unverifiable) - len(holiday_shift)}"
              f" / {len(expiries)}")
        print(f"holiday-shift deviations      : {len(holiday_shift)}")
        for row in holiday_shift:
            print(f"    {row['date']} ({row['weekday']}) instead of {row['nominal']}")
        print(f"deviations UNVERIFIABLE (pre-2024 archive gap): {len(unverifiable)}")
        for row in unverifiable:
            print(f"    {row['date']} ({row['weekday']}) nominal {row['nominal']}")
        print(f"UNEXPLAINED deviations                          : {len(off_regime)}")
        for row in off_regime:
            print(f"    {row['date']} ({row['weekday']}) {row['verdict']}")
        print(f"\nregime weekdays with NO expiry file (3-day shift tolerance): {len(missing_rows)}")
        for row in missing_rows:
            print(f"    {row['date']} ({row['weekday']})  official_traded={row['official_traded']}")
        if symbol == "options/SENSEX":
            print("    caveat: SENSEX is a BSE product; the NSE bhavcopy used as the "
                  "trading-day arbiter reflects the NSE calendar, not BSE's.")
        print(f"\nexpiry files per month (min/max): {min(months.values())}/{max(months.values())}, "
              f"months covered {len(months)}")

        report[symbol] = {
            "n_files": len(expiries), "span": [str(span_start), str(span_end)],
            "weekday_census": dict(Counter(WEEKDAYS[d.weekday()] for d in expiries)),
            "holiday_shift": holiday_shift, "unverifiable": unverifiable,
            "unexplained": off_regime, "missing_expiries": missing_rows,
        }

    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
