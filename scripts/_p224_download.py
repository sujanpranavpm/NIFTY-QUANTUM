"""P2.2.4 stage 1/2 - enumerate and retrieve missing, ELIGIBLE NIFTY option files.

Source: the public HuggingFace dataset ``thetrademarkk/india-index-options-1m``
(licence CC-BY-NC-4.0, anonymous, no account, no cost).  This is the SAME source the
P1/P2 datasets were built from; it downloads ONLY files the repo has not cached,
NEVER overwrites an existing raw file, and verifies every retrieval before keeping it:

  * byte size == the provider's published size,
  * sha256 == the provider's LFS oid (strong content checksum),
  * the parquet opens with the required schema columns,
  * both CE and PE option_type records exist,
  * the internal ``expiry`` column matches the file's expiry stem,
  * rows > 0 and every kept row has finite OHLC with ``high >= low``.

A file that fails any check is NOT written to the cache and is recorded with its reason
(nothing is fabricated or half-saved).  Resumable: already-cached expiries are skipped.

    $env:PYTHONPATH='.'; python scripts/_p224_download.py            # all missing
    $env:PYTHONPATH='.'; python scripts/_p224_download.py --dry      # inventory only

Outputs: data/raw/p1_sample/options__NIFTY__<expiry>.parquet (new files only)
         logs/p224_download_manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import sys
import urllib.request

REPO = "thetrademarkk/india-index-options-1m"
REV = "main"
UA = {"User-Agent": "nifty-quant-data-audit"}
CACHE = pathlib.Path("data/raw/p1_sample")
MANIFEST = pathlib.Path("logs/p224_download_manifest.json")
REQUIRED = {"timestamp", "trading_day", "symbol", "expiry", "strike", "option_type",
            "open", "high", "low", "close", "volume", "open_interest"}


def _get(url: str, timeout: int = 180) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=timeout) as r:
        return r.read()


def tree_listing() -> list[dict]:
    data = json.loads(_get(f"https://huggingface.co/api/datasets/{REPO}/tree/{REV}"
                           f"/options/NIFTY"))
    return [e for e in data if e.get("type") == "file"
            and e.get("path", "").endswith(".parquet")]


def cached_stems() -> set[str]:
    return {p.stem.replace("options__NIFTY__", "") for p in
            CACHE.glob("options__NIFTY__*.parquet")}


def validate(raw: bytes, stem: str) -> tuple[bool, str, dict]:
    """Return (eligible, reason, metrics).  Never mutates the cache."""
    import pandas as pd
    try:
        df = pd.read_parquet(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        return False, f"unreadable:{type(exc).__name__}", {}
    cols = set(df.columns)
    if not REQUIRED <= cols:
        return False, f"missing_columns:{sorted(REQUIRED - cols)}", {}
    if len(df) == 0:
        return False, "zero_rows", {}
    opt_types = set(df["option_type"].astype(str).str.strip().unique())
    if not {"CE", "PE"} <= opt_types:
        return False, f"option_types:{sorted(opt_types)}", {}
    exp_vals = {str(x)[:10] for x in df["expiry"].unique()}
    if exp_vals != {stem}:
        return False, f"expiry_mismatch:{sorted(exp_vals)}", {}
    ohlc = df[["open", "high", "low", "close"]].astype("float64")
    if not ohlc.notna().all().all():
        return False, "null_ohlc", {}
    if (ohlc["high"] < ohlc["low"]).any():
        return False, "high_below_low", {}
    days = sorted(str(x)[:10] for x in df["trading_day"].unique())
    return True, "", {
        "rows": int(len(df)), "n_days": len(days), "first_day": days[0],
        "last_day": days[-1], "n_strikes": int(df["strike"].nunique()),
        "ce_rows": int((df["option_type"] == "CE").sum()),
        "pe_rows": int((df["option_type"] == "PE").sum()),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="inventory only, no download")
    args = ap.parse_args()

    listing = tree_listing()
    by_stem = {e["path"].rsplit("/", 1)[-1][:-len(".parquet")]: e for e in listing}
    cached = cached_stems()
    missing = sorted(set(by_stem) - cached)
    total_bytes = sum(int(by_stem[m].get("size") or by_stem[m].get("lfs", {}).get("size") or 0)
                      for m in missing)
    print(f"published={len(by_stem)} cached={len(cached)} missing={len(missing)} "
          f"missing_bytes={total_bytes/1e6:,.1f}MB")
    if args.dry:
        print("DRY RUN - not downloading.  First/last missing:",
              missing[0] if missing else "-", "..", missing[-1] if missing else "-")
        return 0

    records = []
    CACHE.mkdir(parents=True, exist_ok=True)
    for i, stem in enumerate(missing, 1):
        e = by_stem[stem]
        size = int(e.get("size") or e.get("lfs", {}).get("size") or 0)
        sha_expected = e.get("lfs", {}).get("oid") or e.get("oid")
        rec = {"expiry": stem, "published_size": size, "lfs_sha256": sha_expected}
        try:
            raw = _get(f"https://huggingface.co/datasets/{REPO}/resolve/{REV}/{e['path']}")
        except Exception as exc:  # noqa: BLE001
            rec.update(status="download_failed", reason=f"{type(exc).__name__}")
            records.append(rec)
            print(f"[{i}/{len(missing)}] {stem} DOWNLOAD-FAILED {rec['reason']}", flush=True)
            continue
        sha = hashlib.sha256(raw).hexdigest()
        rec["downloaded_bytes"] = len(raw)
        rec["sha256"] = sha
        if len(raw) != size:
            rec.update(status="rejected", reason=f"size_mismatch {len(raw)}!={size}")
            records.append(rec)
            print(f"[{i}/{len(missing)}] {stem} REJECT {rec['reason']}", flush=True)
            continue
        if sha_expected and sha != sha_expected:
            rec.update(status="rejected", reason="sha256_mismatch")
            records.append(rec)
            print(f"[{i}/{len(missing)}] {stem} REJECT sha256", flush=True)
            continue
        ok, reason, metrics = validate(raw, stem)
        rec.update(status="eligible" if ok else "rejected", reason=reason, **metrics)
        if ok:
            target = CACHE / f"options__NIFTY__{stem}.parquet"
            if target.exists():                       # never overwrite existing raw
                rec["status"] = "skipped_exists"
            else:
                tmp = target.with_suffix(".parquet.part")
                tmp.write_bytes(raw)
                tmp.replace(target)                   # atomic
        records.append(rec)
        tag = rec["status"]
        extra = f" rows={rec.get('rows')} days={rec.get('n_days')}" if ok else f" {reason}"
        print(f"[{i}/{len(missing)}] {stem} {tag}{extra}", flush=True)

    MANIFEST.write_text(json.dumps(records, indent=1), encoding="utf-8")
    kept = [r for r in records if r["status"] == "eligible"]
    print(f"\nKEPT {len(kept)} / {len(missing)}; "
          f"rejected {sum(1 for r in records if r['status']=='rejected')}; "
          f"download_failed {sum(1 for r in records if r['status']=='download_failed')}")
    print(f"wrote {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
