"""P2 stage 1 - open-interest availability trend, measured against the official arbiter.

Why
---
The remediation directive states as a *current finding* that "OI availability
deteriorates substantially in 2026".  That claim must be measured, not repeated.  This
script rolls up, per calendar quarter, how often the provider carries an actual
open-interest observation for a contract the exchange reports with positive open
interest - the honest denominator for any OI-based intraday feature.

Definitions (deliberately kept apart, per the milestone rules)
--------------------------------------------------------------
* provider OI observation  - a bar with ``open_interest != 0``.  The provider never
  emits NaN open interest (verified: 0 NaN in ``logs/m3_oi_norm.txt``), so a zero is an
  *absent observation*, never a filled one.
* exchange positive        - an official row with ``OpnIntrst > 0`` for that contract-day
  (share equivalents; the official report uses that unit on both sides of the break).
* availability            - contract-days where the provider's last-bar converted OI is
  positive, as a share of contract-days the exchange reports positive.
* independent verification - converted provider last-bar OI within 1% of the official
  figure (``units.mark_verified_oi``); the ceiling for OI-driven signals.

Only days with an official arbiter (>= 2024-01-02) are counted; earlier provider days
have no exchange truth and are excluded rather than guessed.

Usage (from the repo root):
    python scripts/_p2_oi_availability.py
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

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT_TXT = Path("logs/p2_oi_availability.txt")
OUT_JSON = Path("logs/p2_oi_availability.json")

COLS = ["timestamp", "open_interest", "trading_day", "symbol", "strike",
        "option_type", "expiry"]


def load_expiry(expiry: str) -> pd.DataFrame:
    path = HF / f"options__NIFTY__{expiry}.parquet"
    frame = pd.read_parquet(path, columns=COLS)
    frame = frame.drop_duplicates(
        subset=["symbol", "expiry", "strike", "option_type", "timestamp"], keep="first")
    frame["strike"] = frame["strike"].astype("int64")
    return frame.sort_values("timestamp", kind="stable")


def quarter(iso: str) -> str:
    year, month = int(iso[:4]), int(iso[5:7])
    return f"{year}Q{(month - 1) // 3 + 1}"


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say("P2 stage 1 - open-interest availability trend (official arbiter days only)")
    say("=" * 104)

    day_list = sorted({p.stem[-8:] for p in NSE.glob("BhavCopy_NSE_FO_*.zip")})
    have = {f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in day_list}
    expiries = sorted(p.stem.replace("options__NIFTY__", "")
                      for p in HF.glob("options__NIFTY__*.parquet"))
    say(f"cached official dates: {len(day_list)} ({day_list[0]} .. {day_list[-1]})")
    say(f"local NIFTY expiry files: {len(expiries)}")

    official_cache: dict[str, pd.DataFrame] = {}

    def official_for(ymd: str) -> pd.DataFrame | None:
        if ymd in official_cache:
            return official_cache[ymd] if not official_cache[ymd].empty else None
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

    # Per contract-day accumulation, keyed by ISO date so the quarter rollup is exact.
    # Day-scoped joins throughout (see scripts/_m5_completeness.py: merging on
    # (strike, option_type) alone fans every official row across every day).
    daily: dict[str, dict] = {}
    for expiry in expiries:
        frame = load_expiry(expiry)
        frame["day"] = frame["trading_day"].astype(str).str.slice(0, 10)
        last_oi = frame.groupby(["day", "strike", "option_type"], observed=True)[
            "open_interest"].last().rename("last_oi").reset_index()
        bars_per = frame.groupby(["day", "strike", "option_type"], observed=True)[
            "open_interest"].agg(n="size", nonzero=lambda s: int((s != 0).sum())).reset_index()
        last_by_day = {d: g.drop(columns="day") for d, g in last_oi.groupby("day")}
        bars_by_day = {d: g.drop(columns="day") for d, g in bars_per.groupby("day")}
        for iso in sorted(frame["day"].unique()):
            if iso not in have or date.fromisoformat(iso) < U.OFFICIAL_ARBITER_FLOOR:
                continue
            off = official_for(iso.replace("-", ""))
            if off is None:
                continue
            off = off[off["expiry"] == expiry].copy()  # this expiry only
            if off.empty:
                continue  # this expiry not listed that day -> inactive, not a gap
            off["official_oi"] = pd.to_numeric(off["OpnIntrst"], errors="coerce")
            cells = off[["strike", "option_type", "official_oi"]].merge(
                last_by_day[iso], on=["strike", "option_type"], how="left").merge(
                bars_by_day[iso][["strike", "option_type", "n", "nonzero"]],
                on=["strike", "option_type"], how="left")
            conv = np.array([
                U.oi_to_share_equivalent(float(v), date.fromisoformat(iso)).value
                if pd.notna(v) else np.nan for v in cells["last_oi"]])
            cells["converted_oi"] = conv
            positive_official = cells["official_oi"].fillna(0) > 0
            provider_positive = cells["converted_oi"] > 0  # NaN -> False
            within = (np.abs(cells["converted_oi"].to_numpy(float)
                              - cells["official_oi"].to_numpy(float))
                      <= 0.01 * np.maximum(cells["official_oi"].to_numpy(float), 1.0))
            rec = daily.setdefault(iso, {
                "expiry": expiry, "contract_days": 0, "official_positive": 0,
                "provider_positive": 0, "missing_observation": 0, "verified": 0,
                "bars": 0, "bars_nonzero": 0})
            rec["contract_days"] += int(len(cells))
            rec["official_positive"] += int(positive_official.sum())
            rec["provider_positive"] += int((positive_official & provider_positive).sum())
            rec["missing_observation"] += int((positive_official & ~provider_positive).sum())
            rec["verified"] += int((positive_official & provider_positive
                                    & np.nan_to_num(within)).sum())
            rec["bars"] += int(cells["n"].fillna(0).sum())
            rec["bars_nonzero"] += int(cells["nonzero"].fillna(0).sum())
        del frame

    say("")
    say("PER-QUARTER OPEN-INTEREST AVAILABILITY (contract-day view, official-positive rows only)")
    say("-" * 104)
    say(f"{'quarter':8s} {'dates':>5s} {'exp-days':>8s} {'offPos':>7s} {'provPos':>8s} "
        f"{'avail%':>7s} {'missing':>8s} {'verified%':>9s} {'barOI>0%':>9s}")
    by_q: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for iso, r in daily.items():
        q = quarter(iso)
        agg = by_q[q]
        agg["dates"] += 1
        for key in ("contract_days", "official_positive", "provider_positive",
                    "missing_observation", "verified", "bars", "bars_nonzero"):
            agg[key] += r[key]
    for q in sorted(by_q):
        a = by_q[q]
        off = a["official_positive"] or 1
        bars = a["bars"] or 1
        say(f"{q:8s} {a['dates']:>5d} {a['contract_days']:>8d} {a['official_positive']:>7d} "
            f"{a['provider_positive']:>8d} {100 * a['provider_positive'] / off:>6.1f}% "
            f"{a['missing_observation']:>8d} "
            f"{100 * a['verified'] / off:>8.1f}% {100 * a['bars_nonzero'] / bars:>8.1f}%")

    say("")
    say("2026 VERSUS EARLIER - availability of a provider observation where the exchange reports OI")
    say("-" * 104)

    def group_years(pred) -> tuple[int, int, int, int]:
        off = prov = miss = ver = 0
        for iso, r in daily.items():
            if not pred(iso):
                continue
            off += r["official_positive"]
            prov += r["provider_positive"]
            miss += r["missing_observation"]
            ver += r["verified"]
        return off, prov, miss, ver

    for label, pred in (("pre-2026 (2024-01-02 .. 2025-12-31)",
                         lambda iso: iso < "2026-01-01"),
                        ("2026 on" , lambda iso: iso >= "2026-01-01")):
        off, prov, miss, ver = group_years(pred)
        if not off:
            say(f"  {label}: no arbiter-positive contract-days")
            continue
        say(f"  {label}: exchange-positive {off:,}, provider-observed {prov:,} "
            f"({100 * prov / off:.1f}%), missing observation {miss:,} "
            f"({100 * miss / off:.1f}%), independently verified {ver:,} "
            f"({100 * ver / off:.1f}%)")

    pre_off, pre_prov, _, _ = group_years(lambda iso: iso < "2026-01-01")
    post_off, post_prov, _, _ = group_years(lambda iso: iso >= "2026-01-01")
    say("")
    if pre_off and post_off:
        drop = 100 * pre_prov / pre_off - 100 * post_prov / post_off
        say(f"VERDICT: provider OI-observation availability on exchange-positive contract-days "
            f"is {100 * pre_prov / pre_off:.1f}% before 2026 and {100 * post_prov / post_off:.1f}% "
            f"in 2026 -> a {drop:+.1f} percentage-point change. "
            f"{'CONFIRMED: availability deteriorates in 2026.' if drop > 5 else 'NOT CONFIRMED at the 5pp bar; the finding is overstated for this sample.'}")
    say("Availability here means a NON-ZERO provider observation exists, never that the value "
        "is correct; correctness is the separate 1% verification column above.")
    say("No OI is manufactured, filled or forward/backward-filled: a missing observation stays "
        "a missing observation.")

    OUT_JSON.write_text(pd.DataFrame(
        [{"date": iso, **r} for iso, r in sorted(daily.items())]).to_json(
        orient="records", indent=1), encoding="utf-8")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_TXT} and {OUT_JSON} ({len(daily)} arbiter file-days)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
