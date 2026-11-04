"""P1 step 15b - actually fetch a Cloud Trader Pro sample and read the licence.

The dataset pages link their free samples to Google Drive, so the advertised
schema has to be checked against a real file rather than the marketing table.
This also pulls the machine-readable licence from the page's JSON-LD block, which
is what decides whether the data may be used in this project at all.

Usage:
    python scripts/_p1_cloudtrader_fetch.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
from curl_cffi import requests as curl

RAW = Path("data/raw/p1_cloudtrader")
OUT = Path("logs/p1_cloudtrader_sample.json")

DRIVE_IDS = [
    "1-GRg9-ScM11OhxhxoovgXrLw6zFqDed7",
    "14XrexravJBnpqSQqnu_8GtaT62Kp8EW6",
    "158uPiPp4xvtR-ueziJPMvXE2gMug6wk9",
    "1XtYhBdsofjGqPvq91_RyIsZ7sXRkcKJl",
    "1_cYjZwk6bc-3c-NCX4LiWUth66vr2D4f",
    "1tR5eFl99qBM-hF7XHKTexR-bGEK0AOlv",
]


def licence_from_pages() -> dict:
    found: dict[str, set[str]] = {}
    for html in RAW.glob("*.html"):
        text = html.read_text(encoding="utf-8", errors="replace")
        found.setdefault("license_url", set()).update(re.findall(r"https://creativecommons\.org[^\"' ]+", text))
        found.setdefault("schema_licenses", set()).update(re.findall(r'"license"\s*:\s*"([^"]+)"', text))
        found.setdefault("og_description", set()).update(re.findall(r'"description"\s*:\s*"([^"]{40,300})"', text))
    return {k: sorted(v) for k, v in found.items()}


def drive_probe(session, file_id: str) -> dict:
    """Ask Drive for the file header only, so size and name are known before
    anything is downloaded.

    drive.google.com/uc returns an interstitial HTML page for any file large
    enough to need a scan confirmation, so the direct usercontent endpoint with
    confirm=t is used instead.
    """
    urls = [
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}",
    ]
    for url in urls:
        try:
            response = session.get(url, timeout=90, headers={"Range": "bytes=0-0"},
                                   allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            continue
        ctype = response.headers.get("Content-Type", "")
        info = {
            "id": file_id,
            "endpoint": url.split("/")[2],
            "status": response.status_code,
            "final_url": str(response.url)[:200],
            "content_range": response.headers.get("Content-Range"),
            "content_length": response.headers.get("Content-Length"),
            "content_disposition": response.headers.get("Content-Disposition"),
            "content_type": ctype,
            "magic": response.content[:4].hex(),
        }
        if "text/html" not in ctype:
            return info
        # an HTML interstitial still names the file, which is evidence on its own
        body = response.text[:200_000]
        m = re.search(r"([A-Za-z0-9._ -]+\.(?:csv|zip|parquet))", body, re.I)
        info["interstitial_title"] = (re.search(r"<title>([^<]+)</title>", body) or [None, ""])[1]
        info["interstitial_filename"] = m.group(1) if m else None
        info["interstitial_hint"] = re.findall(r"Download anyway|cannot scan|virus", body, re.I)[:3]
    return info


def main() -> int:
    print("=== licence / terms stated by the pages themselves ===")
    lic = licence_from_pages()
    for key, values in lic.items():
        print(f"  {key}:")
        for value in values:
            print(f"     {value[:300]}")

    session = curl.Session(impersonate="chrome124")
    print("\n=== Google Drive sample files: metadata before download ===")
    probes = []
    for file_id in DRIVE_IDS:
        info = drive_probe(session, file_id)
        size = info.get("content_range") or info.get("content_length")
        print(f"  {file_id[:18]}  HTTP {info.get('status')}  {info.get('content_type')}  "
              f"range={size}  name={info.get('content_disposition')}  "
              f"title={info.get('interstitial_title')}  via={info.get('endpoint')}")
        probes.append(info)

    # Download one small CSV sample if any file reports a byte size we can trust.
    downloaded = None
    for info in probes:
        total = None
        if info.get("content_range"):
            total = int(info["content_range"].split("/")[-1])
        elif info.get("content_length"):
            try:
                total = int(info["content_length"])
            except ValueError:
                total = None
        if not total or total > 300_000_000:
            continue
        target = RAW / f"sample_{info['id'][:12]}.bin"
        got = session.get(
            f"https://drive.usercontent.google.com/download?id={info['id']}"
            "&export=download&confirm=t", timeout=600)
        target.write_bytes(got.content)
        print(f"\n  downloaded {target.name}  {len(got.content):,}B  magic={got.content[:4]!r}")
        downloaded = {"file": str(target), "bytes": len(got.content), "id": info["id"],
                      "declared_total": total, "disposition": info.get("content_disposition")}
        break
    if downloaded is None:
        print("\n  no file within the 300 MB inspection budget; samples left undownloaded")

    out = {"licence": lic, "drive_probe": probes, "downloaded": downloaded}
    if downloaded:
        path = Path(downloaded["file"])
        head = path.read_bytes()[:4]
        if head[:2] == b"PK":
            import zipfile

            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                print(f"  zip archive with {len(names)} entries, first: {names[:6]}")
                with archive.open(names[0]) as handle:
                    frame = pd.read_csv(handle)
        else:
            try:
                frame = pd.read_csv(path)
            except Exception as exc:  # noqa: BLE001
                print(f"  not a CSV: {type(exc).__name__}: {exc}")
                frame = None
        if frame is not None:
            print("\n=== real columns in the downloaded sample ===")
            print(list(frame.columns))
            print(frame.head(3).to_string())
            print(f"rows={len(frame):,}")
            out["sample_columns"] = list(map(str, frame.columns))
            out["sample_rows"] = int(len(frame))
    OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
