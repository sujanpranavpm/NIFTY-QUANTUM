"""P1 step 2 - read the real Parquet schema of selected dataset files.

Downloads a deliberately small representative sample (each options file is a
single expiry week, 2-6 MB) into ``data/raw/p1_sample`` which is git-ignored,
then reports the *actual* arrow schema of each file plus row-group statistics.

Nothing here reads the whole 890 MB tree.

Usage:
    python scripts/_p1_hf_schema.py [path1 path2 ...]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

REPO = "thetrademarkk/india-index-options-1m"
REV = "main"
CACHE = Path("data/raw/p1_sample")

# Representative sample spanning old / middle / recent, month-end expiries,
# both other indices and the spot index file.
DEFAULT_PATHS = [
    "options/NIFTY/2021-05-27.parquet",
    "options/NIFTY/2021-12-30.parquet",
    "options/NIFTY/2022-09-29.parquet",
    "options/NIFTY/2022-11-24.parquet",
    "options/NIFTY/2023-06-22.parquet",
    "options/NIFTY/2023-12-28.parquet",
    "options/NIFTY/2024-01-25.parquet",
    "options/NIFTY/2024-07-25.parquet",
    "options/NIFTY/2024-12-26.parquet",
    "options/NIFTY/2025-01-30.parquet",
    "options/NIFTY/2025-03-27.parquet",
    "options/NIFTY/2025-09-02.parquet",
    "options/NIFTY/2025-09-30.parquet",
    "options/NIFTY/2026-02-24.parquet",
    "options/NIFTY/2026-08-04.parquet",
    "options/BANKNIFTY/2024-12-24.parquet",
    "options/SENSEX/2024-12-26.parquet",
    "index/NIFTY.parquet",
]

UA = {"User-Agent": "nifty-quant-data-audit"}


def fetch(path: str) -> Path:
    """Download once, reuse thereafter (resumable across audit runs)."""
    target = CACHE / path.replace("/", "__")
    if target.exists() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/datasets/{REPO}/resolve/{REV}/{path}"
    tmp = target.with_suffix(".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as response:
        expected = int(response.headers.get("content-length") or 0)
        with open(tmp, "wb") as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
    if expected and tmp.stat().st_size != expected:
        raise OSError(f"truncated download: got {tmp.stat().st_size} of {expected}")
    tmp.replace(target)
    return target


def describe(path: str) -> dict:
    local = fetch(path)
    meta = pq.read_metadata(local)
    schema = pq.read_schema(local)
    columns = [f"{field.name}:{field.type}" for field in schema]

    groups = []
    for g in range(meta.num_row_groups):
        rg = meta.row_group(g)
        groups.append(
            {
                "rows": rg.num_rows,
                "bytes": sum(rg.column(c).total_compressed_size for c in range(rg.num_columns)),
                "physical": [rg.column(c).physical_type for c in range(rg.num_columns)],
                "encodings": sorted({enc for c in range(rg.num_columns) for enc in (rg.column(c).encodings or [])}),
                "codecs": sorted({rg.column(c).compression for c in range(rg.num_columns)}),
            }
        )

    stats = {}
    if meta.num_row_groups:
        rg = meta.row_group(0)
        for c in range(rg.num_columns):
            col = rg.column(c)
            column_stats = col.statistics
            stats[schema.field(c).name] = {
                "nulls": column_stats.null_count if column_stats is not None else None,
                "min": column_stats.min if column_stats is not None else None,
                "max": column_stats.max if column_stats is not None else None,
            }

    return {
        "path": path,
        "local": str(local),
        "bytes": local.stat().st_size,
        "rows": meta.num_rows,
        "columns": columns,
        "n_columns": len(columns),
        "row_groups": meta.num_row_groups,
        "created_by": meta.created_by,
        "kv_metadata": {k.decode(): v.decode() for k, v in (schema.metadata or [])},
        "first_group_stats": stats,
        "groups_summary": [
            {"rows": g["rows"], "bytes": g["bytes"], "physical": g["physical"][:12], "codecs": g["codecs"]}
            for g in groups[:3]
        ],
    }


def main() -> int:
    paths = sys.argv[1:] or DEFAULT_PATHS
    report = []
    for path in paths:
        try:
            info = describe(path)
        except Exception as exc:
            info = {"path": path, "error": f"{type(exc).__name__}: {exc}"}
        report.append(info)

        print("=" * 78)
        print(path)
        print("=" * 78)
        if "error" in info:
            print(f"  ERROR {info['error']}")
            continue
        print(f"  size={info['bytes']:,} bytes   rows={info['rows']:,}   row_groups={info['row_groups']}")
        print(f"  columns ({info['n_columns']}): {info['columns']}")
        print(f"  created_by: {info['created_by']}")
        if info["kv_metadata"]:
            print(f"  kv_metadata: {json.dumps(info['kv_metadata'], default=str)[:700]}")
        for group in info["groups_summary"]:
            print(f"    rg: rows={group['rows']:,} bytes={group['bytes']:,} codecs={group['codecs']} phys={group['physical']}")
        for name, stat in (info["first_group_stats"] or {}).items():
            print(f"      {name:16s} {stat}")

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    out = Path("logs/p1_hf_schema.json")
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
