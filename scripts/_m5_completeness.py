"""M5 item 4 - expiry and day completeness: missing vs inactive vs no-trade vs truncated.

P1 answered one narrow question ("which monthly expiries have no Parquet file?") with a
one-day-per-month probe.  This report answers the completeness question the milestone
actually poses: for every expiry the exchange listed on any cached official date, and for
every contract-day inside the locally cached files, using the vocabulary in
``nifty_quant.derivatives.completeness`` so the causes never get merged:

``MISSING_FILE``
    the exchange listed the expiry and the provider publishes no file for it;
``PUBLISHED_NOT_CACHED``
    the provider publishes it but this audit never downloaded it - an audit gap, not a
    provider defect, and never reported as a hole;
``TRUNCATED_TAIL``
    the file exists but stops well before its own expiry day;
``NO_TRADES``
    the exchange reports zero volume and zero open interest, so absent bars are correct;
``MISSING_DAYS``
    the exchange reports traded volume on a day for which the file carries no bars;
``TRUNCATED_SESSION``
    bars exist but the file-day stops before the close, so the tail is gone;
``MISSING_TRADES``
    the session is complete yet the provider's day total is materially below the official
    traded volume, i.e. trades are missing rather than minutes;
``INACTIVE``
    the contract was not listed yet, or had already expired, so nothing is expected.

Method.  The exchange universe comes from the 318 cached official reports, so the widened
comparison costs no new downloads.  The provider inventory comes from the dataset tree API
(one request) and is cross-checked against the P1 snapshot.  A cached file whose size in
bytes equals the provider's published size proves the *download* completed, which is what
lets a short file be attributed to the provider rather than to this audit.

Nothing is written back to the raw files.  Output: ``logs/m5_completeness.{txt,json}``.

Usage (from the repo root):
    python scripts/_m5_completeness.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _p1_hf_schema import REPO, REV, UA  # noqa: E402
from _p1_nse_reconcile import load_official  # noqa: E402

from nifty_quant.derivatives.completeness import (  # noqa: E402
    DAY_MISSING, DAY_MISSING_TRADES, DAY_NO_OI, DAY_NO_TRADES, DAY_PRESENT,
    DAY_TRUNCATED, FILE_ABSENT, FILE_PRESENT, classify_day, classify_file,
)
from nifty_quant.derivatives.coverage_registry import (  # noqa: E402
    registry_status_for_day)
from nifty_quant.derivatives.timestamps import clock_label  # noqa: E402

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT_TXT = Path("logs/m5_completeness.txt")
OUT_JSON = Path("logs/m5_completeness.json")
COVERAGE_SNAPSHOT = Path("logs/p1_hf_coverage.json")

COLS = ["timestamp", "trading_day", "strike", "option_type", "volume", "open_interest"]
ARBITER_FLOOR = "2024-01-02"
REFERENCE_DATE = date(2026, 11, 3)
PUBLISHED_NOT_CACHED = "PUBLISHED_NOT_CACHED"
NO_OFFICIAL_EVIDENCE = "PUBLISHED_NO_OFFICIAL_EVIDENCE"
MAX_EXAMPLES = 6


def volume_bucket(contracts: float) -> str:
    """How liquid the exchange says the contract was - separates far strikes from holes."""
    if contracts <= 10:
        return "<=10 contracts traded"
    if contracts <= 100:
        return "11-100 contracts"
    return ">100 contracts"


def provider_listing() -> tuple[dict[str, int], str]:
    """Live ``options/NIFTY`` file list from the dataset tree API, or the P1 snapshot."""
    url = f"https://huggingface.co/api/datasets/{REPO}/tree/{REV}/options/NIFTY"
    try:
        request = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(request, timeout=60) as response:
            entries = json.loads(response.read().decode("utf-8"))
        listing = {e["name"].removesuffix(".parquet"): int(e.get("size") or 0)
                   for e in entries if e.get("name", "").endswith(".parquet")}
        if listing:
            return listing, "live tree API"
    except Exception as exc:  # noqa: BLE001
        print(f"  live tree listing failed: {type(exc).__name__}: {exc}")
    if COVERAGE_SNAPSHOT.exists():
        snap = json.loads(COVERAGE_SNAPSHOT.read_text(encoding="utf-8"))
        block = snap.get("options/NIFTY", {}).get("files", [])
        listing = {f["stem"]: int(f["bytes"]) for f in block if f.get("date")}
        if listing:
            return listing, "P1 snapshot (logs/p1_hf_coverage.json)"
    return {}, "unavailable"


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say("M5 item 4 - completeness: expiries, days and contracts")
    say("=" * 110)
    say(f"reference date (the day this audit was run): {REFERENCE_DATE}")
    say(f"official F&O arbiter exists only from {ARBITER_FLOOR}; earlier provider days are")
    say("reported on self-evidence and labelled unverified.")

    # ---------------------------------------------------------------- provider list
    published, listing_source = provider_listing()
    say(f"\nprovider inventory source: {listing_source}")
    say(f"published NIFTY expiry files at the provider: {len(published)}")
    local = {p.stem.replace("options__NIFTY__", ""): p for p in HF.glob("options__NIFTY__*.parquet")}
    say(f"cached locally: {len(local)}")
    size_mismatch = {e: (local[e].stat().st_size, published[e]) for e in local
                     if e in published and published[e] and local[e].stat().st_size != published[e]}
    say(f"cached files whose byte size differs from the published size: {size_mismatch or 'none'}")
    say("  (an equal size proves the download is complete, so a short file is the "
        "provider's, not this audit's)")

    # ---------------------------------------------------------------- exchange universe
    days = sorted(p.stem[-8:] for p in NSE.glob("BhavCopy_NSE_FO_*.zip"))
    say(f"cached official dates: {len(days)} ({days[0]} .. {days[-1]})")
    official_rows: dict[str, pd.DataFrame] = {}
    listed_by_expiry: dict[str, set] = defaultdict(set)
    failed: dict[str, str] = {}
    for ymd in days:
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        try:
            off = load_official(ymd)
        except Exception as exc:  # noqa: BLE001
            failed[iso] = f"{type(exc).__name__}: {exc}"
            continue
        keep = off[["expiry", "strike", "option_type", "TtlTradgVol", "TtlTrfVal",
                    "OpnIntrst", "NewBrdLotQty", "LastPric"]].copy()
        official_rows[iso] = keep
        for exp in keep["expiry"].unique():
            listed_by_expiry[str(exp)[:10]].add(iso)
    say(f"dates parsed: {len(official_rows)}   dates failed: {len(failed)}")
    universe = sorted(listed_by_expiry)
    say(f"distinct NIFTY option expiries the exchange listed across those dates: {len(universe)}")

    # ---------------------------------------------------------------- expiry verdicts
    say("")
    say("EXPIRY-LEVEL COMPLETENESS (exchange universe vs provider inventory vs local cache)")
    say("-" * 110)
    expiry_status: dict[str, str] = {}
    for exp in universe:
        if exp in local:
            expiry_status[exp] = FILE_PRESENT
        elif exp in published:
            expiry_status[exp] = PUBLISHED_NOT_CACHED
        else:
            expiry_status[exp] = FILE_ABSENT
    for exp in published:
        expiry_status.setdefault(exp, NO_OFFICIAL_EVIDENCE)
    counts = Counter(expiry_status.values())
    for status in (FILE_PRESENT, PUBLISHED_NOT_CACHED, FILE_ABSENT, NO_OFFICIAL_EVIDENCE):
        say(f"  {status:28s}: {counts.get(status, 0):>4d}")
    holes = sorted(e for e, s in expiry_status.items() if s == FILE_ABSENT)
    say("")
    say(f"  PROVIDER HOLES (exchange listed, provider publishes no file): {len(holes)}")
    for exp in holes:
        say(f"    {exp}: listed on {len(listed_by_expiry[exp])} cached official dates")
    say("  (2021-2023 expiries cannot appear here: the free official archive starts "
        f"{ARBITER_FLOOR}, so their absence is unverifiable rather than proven)")

    # ---------------------------------------------------------------- file + day scan
    say("")
    say("PER-FILE REPORT (locally cached NIFTY expiry files)")
    say("-" * 110)
    say(f"{'expiry':11s} {'status':17s} {'rows':>9s} {'days':>5s} {'expDays':>7s} "
        f"{'miss':>5s} {'first':10s} {'last':10s} {'truncSess':>9s} {'idleVol>0':>9s}")
    file_reports = []
    day_counts: Counter[str] = Counter()
    examples: dict[str, list] = defaultdict(list)
    truncated_sessions: list[dict] = []
    unverified_days: list[str] = []
    # Where the MISSING_DAYS cells actually are: contract-level sparsity inside a covered
    # file-day, versus a file-day the provider left out entirely.
    missing_by_day: Counter[str] = Counter()
    missing_by_bucket: Counter[str] = Counter()
    absent_file_days: list[dict] = []
    for expiry, path in sorted(local.items()):
        frame = pd.read_parquet(path, columns=COLS)
        frame["strike"] = frame["strike"].astype("int64")
        frame["day"] = frame["trading_day"].astype(str).str.slice(0, 10)
        days_in_file = sorted(frame["day"].unique())
        exp_date = date.fromisoformat(expiry)
        expected = sorted(listed_by_expiry.get(expiry, ()))
        # The denominator an auditor actually wants: every day the exchange listed this
        # expiry between the file's own first day and its expiry.
        expected_in_life = [d for d in expected if days_in_file[0] <= d <= expiry]

        per_day = frame.groupby("day", sort=True).agg(bars=("volume", "size"))
        block_last = frame.groupby("day")["timestamp"].max()
        last_oi = (frame.sort_values("timestamp", kind="stable")
                        .groupby(["day", "strike", "option_type"])["open_interest"].last()
                        .rename("last_oi").reset_index())
        bars_per = frame.groupby(["day", "strike", "option_type"])["volume"].size().rename("bars").reset_index()
        vol_per = frame.groupby(["day", "strike", "option_type"])["volume"].sum().rename("vol").reset_index()
        # Day-scoped lookups.  Joining on (strike, option_type) alone would fan every
        # official row out across every day the file holds, so a contract-day would be
        # judged on some other day's bars and the cell count would grow with days^2.
        bars_by_day = {d: g.drop(columns="day") for d, g in bars_per.groupby("day")}
        vol_by_day = {d: g.drop(columns="day") for d, g in vol_per.groupby("day")}
        oi_by_day = {d: g.drop(columns="day") for d, g in last_oi.groupby("day")}

        trunc_days, idle_with_volume, unlisted_block_days = 0, 0, 0
        for day in days_in_file:
            last_label = clock_label(block_last[day])
            if last_label < "15:25":
                trunc_days += 1
                truncated_sessions.append({"expiry": expiry, "day": day,
                                           "last_label": last_label,
                                           "bars": int(per_day.loc[day, "bars"])})
            if day not in official_rows:
                unverified_days.append(f"{day} x {expiry}")
                continue
            off = official_rows[day]
            off = off[off["expiry"] == expiry].copy()
            if off.empty:
                unlisted_block_days += 1
                continue
            off["contracts"] = pd.to_numeric(off["TtlTradgVol"], errors="coerce").fillna(0)
            off["oi"] = pd.to_numeric(off["OpnIntrst"], errors="coerce")
            off["lot"] = pd.to_numeric(off["NewBrdLotQty"], errors="coerce")
            cells = off[["strike", "option_type", "contracts", "oi", "lot"]].merge(
                bars_by_day[day], on=["strike", "option_type"], how="left").merge(
                vol_by_day[day], on=["strike", "option_type"], how="left").merge(
                oi_by_day[day], on=["strike", "option_type"], how="left")
            cells["bars"] = cells["bars"].fillna(0).astype("int64")
            cells["vol"] = cells["vol"].astype("float64")
            for row in cells.itertuples(index=False):
                official_shares = row.contracts * row.lot if row.lot and row.lot > 0 else None
                # no bars for this contract on this day -> no coverage claim at all
                coverage = (row.vol / official_shares
                            if official_shares and row.contracts > 0 and row.vol == row.vol
                            else None)
                if row.contracts <= 0 and row.vol and row.vol > 0:
                    idle_with_volume += 1
                report = classify_day(
                    trading_day=day, expiry=expiry, strike=row.strike,
                    option_type=row.option_type, listed=True,
                    official_volume=float(row.contracts), official_oi=float(row.oi) if row.oi == row.oi else None,
                    archive_bars=int(row.bars),
                    archive_last_oi=float(row.last_oi) if row.last_oi == row.last_oi else None,
                    expiry_date=exp_date, reference_date=REFERENCE_DATE,
                    block_last_label=last_label, volume_coverage=coverage)
                day_counts[report.status] += 1
                if report.status == DAY_MISSING:
                    missing_by_day[f"{day} x {expiry}"] += 1
                    missing_by_bucket[volume_bucket(row.contracts)] += 1
                if report.status != DAY_PRESENT and len(examples[report.status]) < MAX_EXAMPLES:
                    examples[report.status].append(
                        {"day": day, "expiry": expiry, "strike": report.strike,
                         "option_type": report.option_type, "bars": report.archive_bars,
                         "official_volume": report.official_volume,
                         "official_oi": report.official_oi,
                         "coverage": report.volume_coverage, "reason": report.reason})

        # days the exchange listed this expiry inside its own life but the file carries
        # no bars at all for them
        observed = set(per_day.index)
        for day_missing in [d for d in expected_in_life if d not in observed]:
            off = official_rows[day_missing]
            off = off[off["expiry"] == expiry]
            traded = pd.to_numeric(off["TtlTradgVol"], errors="coerce").fillna(0) > 0
            report = classify_day(
                trading_day=day_missing, expiry=expiry, strike=0, option_type="-",
                listed=True, official_volume=float(traded.sum()),
                official_oi=None, archive_bars=0, archive_last_oi=None,
                expiry_date=exp_date, reference_date=REFERENCE_DATE)
            day_counts[report.status] += 1
            if report.status == DAY_MISSING:
                absent_file_days.append({"day": day_missing, "expiry": expiry,
                                        "contracts_traded": float(traded.sum())})
                missing_by_bucket[volume_bucket(float(traded.sum()))] += 1
            if len(examples[report.status]) < MAX_EXAMPLES:
                examples[report.status].append(
                    {"day": day_missing, "expiry": expiry, "strike": 0,
                     "option_type": "-", "bars": 0, "official_volume": float(traded.sum()),
                     "official_oi": None, "coverage": None, "reason": report.reason})

        report = classify_file(
            symbol="NIFTY", expiry=expiry, provider_file_present=True,
            rows=int(len(frame)), days_observed=int(len(days_in_file)),
            first_day=days_in_file[0], last_day=days_in_file[-1],
            official_days_expected=len(expected_in_life),
            reference_date=REFERENCE_DATE, expiry_date=exp_date)
        file_reports.append({**report.__dict__, "truncated_session_days": trunc_days,
                             "idle_official_zero_archive_volume": idle_with_volume,
                             "unlisted_block_days": unlisted_block_days,
                             "days_expected_total": len(expected)})
        say(f"{expiry:11s} {report.status:17s} {report.rows:>9,d} {report.days_observed:>5d} "
            f"{report.official_days_expected:>7d} {report.days_missing:>5d} "
            f"{report.first_day:10s} {report.last_day:10s} {trunc_days:>9d} {idle_with_volume:>9d}")
        del frame, per_day, block_last, last_oi, bars_per, vol_by_day, oi_by_day, bars_by_day

    say("")
    say("DAY x CONTRACT VERDICT COUNTS (only days with an official arbiter are countable)")
    say("-" * 110)
    total_cells = sum(day_counts.values())
    for status, count in day_counts.most_common():
        say(f"  {status:18s}: {count:>8,d}  ({100 * count / total_cells:5.2f}%)")
    say(f"  total cells: {total_cells:,d}")
    say(f"  file-days with no official arbiter (unverifiable, listed for completeness): "
        f"{len(set(unverified_days)):,}")
    say("")
    say("EXAMPLES OF EVERY NON-PRESENT VERDICT")
    say("-" * 110)
    for status in sorted(examples):
        if status == DAY_PRESENT:
            continue
        say(f"  {status}:")
        for ex in examples[status]:
            cov = f"{ex['coverage']:.4f}" if ex["coverage"] is not None else "  -  "
            say(f"    {ex['day']} x {ex['expiry']} {ex['strike']}{ex['option_type']:>4s} "
                f"bars={ex['bars']:<5d} officialVol={ex['official_volume']} "
                f"officialOI={ex['official_oi']} coverage={cov} | {ex['reason'][:70]}")

    # ---------------------------------------------------------------- tail + session detail
    say("")
    say("WHERE THE MISSING_DAYS CELLS ARE")
    say("-" * 110)
    horizon = max((r["last_day"] for r in file_reports if r["last_day"]), default="")
    for b, c in missing_by_bucket.most_common():
        say(f"  by exchange-reported liquidity: {b:22s}: {c:>7,d}")
    say(f"  file-days the provider left out entirely (no bars for any contract): "
        f"{len(absent_file_days)}")
    for rec in sorted(absent_file_days, key=lambda r: -r["contracts_traded"])[:12]:
        say(f"    {rec['day']} x {rec['expiry']}: {rec['contracts_traded']:,.0f} contracts "
            f"traded per the exchange, zero provider bars")
    say(f"  worst 12 covered file-days for contract-level sparsity "
        f"(the day is in the file, these contracts are not):")
    for key, c in missing_by_day.most_common(12):
        say(f"    {key:25s}: {c:>6,d}")

    say("")
    say("PROVIDER HOLES VERSUS THE ARCHIVE'S OWN HORIZON")
    say("-" * 110)
    horizon_date = date.fromisoformat(horizon) if horizon else None
    in_horizon = [e for e in holes if horizon_date is None or date.fromisoformat(e) <= horizon_date]
    beyond = [e for e in holes if horizon_date is not None and date.fromisoformat(e) > horizon_date]
    say(f"  last trading day anywhere in the cached archive: {horizon or 'unknown'}")
    say(f"  expiries at or before it with no provider file (REAL HOLES) : {len(in_horizon)}")
    for e in in_horizon:
        say(f"    {e}: listed on {len(listed_by_expiry[e])} cached official dates")
    say(f"  expiries after it with no provider file (BEYOND HORIZON, not holes): "
        f"{len(beyond)}")
    say("    e.g. " + ", ".join(beyond[:8]) + (" ..." if len(beyond) > 8 else ""))
    say("  (the archive stops on the day above, so an expiry after it has had no session "
        "to be published for; calling those 'missing' would overstate the defect)")

    say("")
    say("TRUNCATED SESSIONS (file-day stops before the close)")
    say("-" * 110)
    if truncated_sessions:
        for rec in sorted(truncated_sessions, key=lambda r: r["day"]):
            say(f"  {rec['day']} x {rec['expiry']}: last minute label {rec['last_label']}, "
                f"{rec['bars']:,} bars")
        say(f"  total: {len(truncated_sessions)} truncated file-days across "
            f"{len({r['expiry'] for r in truncated_sessions})} files")
    else:
        say("  none")

    say("")
    say("THE 2026-08-04 FILE (the incomplete August 2026 file named in the milestone)")
    say("-" * 110)
    target = local.get("2026-08-04")
    if target is not None:
        rec = next((r for r in file_reports if r["expiry"] == "2026-08-04"), None)
        pub_size = published.get("2026-08-04")
        local_size = target.stat().st_size
        say(f"  local file {target.name}: {local_size:,} bytes")
        say(f"  provider published size: "
            f"{'not in the listing (provider inventory unavailable)' if pub_size is None else f'{pub_size:,} bytes -> ' + ('download complete, the provider file itself is short' if pub_size == local_size else 'SIZE DIFFERS: re-download before blaming the provider')}")
        if rec:
            say(f"  rows {rec['rows']:,}, days {rec['days_observed']} "
                f"({rec['first_day']} .. {rec['last_day']}), status {rec['status']}")
            say(f"  official dates on which the exchange listed this expiry: "
                f"{rec['days_expected_total']} (of which inside the file's own window: "
                f"{rec['official_days_expected']})")
            say(f"  expiry day {rec['expiry']} is after the last observed day "
                f"{rec['last_day']}: the file carries neither its expiry session nor the "
                f"sessions between {rec['last_day']} and that day")
    else:
        say("  file not cached locally")

    # ---------------------------------------------------------------- verdict
    say("")
    say("VERDICT")
    say("-" * 110)
    say(f"  1. {len(in_horizon)} exchange-listed NIFTY expiry file(s) are missing from the "
        f"provider inside the archive's own span ({horizon}); they are listed above and are "
        f"never skipped silently. {len(beyond)} further listed expiries fall after that "
        f"date, where the archive has no session at all, so they are reported as beyond the "
        f"horizon rather than as holes. {counts.get(PUBLISHED_NOT_CACHED, 0)} expiries are "
        f"published by the provider but not downloaded here - an audit gap, as such.")
    say(f"  2. {day_counts.get(DAY_MISSING, 0):,} contract-days are genuinely missing "
        f"(exchange traded, no bars); {day_counts.get(DAY_NO_TRADES, 0):,} are absence of "
        f"trades; {day_counts.get(DAY_TRUNCATED, 0):,} sit in truncated sessions; "
        f"{day_counts.get(DAY_MISSING_TRADES, 0):,} have the session but not all trades; "
        f"{day_counts.get(DAY_NO_OI, 0):,} have bars without any open-interest "
        f"observation.")
    say(f"  3. {len(truncated_sessions)} file-days stop before the close, so the closing "
        f"minutes are absent - these must not be aggregated into candles and are excluded "
        f"by strict validation.")
    say("  4. no contract is silently dropped: every non-present verdict above carries a "
        "reason and an example, and 2021-2023 days are reported as unverifiable rather "
        "than assumed complete.")

    OUT_JSON.write_text(json.dumps({
        "expiry_status": expiry_status, "provider_holes": holes,
        "holes_in_archive_span": in_horizon, "holes_beyond_horizon": beyond,
        "archive_horizon": horizon, "absent_file_days": absent_file_days,
        "missing_by_bucket": dict(missing_by_bucket),
        "published": len(published), "listing_source": listing_source,
        "size_mismatch": size_mismatch, "files": file_reports,
        "day_counts": dict(day_counts), "examples": examples,
        "registry_status_counts": {
            registry_status_for_day(status): count
            for status, count in day_counts.items()},
        "truncated_sessions": truncated_sessions,
        "unverified_file_days": sorted(set(unverified_days))[:200],
    }, indent=1, default=str), encoding="utf-8")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_TXT} and {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
