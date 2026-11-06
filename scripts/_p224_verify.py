"""P2.2.4 verification - integrity of the expanded raw cache + manifest aggregation.

Confirms (step 4 acceptance):
  * every published provider expiry is now cached (missing == 0),
  * every downloaded file passed byte-size and sha256 == provider LFS oid checks,
  * a full scan of ALL cached raw files for schema completeness and data quality:
    rows > 0, both CE and PE present, internal ``expiry`` == file stem, finite OHLC,
    ``high >= low``.  Any anomaly is reported (never silently dropped or fabricated).

Outputs: logs/p224_verify.txt, logs/p224_cache_quality.json
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _p224_download import (  # noqa: E402
    CACHE, MANIFEST, REQUIRED, cached_stems, tree_listing)

OUT_TXT = pathlib.Path("logs/p224_verify.txt")
OUT_JSON = pathlib.Path("logs/p224_cache_quality.json")


def main() -> int:
    lines: list[str] = []

    def say(t: str = "") -> None:
        print(t, flush=True)
        lines.append(t)

    say("P2.2.4 verification - expanded raw cache integrity")
    say("=" * 72)

    # 1. provider coverage: published vs cached
    listing = tree_listing()
    published = {e["path"].rsplit("/", 1)[-1][:-len(".parquet")] for e in listing}
    cached = cached_stems()
    missing = sorted(published - cached)
    extra = sorted(cached - published)
    say(f"published={len(published)} cached={len(cached)} "
        f"missing={len(missing)} extra_local={len(extra)}")
    if missing:
        say(f"  STILL MISSING: {missing}")
    if extra:
        say(f"  LOCAL-NOT-PUBLISHED: {extra}")

    # 2. manifest aggregation (downloaded this session)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else []
    status = collections.Counter(r["status"] for r in manifest)
    sha_ok = sum(1 for r in manifest
                 if r.get("sha256") and r.get("lfs_sha256")
                 and r["sha256"] == r["lfs_sha256"])
    size_ok = sum(1 for r in manifest
                  if r.get("downloaded_bytes") == r.get("published_size"))
    rows_new = sum(int(r.get("rows", 0)) for r in manifest if r["status"] == "eligible")
    days_new = sum(int(r.get("n_days", 0)) for r in manifest if r["status"] == "eligible")
    say(f"\nmanifest records={len(manifest)} statuses={dict(status)}")
    say(f"  sha256==LFS oid : {sha_ok}/{len(manifest)}")
    say(f"  size matched    : {size_ok}/{len(manifest)}")
    say(f"  rows downloaded : {rows_new:,}")
    say(f"  contract-days   : {days_new:,}")

    # 3. full quality scan of EVERY cached raw file
    say("\nfull cache quality scan (all cached files):")
    issues = []
    per_file = {}
    total_rows = 0
    day_set: set[str] = set()
    for p in sorted(CACHE.glob("options__NIFTY__*.parquet")):
        stem = p.stem.replace("options__NIFTY__", "")
        try:
            d = pd.read_parquet(p)
        except Exception as exc:  # noqa: BLE701
            issues.append({"expiry": stem, "issue": f"unreadable:{type(exc).__name__}"})
            continue
        cols = set(d.columns)
        rec = {"rows": int(len(d))}
        if not REQUIRED <= cols:
            issues.append({"expiry": stem, "issue": f"missing_columns:{sorted(REQUIRED - cols)}"})
            per_file[stem] = rec
            continue
        n = len(d)
        total_rows += n
        ot = set(d["option_type"].astype(str).str.strip().unique())
        exp_vals = {str(x)[:10] for x in d["expiry"].unique()}
        ohlc = d[["open", "high", "low", "close"]].astype("float64")
        days = sorted(str(x)[:10] for x in d["trading_day"].unique())
        day_set.update(days)
        hi_below = int((ohlc["high"] < ohlc["low"]).sum())
        rec.update({"n_days": len(days), "strikes": int(d["strike"].nunique()),
                    "ce": int((d["option_type"] == "CE").sum()),
                    "pe": int((d["option_type"] == "PE").sum()),
                    "first_day": days[0] if days else "", "last_day": days[-1] if days else ""})
        if n == 0:
            issues.append({"expiry": stem, "issue": "zero_rows"})
        if not {"CE", "PE"} <= ot:
            issues.append({"expiry": stem, "issue": f"option_types:{sorted(ot)}"})
        if exp_vals != {stem}:
            issues.append({"expiry": stem, "issue": f"expiry_mismatch:{sorted(exp_vals)}"})
        if hi_below:
            issues.append({"expiry": stem, "issue": f"high_below_low:{hi_below}"})
        if not ohlc.notna().all().all():
            issues.append({"expiry": stem, "issue": "null_ohlc"})
        per_file[stem] = rec

    say(f"  files scanned    : {len(per_file)}")
    say(f"  total raw rows   : {total_rows:,}")
    say(f"  distinct trading_days across cache: {len(day_set)}")
    say(f"  trading_day span : {min(day_set)} .. {max(day_set)}")
    say(f"  files with issues: {len(issues)}")
    for it in issues:
        say(f"    - {it['expiry']}: {it['issue']}")

    OUT_JSON.write_text(json.dumps(
        {"published": len(published), "cached": len(cached), "missing": missing,
         "manifest_status": dict(status), "sha_ok": sha_ok, "size_ok": size_ok,
         "rows_new": rows_new, "days_new": days_new, "total_rows": total_rows,
         "distinct_days": len(day_set), "day_span": [min(day_set), max(day_set)],
         "issues": issues, "per_file": per_file}, indent=1), encoding="utf-8")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"\nwrote {OUT_TXT} and {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
