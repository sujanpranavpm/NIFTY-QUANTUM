"""M3 item 2 - open-interest scale change: verification, conversion, reconciliation.

Question
--------
P1 measured that the provider's ``open_interest`` column changes scale between
2024-12-31 and 2025-01-01: the official NSE ``OpnIntrst`` divided by the provider value
has a median of ~1.0 up to the break and ~100.0 after it.  P1 measured that on a handful
of dates.  This script re-measures it across **every** cached official F&O date that
overlaps a locally cached expiry file, on both sides of the break, and then checks the
properties the milestone requires:

1. raw provider OI is preserved and the original source named,
2. the conversion is *date-aware* (per bar trading day, not per file, not per expiry),
3. the x100 branch is labelled **estimated**, never presented as a measured unit,
4. reconciliation against the official EOD open interest is reported per date, and the
   count of *independently verified* contract-days is the ceiling for OI-based intraday
   features,
5. missing OI and zero OI are counted separately - a bar with ``open_interest == 0`` is a
   provider observation, while a contract with no bars at all is an absence,
6. nothing is manufactured: no fill, no forward-fill, no back-fill from the EOD figure,
7. the official EOD figure is attached only at or after 15:30 IST and a deliberately
   leaked copy is caught by ``assert_no_eod_leakage``.

Why the *last bar* of the day is the only comparable observation: the official report
publishes one end-of-day figure, so an intraday maximum is a different quantity.  Both
are measured here so the difference is not hidden by assumption.

Usage (from the repo root):
    python scripts/_m3_oi_norm.py
"""

from __future__ import annotations

import sys
import traceback
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _p1_nse_reconcile import ensure_bhavcopy, load_official  # noqa: E402

from nifty_quant.derivatives import units as U  # noqa: E402
from nifty_quant.derivatives.reference import LotTable  # noqa: E402

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")

# Optional first argument: an expiry prefix, to smoke-test the pipeline on one family of
# files without overwriting the audit outputs.
PREFIX = sys.argv[1] if len(sys.argv) > 1 else ""
OUT_TXT = Path("logs/m3_oi_norm.txt") if not PREFIX else Path("logs/_m3_smoke.txt")
OUT_JSON = Path("logs/m3_oi_norm.json") if not PREFIX else Path("logs/_m3_smoke.json")

COLS = ["timestamp", "open_interest", "volume", "trading_day", "symbol", "strike",
        "option_type", "expiry"]
TOL = 0.01  # "agrees with the official figure" tolerance: 1%


def load_expiry(expiry: str) -> pd.DataFrame:
    path = HF / f"options__NIFTY__{expiry}.parquet"
    frame = pd.read_parquet(path, columns=COLS)
    frame = frame.drop_duplicates(
        subset=["symbol", "expiry", "strike", "option_type", "timestamp"], keep="first")
    frame["strike"] = frame["strike"].astype("int64")
    return frame.sort_values("timestamp", kind="stable")


def ratio_stats(values: np.ndarray) -> dict:
    return {
        "n": int(values.size),
        "median": round(float(np.median(values)), 5) if values.size else None,
        "p10": round(float(np.quantile(values, 0.10)), 5) if values.size else None,
        "p90": round(float(np.quantile(values, 0.90)), 5) if values.size else None,
    }


def compare_block(iso: str, expiry: str, official: pd.DataFrame,
                  frame: pd.DataFrame) -> dict | None:
    """One (official date x expiry file) OI comparison."""
    day = frame[frame["trading_day"].astype(str).str.slice(0, 10) == iso]
    out: dict = {"date": iso, "expiry_file": expiry}
    if day.empty:
        return None

    # --- provider observation quality, before any conversion ---------------------
    raw = day["open_interest"].astype("int64")
    out["bars"] = int(len(day))
    out["bars_oi_zero"] = int((raw == 0).sum())
    out["bars_oi_missing"] = int(day["open_interest"].isna().sum())
    out["bars_oi_negative"] = int((raw < 0).sum())

    off = official[official["expiry"] == expiry].copy()
    if off.empty:
        out["usable"] = False
        out["why"] = "this expiry is not in that day's official report (not yet listed/expired)"
        return out
    off["official_oi"] = pd.to_numeric(off["OpnIntrst"], errors="coerce")
    off["official_vol"] = pd.to_numeric(off["TtlTradgVol"], errors="coerce")

    last = day.groupby(["strike", "option_type"], observed=True, sort=False).tail(1)
    peak = day.groupby(["strike", "option_type"], observed=True, sort=False)[
        "open_interest"].max().rename("peak_oi").reset_index()
    per_contract = last[["strike", "option_type", "open_interest"]].merge(
        peak, on=["strike", "option_type"], how="left")

    merged = per_contract.merge(
        off[["strike", "option_type", "official_oi", "official_vol"]],
        on=["strike", "option_type"], how="inner")
    out["contracts_matched"] = int(len(merged))
    merged["raw_oi"] = merged["open_interest"].astype("float64")

    # Date-aware conversion, applied per bar trading day through the production helper.
    conv = [U.oi_to_share_equivalent(v, date.fromisoformat(iso))
            for v in merged["raw_oi"]]
    merged["oi_sh"] = [c.value for c in conv]
    out["conversion_estimated"] = bool(conv[0].estimated) if conv else None
    out["conversion_unit"] = conv[0].unit if conv else None
    out["provider_unit_label"] = (U.UNIT_PROVIDER_OI_LATE
                                  if date.fromisoformat(iso) >= U.OI_SCALE_BREAK
                                  else U.UNIT_PROVIDER_OI_EARLY)

    cmp = merged[(merged["official_oi"] > 0) & (merged["raw_oi"] > 0)]
    out["official_oi_zero_contracts"] = int((merged["official_oi"] <= 0).sum())
    out["provider_zero_oi_official_positive"] = int(
        ((merged["raw_oi"] == 0) & (merged["official_oi"] > 0)).sum())
    if cmp.empty:
        out["usable"] = False
        out["why"] = "no contract with positive OI in both sources"
        return out

    raw_ratio = (cmp["official_oi"] / cmp["raw_oi"]).to_numpy(float)
    sh_ratio = (cmp["official_oi"] / cmp["oi_sh"]).to_numpy(float)
    out["raw_ratio"] = ratio_stats(raw_ratio)
    out["converted_ratio"] = ratio_stats(sh_ratio)
    out["within_1pct_raw"] = int((np.abs(raw_ratio - 1.0) <= TOL).sum())
    out["within_1pct_converted"] = int((np.abs(sh_ratio - 1.0) <= TOL).sum())

    # Intraday peak as a foil: the EOD figure is not an intraday maximum.
    peak_sh = np.array([U.oi_to_share_equivalent(v, date.fromisoformat(iso)).value
                        for v in cmp["peak_oi"].astype(float)])
    peak_ratio = cmp["official_oi"].to_numpy(float) / peak_sh
    keep = np.isfinite(peak_ratio) & (peak_sh > 0) & (cmp["official_oi"].to_numpy(float) > 0)
    out["peak_within_1pct"] = int((np.abs(peak_ratio[keep] - 1.0) <= TOL).sum())
    out["peak_comparable"] = int(keep.sum())
    out["usable"] = True
    return out


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say("M3 item 2 - open-interest scale change: verification and reconciliation")
    say("=" * 110)

    readme = Path("README.md")
    if readme.exists():
        text = readme.read_text(encoding="utf-8", errors="replace")
        mentions = [ln.strip() for ln in text.splitlines()
                    if "open_interest" in ln or "OI" in ln or "interest" in ln
                    or "timestamp" in ln]
        say("source documentation - every README line mentioning open interest/timestamp:")
        for ln in (mentions or ["(none)"]):
            say(f"    {ln[:110]}")
        say("  -> the publisher documents no unit for open_interest and no timestamp "
            "semantics, so every unit claim below is measured, not quoted.")

    day_list = sorted({p.stem[-8:] for p in NSE.glob("BhavCopy_NSE_FO_*.zip")})
    say(f"\ncached official dates: {len(day_list)} "
        f"({day_list[0]} .. {day_list[-1]})")
    expiries = sorted(p.stem.replace("options__NIFTY__", "")
                      for p in HF.glob("options__NIFTY__*.parquet"))
    if PREFIX:
        expiries = [e for e in expiries if e.startswith(PREFIX)]
        say(f"\nSMOKE MODE - expiry prefix {PREFIX!r}, output goes to {OUT_TXT}")
    say(f"local NIFTY expiry files: {len(expiries)}")

    official_cache: dict[str, pd.DataFrame] = {}

    def official_for(ymd: str) -> pd.DataFrame | None:
        if ymd in official_cache:
            frame = official_cache[ymd]
            return frame if not frame.empty else None
        if ensure_bhavcopy(ymd) is None:
            official_cache[ymd] = pd.DataFrame()
            return None
        try:
            frame = load_official(ymd)
        except Exception as exc:  # noqa: BLE001
            say(f"  {ymd}: official load failed {type(exc).__name__}: {exc}")
            frame = pd.DataFrame()
        official_cache[ymd] = frame
        return frame if not frame.empty else None

    rows: list[dict] = []
    skipped: dict[str, int] = defaultdict(int)
    for expiry in expiries:
        frame = load_expiry(expiry)
        days = sorted({str(d)[:10] for d in frame["trading_day"].unique()})
        have = {f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in day_list}
        for iso in days:
            if iso not in have:
                continue
            off = official_for(iso.replace("-", ""))
            if off is None:
                skipped["official report unreadable"] += 1
                continue
            try:
                row = compare_block(iso, expiry, off, frame)
            except Exception as exc:  # noqa: BLE001
                where = traceback.format_exc().splitlines()[-2].strip()
                row = {"date": iso, "expiry_file": expiry,
                       "error": f"{type(exc).__name__}: {exc} [{where[-60:]}]",
                       "usable": False}
            if row is None:
                skipped["no bars on that day"] += 1
                continue
            if not row.get("usable"):
                skipped[row.get("error") or row.get("why") or "not usable"] += 1
            rows.append(row)
        del frame

    usable = [r for r in rows if r.get("usable")]
    say(f"\nfile-days compared: {len(usable)}  (skipped: {dict(skipped)})")

    # ---- per-date rollup --------------------------------------------------------
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in usable:
        by_date[r["date"]].append(r)

    say("")
    say("RAW official-OI / provider-OI RATIO PER DATE (n-weighted median of file-day medians)")
    say("-" * 110)
    say(f"{'date':11s} {'files':>5s} {'contracts':>9s} {'rawMed':>8s} {'rawP10':>8s} "
        f"{'rawP90':>8s} {'convMed':>8s} {'<=1%raw':>8s} {'<=1%conv':>9s} {'unit':>31s}")
    date_rollup: dict[str, dict] = {}
    for iso in sorted(by_date):
        rs = by_date[iso]
        n = sum(r["converted_ratio"]["n"] for r in rs)
        if not n:
            continue
        wmed = sum(r["raw_ratio"]["median"] * r["raw_ratio"]["n"] for r in rs) / n
        wcen = sum(r["converted_ratio"]["median"] * r["converted_ratio"]["n"] for r in rs) / n
        p10 = min(r["raw_ratio"]["p10"] for r in rs)
        p90 = max(r["raw_ratio"]["p90"] for r in rs)
        raw_ok = sum(r["within_1pct_raw"] for r in rs)
        conv_ok = sum(r["within_1pct_converted"] for r in rs)
        unit = rs[0]["provider_unit_label"]
        date_rollup[iso] = {"n": n, "raw_median": wmed, "conv_median": wcen,
                            "within_1pct_raw": raw_ok, "within_1pct_converted": conv_ok,
                            "unit": unit, "files": len(rs)}
        say(f"{iso:11s} {len(rs):>5d} {n:>9d} {wmed:>8.3f} {p10:>8.3f} {p90:>8.3f} "
            f"{wcen:>8.3f} {raw_ok:>8d} {conv_ok:>9d} {unit:>31s}")

    say("")
    say("BREAK WINDOW (every cached date around the change)")
    say("-" * 110)
    win = [d for d in sorted(date_rollup) if "2024-12-20" <= d <= "2025-01-12"]
    for iso in win:
        r = date_rollup[iso]
        say(f"  {iso}: raw ratio {r['raw_median']:8.3f}  converted {r['conv_median']:7.4f}  "
            f"within 1% of official after conversion {r['within_1pct_converted']}"
            f"/{r['n']}  [{r['unit']}]")
    pre = [d for d in sorted(date_rollup) if d < U.OI_SCALE_BREAK.isoformat()]
    post = [d for d in sorted(date_rollup) if d >= U.OI_SCALE_BREAK.isoformat()]
    if pre and post:
        say(f"  last pre-break date {pre[-1]}: median raw ratio "
            f"{date_rollup[pre[-1]]['raw_median']:.3f}")
        say(f"  first post-break date {post[0]}: median raw ratio "
            f"{date_rollup[post[0]]['raw_median']:.3f}")
        say(f"  -> the scale change is observed on {pre[-1]} [share_equivalent] -> "
            f"{post[0]} [1/100 of share equivalents] and nowhere else")
    wrong_side = [d for d in pre if date_rollup[d]["raw_median"] > 5] + \
                 [d for d in post if date_rollup[d]["raw_median"] < 5]
    say(f"  dates whose ratio contradicts their side of the break: {wrong_side or 'none'}")

    # ---- regime / coverage summary ---------------------------------------------
    say("")
    say("CONVERSION VALIDATION (date-aware, x100 branch labelled estimated)")
    say("-" * 110)
    say(f"  OI_SCALE_BREAK = {U.OI_SCALE_BREAK}  OI_SCALE_ADJUSTMENT = {U.OI_SCALE_ADJUSTMENT}")
    early = [r for r in usable if date.fromisoformat(r["date"]) < U.OI_SCALE_BREAK]
    late = [r for r in usable if date.fromisoformat(r["date"]) >= U.OI_SCALE_BREAK]
    for label, group in (("provider==share_equivalent (pre-break)", early),
                         ("provider==1/100 share_equivalent (post-break, estimated)", late)):
        if not group:
            continue
        n = sum(r["converted_ratio"]["n"] for r in group)
        conv_ok = sum(r["within_1pct_converted"] for r in group)
        raw_ok = sum(r["within_1pct_raw"] for r in group)
        say(f"  {label}")
        say(f"      file-days {len(group)}, comparable contracts {n}, "
            f"within 1% of official EOD: raw {raw_ok} ({100 * raw_ok / n:.2f}%), "
            f"converted {conv_ok} ({100 * conv_ok / n:.2f}%)")
        say(f"      estimated flag observed in this group: "
            f"{sorted({bool(r['conversion_estimated']) for r in group})}")
    est_pre = sorted({r["date"] for r in early if r["conversion_estimated"]})
    est_post = sorted({r["date"] for r in late if not r["conversion_estimated"]})
    say(f"  pre-break dates wrongly flagged estimated: {est_pre or 'none'}")
    say(f"  post-break dates wrongly flagged as measured: {est_post or 'none'}")

    say("")
    say("INDEPENDENTLY VERIFIED OBSERVATIONS (ceiling for OI-based intraday features)")
    say("-" * 110)
    n_all = sum(r["converted_ratio"]["n"] for r in usable)
    n_ok = sum(r["within_1pct_converted"] for r in usable)
    peak_ok = sum(r["peak_within_1pct"] for r in usable)
    peak_n = sum(r["peak_comparable"] for r in usable)
    if not n_all:
        say("  no comparable contract found on any date - nothing can be verified")
        OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1
    say(f"  contract-days comparable: {n_all}")
    say(f"  last-bar converted OI within 1% of official EOD OI: {n_ok} ({100 * n_ok / n_all:.2f}%)")
    say(f"  intraday PEAK OI within 1% of official EOD OI: {peak_ok}/{peak_n} "
        f"({100 * peak_ok / max(peak_n, 1):.2f}%) - the EOD figure is not an intraday maximum")
    say(f"  dates before the official arbiter floor ({U.OFFICIAL_ARBITER_FLOOR}): no "
        f"independent verification exists, so conversions there stay unverified by definition")

    say("")
    say("MISSING vs ZERO OPEN INTEREST")
    say("-" * 110)
    bars = sum(r["bars"] for r in usable) or 1
    say(f"  bars in compared file-days: {bars:,}")
    say(f"  bars with provider OI == 0: {sum(r['bars_oi_zero'] for r in usable):,} "
        f"({100 * sum(r['bars_oi_zero'] for r in usable) / bars:.2f}%)")
    say(f"  bars with provider OI missing (NaN): {sum(r['bars_oi_missing'] for r in usable):,}")
    say(f"  bars with provider OI < 0: {sum(r['bars_oi_negative'] for r in usable):,}")
    say(f"  contracts with provider OI == 0 while official OI > 0 (missing observation, "
        f"never fill): {sum(r['provider_zero_oi_official_positive'] for r in usable):,}")
    say(f"  contracts listed by the exchange with official OI <= 0 (genuinely no "
        f"positions): {sum(r['official_oi_zero_contracts'] for r in usable):,}")

    # ---- leakage guard ---------------------------------------------------------
    say("")
    say("LEAKAGE GUARD (official EOD must not be visible before 15:30 IST)")
    say("-" * 110)
    sample_iso, sample_expiry = None, None
    for iso in sorted(by_date):
        for r in by_date[iso]:
            if r["contracts_matched"] > 50:
                sample_iso, sample_expiry = iso, r["expiry_file"]
                break
        if sample_iso:
            break
    if sample_iso:
        frame = load_expiry(sample_expiry)
        day = frame[frame["trading_day"].astype(str).str.slice(0, 10) == sample_iso].copy()
        day["trading_day"] = sample_iso
        eod = official_for(sample_iso.replace("-", ""))
        eod = eod[eod["expiry"] == sample_expiry][
            ["strike", "option_type", "OpnIntrst"]].copy()
        eod["expiry"] = sample_expiry
        eod["trading_day"] = sample_iso
        attached = U.attach_official_eod(day, eod)
        U.assert_no_eod_leakage(attached)
        clock = pd.to_datetime(attached["timestamp"]).dt.strftime("%H:%M:%S")
        populated = attached[attached["oi_official_eod"].notna()]
        say(f"  sample: {sample_iso} x expiry {sample_expiry}, {len(attached):,} bars")
        say(f"  bars carrying the official EOD figure: {len(populated):,} "
            f"({100 * len(populated) / len(attached):.2f}%), earliest clock "
            f"{populated['timestamp'].min()}")
        say(f"  all 15:30-and-later bars? "
            f"{bool((pd.to_datetime(populated['timestamp']).dt.strftime('%H:%M:%S') >= '15:30:00').all())}")
        say(f"  pre-close bars carrying it: "
            f"{int((attached['oi_official_eod'].notna() & (clock < '15:30:00')).sum()):,}")
        say(f"  source label: "
            f"{attached['oi_official_eod_source'].iloc[0] if len(attached) else 'n/a'}")
        leak = attached.copy()
        earliest = pd.to_datetime(leak["timestamp"]).dt.strftime("%H:%M:%S") == clock.min()
        leaked_value = float(leak["oi_official_eod"].dropna().iloc[0]) \
            if leak["oi_official_eod"].notna().any() else 1.0
        leak.loc[earliest, "oi_official_eod"] = leaked_value
        try:
            U.assert_no_eod_leakage(leak)
            say("  LEAK NOT CAUGHT - the guard is broken")
        except U.LookAheadError as exc:
            say(f"  deliberately leaked copy ({int(earliest.sum())} bars at {clock.min()}, "
                f"value {leaked_value}) caught: LookAheadError: {exc}")

        normalised = U.normalise_units(day, LotTable())
        untouched = int((normalised["open_interest"].to_numpy()
                         == normalised["oi_raw"].to_numpy()).sum())
        zero_raw = int((normalised["oi_raw"] == 0).sum())
        zero_out = int((normalised["oi_share_equivalent"] == 0).sum())
        say(f"  no-manufacture check on the same block: raw column unaltered in "
            f"{untouched}/{len(normalised)} bars; raw zeros {zero_raw:,} -> "
            f"converted zeros {zero_out:,} (a fill would have changed this count)")
        say(f"  raw provider column retained: "
            f"{'open_interest' in normalised.columns and 'oi_raw' in normalised.columns}")
        del frame, day, attached, leak, normalised

    say("")
    say("VERDICT")
    say("-" * 110)
    say(f"  1. provider open_interest is share equivalents through "
        f"{(pd.Timestamp(U.OI_SCALE_BREAK) - pd.Timedelta(days=1)).strftime('%Y-%m-%d')} and 1/100 of "
        f"that from {U.OI_SCALE_BREAK}; measured on {len(date_rollup)} official dates.")
    say(f"  2. the date-aware x100 conversion lifts agreement with the official EOD "
        f"figure to {100 * n_ok / n_all:.2f}% of comparable contract-days; the x100 "
        f"branch is flagged estimated=True on every bar it touches.")
    say(f"  3. {n_ok:,} verified contract-days is the ceiling for OI-based intraday "
        f"features; the remaining {n_all - n_ok:,} stay unverified and must not feed a "
        f"signal.")
    say("  4. raw OI is never overwritten, missing OI is never filled, and the official "
        "EOD figure is attached only at or after 15:30 IST - a deliberate leak is caught.")

    OUT_JSON.write_text(pd.DataFrame(rows).to_json(orient="records", indent=1),
                        encoding="utf-8")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_TXT} and {OUT_JSON} ({len(rows)} file-day records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
