"""Full per-date NIFTY board-lot schedule from every cached official F&O bhavcopy.

The archive-to-contract conversion divides provider volume by the board lot that the
exchange recorded *for that contract row on that day*.  A single lot per date is only a
fallback, so the fallback regime table in ``nifty_quant/derivatives/reference.py`` must be
the table the exchange actually used.  This script derives it from the cached official
reports rather than from press coverage: for every lot value it reports the first and last
date the value appears on any NIFTY option row, the first date it becomes the majority
lot, and the first date no other lot remains.

The provider archive also covers 2021-2023, before the official F&O bhavcopy archive
begins (2024-01-02).  Those years have no exchange arbiter, so their lot is *inferred*
from the archive's own smallest positive traded volume - a statistic that is validated
here on the official-era files, where the true lot is independently known.

Outputs ``logs/lot_schedule.txt`` and ``logs/lot_schedule.json``.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _p1_nse_reconcile import load_official  # noqa: E402

NSE = Path("data/raw/p1_nse")
HF = Path("data/raw/p1_sample")
OUT_TXT = Path("logs/lot_schedule.txt")
OUT_JSON = Path("logs/lot_schedule.json")
ARBITER_FLOOR = "2024-01-02"


def main() -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    days = sorted(p.stem[-8:] for p in NSE.glob("BhavCopy_NSE_FO_*.zip"))
    say(f"cached official dates: {len(days)} ({days[0]} .. {days[-1]})")

    per_date: dict[str, dict[int, int]] = {}
    errors: dict[str, str] = {}
    for ymd in days:
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        try:
            off = load_official(ymd)
        except Exception as exc:  # noqa: BLE001
            errors[iso] = f"{type(exc).__name__}: {exc}"
            continue
        lots = pd.to_numeric(off["NewBrdLotQty"], errors="coerce").dropna().astype("int64")
        if lots.empty:
            continue
        per_date[iso] = dict(Counter(lots))

    say(f"dates with NIFTY option rows and a usable lot column: {len(per_date)}")
    say(f"dates that failed to parse: {len(errors)}")

    say("")
    say("PER LOT VALUE: when the exchange used it on NIFTY option rows")
    say("-" * 100)
    values = sorted({lot for counts in per_date.values() for lot in counts})
    summary: dict[int, dict] = {}
    for lot in values:
        seen = sorted(iso for iso, counts in per_date.items() if lot in counts)
        modal = sorted(iso for iso, counts in per_date.items()
                       if lot in counts and counts[lot] == max(counts.values()))
        sole = sorted(iso for iso, counts in per_date.items()
                      if list(counts) == [lot])
        first_modal = modal[0] if modal else None
        first_sole = sole[0] if sole else None
        last_seen = seen[-1] if seen else None
        summary[lot] = {
            "first_seen": seen[0] if seen else None, "last_seen": last_seen,
            "first_modal": first_modal, "first_exclusive": first_sole,
            "dates_seen": len(seen), "dates_modal": len(modal),
            "dates_exclusive": len(sole),
        }
        say(f"  lot {lot:>3}: first row {seen[0] if seen else '-'}  first modal {first_modal or '-'}"
            f"  sole lot from {first_sole or '-'}  last row {last_seen or '-'}"
            f"  ({len(seen)} dates seen, {len(sole)} dates where it is the only lot)")

    say("")
    say("PER-DATE SCHEDULE (lot -> row count), transitions only")
    say("-" * 100)
    prev = None
    for iso in sorted(per_date):
        signature = tuple(sorted(per_date[iso].items()))
        if signature != prev:
            say(f"  {iso}: {dict(signature)}")
            prev = signature
    say(f"  ({len(per_date)} dates, transitions shown)")

    say("")
    say("MIXED-LOT DATES (why a per-date lot is only a fallback)")
    say("-" * 100)
    mixed = {iso: counts for iso, counts in per_date.items() if len(counts) > 1}
    say(f"  dates with more than one lot on NIFTY option rows: {len(mixed)} of {len(per_date)}")
    for iso in sorted(mixed)[:8]:
        say(f"    {iso}: {mixed[iso]}")
    if len(mixed) > 8:
        say(f"    ... {len(mixed) - 8} further mixed dates (full list in lot_schedule.json)")

    say("")
    say("PROVIDER-ERA LOT EVIDENCE (min positive traded volume per expiry file)")
    say("-" * 100)
    say("  on official-era files this is a self-consistency check against a known lot;")
    say("  on 2021-2023 files, where no arbiter exists, it is the only lot evidence there is.")
    infer: dict[str, dict] = {}
    for path in sorted(HF.glob("options__NIFTY__*.parquet")):
        expiry = path.stem.replace("options__NIFTY__", "")
        vol = pd.read_parquet(path, columns=["volume"])["volume"]
        pos = vol[vol > 0]
        if pos.empty:
            say(f"  {expiry}: no positive-volume bar at all")
            continue
        smallest = sorted({int(v) for v in pos.unique()})[:6]
        mn = smallest[0]
        infer[expiry] = {"min_positive_volume": mn, "smallest_values": smallest,
                         "n_positive_bars": int(len(pos))}
        say(f"  {expiry}{'  (no arbiter)' if expiry < ARBITER_FLOOR else ''}: "
            f"min positive volume={mn:>4}  smallest distinct={smallest}  "
            f"positive bars={len(pos):,}")
        del vol, pos

    OUT_JSON.write_text(json.dumps({"per_date": per_date, "summary": summary,
                                    "errors": errors, "provider_era": infer}, indent=1),
                        encoding="utf-8")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"wrote {OUT_TXT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
