"""P1 step 3 - full file-listing coverage audit of the HF dataset.

Lists every object under ``index/``, ``options/NIFTY``, ``options/BANKNIFTY``
and ``options/SENSEX`` through the HF API (metadata only, no market data), then
computes date-range, cadence, weekday and gap statistics.

Writes ``logs/p1_hf_coverage.json`` and prints a readable summary.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta

REPO = "thetrademarkk/india-index-options-1m"
BASE = f"https://huggingface.co/api/datasets/{REPO}"
UA = {"User-Agent": "nifty-quant-data-audit"}

TARGETS = ["index", "options/NIFTY", "options/BANKNIFTY", "options/SENSEX"]

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def list_dir(path: str) -> list[dict]:
    out: list[dict] = []
    query: dict[str, str] = {"recursive": "false", "limit": "1000"}
    while True:
        url = f"{BASE}/tree/main/{path}?{urllib.parse.urlencode(query)}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as response:
            payload = json.load(response)
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        out.extend(entries)
        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not cursor or not entries:
            break
        query["cursor"] = cursor
    return entries


def size_of(entry: dict) -> int:
    if entry.get("lfs"):
        return int(entry["lfs"].get("size") or 0)
    return int(entry.get("size") or 0)


def parse_date(stem: str) -> date | None:
    try:
        return datetime.strptime(stem, "%Y-%m-%d").date()
    except ValueError:
        return None


def month_end(d: date) -> bool:
    """True when the date is the last Thursday of its month (2021-2025 NIFTY
    monthly-expiry convention) - used only to *label* candidates, not to assert
    exchange truth, which NSE reconciliation later checks."""
    if d.weekday() != 3:
        return False
    return d + timedelta(days=7) > d.replace(day=28)


def main() -> int:
    report: dict[str, object] = {}
    for target in TARGETS:
        entries = [e for e in list_dir(target) if e.get("type") != "directory"]
        files = []
        for entry in sorted(entries, key=lambda e: e["path"]):
            name = entry["path"].split("/")[-1]
            stem = name.rsplit(".", 1)[0]
            files.append(
                {
                    "path": entry["path"],
                    "name": name,
                    "stem": stem,
                    "bytes": size_of(entry),
                    "date": str(parse_date(stem) or ""),
                }
            )
        total = sum(f["bytes"] for f in files)
        dated = [f for f in files if f["date"]]
        dates = sorted(datetime.strptime(f["date"], "%Y-%m-%d").date() for f in dated)
        block: dict[str, object] = {
            "n_files": len(files),
            "total_bytes": total,
            "total_mb": round(total / 1048576, 1),
            "files": files,
        }
        if dates:
            block["earliest"] = str(dates[0])
            block["latest"] = str(dates[-1])
            block["span_days"] = (dates[-1] - dates[0]).days
            block["weekday_counts"] = dict(Counter(WEEKDAYS[d.weekday()] for d in dates))
            block["year_counts"] = dict(sorted(Counter(d.year for d in dates).items()))
            gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            block["gap_days"] = dict(sorted(Counter(gaps).items()))
            block["largest_gaps"] = [
                {"from": str(dates[i]), "to": str(dates[i + 1]), "days": gaps[i]}
                for i in range(len(gaps))
                if gaps[i] > 10
            ]
            block["month_end_candidates"] = sum(1 for d in dates if month_end(d))
            block["non_month_end"] = sum(1 for d in dates if not month_end(d))
        print("=" * 78)
        print(target)
        print("=" * 78)
        for key in (
            "n_files", "total_mb", "earliest", "latest", "span_days",
            "weekday_counts", "year_counts", "gap_days",
            "month_end_candidates", "non_month_end",
        ):
            if key in block:
                print(f"  {key:22s} {block[key]}")
        if block.get("largest_gaps"):
            print(f"  gaps > 10 days        {block['largest_gaps']}")
        report[target] = block

    out = "logs/p1_hf_coverage.json"
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
