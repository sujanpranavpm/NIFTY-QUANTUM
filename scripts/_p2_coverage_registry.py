"""P2 stage 3 - build the persistent expiry-and-contract coverage registry.

This turns the completeness classification (``nifty_quant.derivatives.completeness``)
into a durable registry (``nifty_quant.derivatives.coverage_registry``) that records
*every* exchange-listed expiry and *every* contract-day cell with an explicit status,
reason and evidence - nothing silently skipped.  It reuses the exact per-cell
``classify_day`` / ``classify_file`` logic of ``scripts/_m5_completeness.py`` so the
registry's contract-day counts reconcile line-for-line with the completeness report; a
mismatch fails closed.

Two extra, strictly-evidenced measurements are attached to each contract-day cell:

* ``candle_gaps`` - missing traded-minutes inside the contract's own first..last span
  (detected, never fabricated);
* ``high_low_flag`` - whether the archive intraday range stays inside the official
  ``HghPric``/``LwPric`` range, ``None`` when no official range exists to compare.

The offline provider inventory (published-but-not-cached vs a true provider hole) comes
from the P1 coverage snapshot, never a live account-gated fetch; when the snapshot is
absent the expiry status degrades to ``UNKNOWN`` with a stated reason, so a gap in the
audit is never reported as a gap in the exchange.

Outputs:
  data/reference/nifty_coverage_registry.csv   the durable registry
  logs/p2_coverage.txt                         the reconciliation report
  logs/p2_alt_sources.txt                      the free-source assessment (Part 3)

Usage (from the repo root):
    python scripts/_p2_coverage_registry.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _p1_nse_reconcile import load_official  # noqa: E402  (cache parsing only)

from nifty_quant.derivatives.completeness import (  # noqa: E402
    classify_day, classify_file)
from nifty_quant.derivatives.coverage_registry import (  # noqa: E402
    CONTRACT_DAY_LEVEL, EXPIRY_LEVEL, MISSING_FILE, PUBLISHED_NOT_CACHED, UNKNOWN,
    CoverageEntry, CoverageRegistry, high_low_within_official,
    minute_gap_from_span, registry_status_for_day, registry_status_for_file)
from nifty_quant.derivatives.reference import REFERENCE_DIR  # noqa: E402
from nifty_quant.derivatives.timestamps import clock_label, clock_labels  # noqa: E402

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT_CSV = REFERENCE_DIR / "nifty_coverage_registry.csv"
OUT_TXT = Path("logs/p2_coverage.txt")
M5_JSON = Path("logs/m5_completeness.json")
COVERAGE_SNAPSHOT = Path("logs/p1_hf_coverage.json")

COLS = ["timestamp", "trading_day", "strike", "option_type",
        "volume", "open_interest", "high", "low"]
ARBITER_FLOOR = "2024-01-02"
REFERENCE_DATE = date(2026, 11, 3)
SESSION_TAIL_FLOOR = "15:25"


def offline_provider_inventory() -> tuple[dict[str, int], str]:
    """Provider expiry inventory from the P1 snapshot only (no live, account-gated fetch)."""
    if COVERAGE_SNAPSHOT.exists():
        snap = json.loads(COVERAGE_SNAPSHOT.read_text(encoding="utf-8"))
        block = snap.get("options/NIFTY", {}).get("files", [])
        listing = {f["stem"]: int(f["bytes"]) for f in block if f.get("date")}
        if listing:
            return listing, "P1 snapshot (logs/p1_hf_coverage.json, offline)"
    return {}, "unavailable offline (no cached provider inventory)"


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    say("P2 stage 3 - coverage registry (every listed expiry, every contract-day)")
    say("=" * 104)
    say(f"reference date: {REFERENCE_DATE}; official F&O arbiter only from {ARBITER_FLOOR}")

    days = sorted(p.stem.split("_")[-1] for p in NSE.glob("BhavCopy_NSE_FO_*.zip")
                  if p.stem.split("_")[-1].isdigit())
    iso = lambda y: f"{y[:4]}-{y[4:6]}-{y[6:]}"  # noqa: E731
    say(f"cached official dates: {len(days)} ({iso(days[0])} .. {iso(days[-1])})")

    local = {p.stem.replace("options__NIFTY__", ""): p
             for p in HF.glob("options__NIFTY__*.parquet")}
    say(f"cached provider expiry files: {len(local)}")

    published, inventory_source = offline_provider_inventory()
    say(f"provider inventory: {len(published)} published expiries via {inventory_source}")

    # ---- exchange universe + per-day official rows (one parse) ----
    say("parsing cached bhavcopies ...")
    official_rows: dict[str, pd.DataFrame] = {}
    listed_by_expiry: dict[str, set] = defaultdict(set)
    for ymd in days:
        d = iso(ymd)
        off = load_official(ymd)
        keep = off[["expiry", "strike", "option_type", "TtlTradgVol", "OpnIntrst",
                    "NewBrdLotQty", "HghPric", "LwPric"]].copy()
        official_rows[d] = keep
        for exp in keep["expiry"].unique():
            listed_by_expiry[str(exp)[:10]].add(d)
    universe = sorted(listed_by_expiry)
    say(f"distinct NIFTY expiries the exchange listed: {len(universe)}")

    registry = CoverageRegistry()

    # ---- expiry-level rows ----
    expiry_status: dict[str, str] = {}
    for exp in universe:
        if exp in local:
            expiry_status[exp] = registry_status_for_file(
                classify_file(
                    symbol="NIFTY", expiry=exp, provider_file_present=True, rows=0,
                    days_observed=0, first_day="", last_day="",
                    official_days_expected=len(listed_by_expiry[exp]),
                    reference_date=REFERENCE_DATE,
                    expiry_date=date.fromisoformat(exp)).status)
        elif exp in published:
            expiry_status[exp] = PUBLISHED_NOT_CACHED
        elif not published:
            expiry_status[exp] = UNKNOWN
        else:
            expiry_status[exp] = MISSING_FILE
    for exp in published:
        expiry_status.setdefault(exp, UNKNOWN if exp not in universe else "PRESENT")
    for exp, status in expiry_status.items():
        reason = {
            MISSING_FILE: "exchange listed this expiry on cached dates; provider "
                          "publishes no file (verified against cached provider inventory)",
            PUBLISHED_NOT_CACHED: "provider publishes the file but this offline audit "
                                  "never cached it - an audit gap, not a provider hole",
            UNKNOWN: "provider inventory unavailable offline; cannot distinguish a "
                     "true hole from an un-cached file",
        }.get(status, f"cached provider file for exchange-listed expiry {exp}")
        registry.add(CoverageEntry(
            level=EXPIRY_LEVEL, expiry=exp, status=status, expected=exp in universe,
            observed=exp in local, arbiter=any(d >= ARBITER_FLOOR
                                               for d in listed_by_expiry.get(exp, ())),
            reason=reason,
            evidence=f"listed_on_{len(listed_by_expiry.get(exp, ()))}_cached_official_dates"))

    # ---- contract-day cells for every cached file ----
    say("scanning cached provider files for contract-day coverage ...")
    day_counts: Counter[str] = Counter()
    gaps_measured = flagged_out = flagged_total = 0
    unverified_cells = 0
    for expiry, path in sorted(local.items()):
        frame = pd.read_parquet(path, columns=COLS)
        frame["strike"] = frame["strike"].astype("int64")
        frame["day"] = frame["trading_day"].astype(str).str.slice(0, 10)
        exp_date = date.fromisoformat(expiry)
        block_last = frame.groupby("day")["timestamp"].max()
        frame = frame.assign(clock=clock_labels(frame["timestamp"]))
        agg = frame.groupby(["day", "strike", "option_type"]).agg(
            bars=("volume", "size"), vol=("volume", "sum"),
            last_oi=("open_interest", "last"),
            arch_high=("high", "max"), arch_low=("low", "min"),
            n_labels=("clock", "nunique"),
            first_clock=("clock", "min"), last_clock=("clock", "max")).reset_index()
        day_groups = {d: g for d, g in agg.groupby("day", sort=True)}

        for day in sorted(frame["day"].unique()):
            last_label = clock_label(block_last[day])
            sub = day_groups.get(day)
            off = official_rows.get(day)
            if off is None:
                # provider file-day with no official arbiter (2021-2023): unverifiable
                # per contract, so it is aggregated to ONE UNKNOWN registry row rather
                # than exploded into a row per contract that no arbiter can classify.
                if sub is not None and len(sub):
                    registry.add(CoverageEntry(
                        level=CONTRACT_DAY_LEVEL, trading_day=day, expiry=expiry,
                        strike=0, option_type="*", status=UNKNOWN, expected=False,
                        observed=True, arbiter=False,
                        reason="provider bars exist for this file-day but no official "
                               "F&O report is cached, so per-contract completeness is "
                               "unverifiable (aggregated to one registry row)",
                        evidence=f"contracts={len(sub)};bars={int(sub['bars'].sum())}"))
                    unverified_cells += int(len(sub))
                continue
            off = off[off["expiry"] == expiry]
            if off.empty:
                continue
            off = off.assign(
                strike=off["strike"].astype("int64"),
                contracts=pd.to_numeric(off["TtlTradgVol"], errors="coerce").fillna(0),
                oi=pd.to_numeric(off["OpnIntrst"], errors="coerce"),
                lot=pd.to_numeric(off["NewBrdLotQty"], errors="coerce"),
                hi=pd.to_numeric(off["HghPric"], errors="coerce"),
                lo=pd.to_numeric(off["LwPric"], errors="coerce"))
            offmap = {(int(r.strike), str(r.option_type)): r
                      for r in off.itertuples(index=False)}
            cellmap = {(int(r.strike), str(r.option_type)): r
                       for r in sub.itertuples(index=False)} if sub is not None else {}
            universe_keys = set(offmap) | set(cellmap)
            for key in sorted(universe_keys):
                strike, option_type = key
                row = cellmap.get(key)
                bars = int(row.bars) if row is not None else 0
                vol = float(row.vol) if row is not None else 0.0
                last_oi = float(row.last_oi) if row is not None else None
                orow = offmap.get(key)
                listed = orow is not None
                contracts = float(orow.contracts) if listed else 0.0
                oi = float(orow.oi) if listed and orow.oi == orow.oi else None
                lot = float(orow.lot) if listed and orow.lot == orow.lot else None
                o_hi = float(orow.hi) if listed and orow.hi == orow.hi else None
                o_lo = float(orow.lo) if listed and orow.lo == orow.lo else None
                official_shares = contracts * lot if lot and lot > 0 else None
                coverage = (vol / official_shares if official_shares and contracts > 0
                            else None)
                report = classify_day(
                    trading_day=day, expiry=expiry, strike=strike,
                    option_type=option_type, listed=listed,
                    official_volume=contracts if listed else 0.0,
                    official_oi=oi, archive_bars=bars, archive_last_oi=last_oi,
                    expiry_date=exp_date, reference_date=REFERENCE_DATE,
                    block_last_label=last_label, volume_coverage=coverage)
                if listed:
                    day_counts[report.status] += 1   # only exchange-enumerated cells
                status = registry_status_for_day(report.status)
                gaps = (minute_gap_from_span(row.first_clock, row.last_clock,
                                             int(row.n_labels))
                        if row is not None else 0)
                if gaps:
                    gaps_measured += 1
                flag = high_low_within_official(
                    float(row.arch_high) if row is not None else None,
                    float(row.arch_low) if row is not None else None,
                    o_hi, o_lo)
                if flag is False:
                    flagged_out += 1
                if flag is not None:
                    flagged_total += 1
                registry.add(CoverageEntry(
                    level=CONTRACT_DAY_LEVEL, trading_day=day, expiry=expiry,
                    strike=strike, option_type=option_type, status=status,
                    expected=listed, observed=bool(bars), arbiter=True,
                    candle_gaps=gaps, high_low_flag=flag,
                    reason=report.reason or ("exchange and archive both report the "
                                             "contract traded on this day"
                                             if status == "PRESENT" else ""),
                    evidence=f"bars={bars};official_contracts={contracts:g};oi={oi}"))

        # Whole file-days the exchange listed inside this expiry's own life but the
        # provider file carries no bars for at all.  The completeness report collapses
        # each to one rollup row (strike 0); reproduce that here so the registry's
        # exchange-enumerated subset reconciles exactly, while still recording the
        # absence rather than skipping it.
        days_in_file = sorted(frame["day"].unique())
        observed_days = set(days_in_file)
        for d in sorted(listed_by_expiry.get(expiry, ())):
            if d in observed_days or d not in official_rows:
                continue
            if not (days_in_file[0] <= d <= expiry):
                continue
            offd = official_rows[d]
            offd = offd[offd["expiry"] == expiry]
            traded = pd.to_numeric(offd["TtlTradgVol"], errors="coerce").fillna(0)
            report = classify_day(
                trading_day=d, expiry=expiry, strike=0, option_type="-", listed=True,
                official_volume=float(traded.sum()), official_oi=None,
                archive_bars=0, archive_last_oi=None, expiry_date=exp_date,
                reference_date=REFERENCE_DATE)
            day_counts[report.status] += 1
            registry.add(CoverageEntry(
                level=CONTRACT_DAY_LEVEL, trading_day=d, expiry=expiry, strike=0,
                option_type="-", status=registry_status_for_day(report.status),
                expected=True, observed=False, arbiter=True, reason=report.reason,
                evidence=f"whole file-day absent from the provider file; "
                         f"official_traded={float(traded.sum()):g}"))
        del frame, agg, sub

    # ---- guards + reconciliation ----
    registry.assert_no_silent_skip()

    mapped_expected = {registry_status_for_day(k): v for k, v in day_counts.items()}
    reg_cells = registry.counts(CONTRACT_DAY_LEVEL)
    reg_expected = registry.counts(CONTRACT_DAY_LEVEL, expected=True)
    # the exchange-enumerated cells must reproduce the completeness pass exactly
    internal_drift = {s: {"registry": reg_expected.get(s, 0),
                          "pass": mapped_expected.get(s, 0)}
                      for s in set(reg_expected) | set(mapped_expected)
                      if reg_expected.get(s, 0) != mapped_expected.get(s, 0)}
    reconcile_ok = not internal_drift
    say("")
    say("CONTRACT-DAY REGISTRY COUNTS (registry vocabulary)")
    say("-" * 104)
    total = sum(reg_cells.values())
    for status, count in sorted(reg_cells.items(), key=lambda kv: -kv[1]):
        say(f"  {status:20s}: {count:>8,d}  ({100 * count / total:5.2f}%)")
    say(f"  total contract-day rows: {total:,d} "
        f"(of which exchange-enumerated {sum(reg_expected.values()):,d})")

    # cross-check against the completeness report we reconcile to
    if M5_JSON.exists():
        m5 = json.loads(M5_JSON.read_text(encoding="utf-8"))
        m5_mapped = {registry_status_for_day(k): v
                     for k, v in m5.get("day_counts", {}).items()}
        drift = {s: {"registry_expected": reg_expected.get(s, 0), "m5": m5_mapped.get(s, 0)}
                 for s in set(reg_expected) | set(m5_mapped)
                 if reg_expected.get(s, 0) != m5_mapped.get(s, 0)}
        say("")
        say(f"reconcile vs logs/m5_completeness.json: "
            f"{'MATCH' if not drift else 'DRIFT ' + str(drift)}")
    say(f"internal reconcile (registry exchange-enumerated subset vs its own pass): "
        f"ok={reconcile_ok} {internal_drift or ''}")

    say("")
    say("EXPIRY-LEVEL REGISTRY COUNTS")
    say("-" * 104)
    exp_counts = registry.counts(EXPIRY_LEVEL)
    for status, count in sorted(exp_counts.items(), key=lambda kv: -kv[1]):
        say(f"  {status:22s}: {count:>5d}")

    say("")
    say("COVERAGE CEILING AND SUPPLEMENTARY MEASURES")
    say("-" * 104)
    cov = registry.max_observed_coverage()
    say(f"  arbitrated contract-days      : {cov['arbitrated_contract_days']:,d}")
    say(f"  present contract-days         : {cov['present_contract_days']:,d}")
    say(f"  maximum observed coverage     : {cov['present_share']}% "
        "(measured; never extended)")
    say(f"  expiries / expiries present   : {cov['expiries']} / {cov['expiries_present']}")
    say(f"  unverifiable provider cells   : {unverified_cells:,d} (2021-2023, no arbiter)")
    say(f"  cells with candle minute gaps : {gaps_measured:,d} (detected, not fabricated)")
    say(f"  high/low integrity            : {flagged_out:,d} of {flagged_total:,d} "
        "compared cells exceed the official range (None where no official range)")

    registry.write_csv(OUT_CSV)
    say("")
    say(f"wrote {OUT_CSV}  ({len(registry):,d} rows)")

    # ---- alt-source assessment (fail-closed, no accounts) ----
    try:
        import _p2_alt_sources as alt  # noqa: E402
        # probe=False keeps this build offline and deterministic; the fail-closed
        # conclusion does not depend on a live reachability check, and a standalone
        # `python scripts/_p2_alt_sources.py` run is where an anonymous probe is made.
        alt_lines = alt.assess(say=say, probe=False)
        Path("logs/p2_alt_sources.txt").write_text("\n".join(alt_lines) + "\n",
                                                   encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        say(f"  alt-source assessment failed closed: {type(exc).__name__}: {exc}")

    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_TXT}")
    return 0 if reconcile_ok else 1


if __name__ == "__main__":
    sys.exit(main())
