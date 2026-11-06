"""P2.1 - fetch and excerpt the official charge documents (Rs 0, anonymous).

Downloads the official primary sources named by the P2.1 review (plus the Finance
Bills that carry the STT amendments), extracts their text with pypdf and writes
keyword excerpts so every rate adopted later can be quoted from a real document.

Nothing in nifty_quant/ is touched here; this script only produces evidence under
data/raw/p21_charges/ (git-ignored) and logs/p21_* (git-ignored).

Usage (from the repo root):
    python scripts/_p21_charge_sources.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "raw" / "p21_charges"
LOG_DIR = ROOT / "logs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")}

TARGETS = [
    ("FA73061.pdf",
     "https://nsearchives.nseindia.com/content/circulars/FA73061.pdf"),
    ("FA64323.pdf",
     "https://nsearchives.nseindia.com/content/circulars/FA64323.pdf"),
    ("FA64232.pdf",
     "https://nsearchives.nseindia.com/content/circulars/FA64232.pdf"),
    ("FA64323_faop.pdf",
     "https://nsearchives.nseindia.com/content/circulars/FAOP64323.pdf"),
    ("FA64232_faop.pdf",
     "https://nsearchives.nseindia.com/content/circulars/FAOP64232.pdf"),
    ("FA56129.pdf",
     "https://nsearchives.nseindia.com/content/circulars/FA56129.pdf"),
    ("FA46730.pdf",
     "https://nsearchives.nseindia.com/content/circulars/FA46730.pdf"),
    ("finance_bill_2024_25.pdf",
     "https://www.indiabudget.gov.in/budget2024-25/doc/Finance_Bill.pdf"),
    ("finance_bill_2025_26.pdf",
     "https://www.indiabudget.gov.in/budget2025-26/doc/Finance_Bill.pdf"),
    ("finance_bill_2026_27.pdf",
     "https://www.indiabudget.gov.in/budget2026-27/doc/Finance_Bill.pdf"),
    ("nse_sebi_stt_levies.html",
     "https://www.nseindia.com/static/invest/"
     "first-time-investor-sebi-turnover-fees-stt-other-levies"),
    ("nse_stamp_duty.html",
     "https://www.nseindia.com/static/invest/"
     "first-time-investor-stamp-duty-charges-taxes"),
    ("nse_sebi_turnover.html",
     "https://www.nseindia.com/static/trade/members-sebi-turnover-fees"),
    ("nse_stt_page.html",
     "https://www.nseindia.com/static/products-services/"
     "equity-derivatives-securities-transaction-tax"),
]

KEY = re.compile(r"(crore|lakh|IPFT|investor protection|transaction charge|turnover fee|"
                 r"turnover charge|securities transaction tax|\bSTT\b|stamp duty|"
                 r"\bSEBI\b|supersession|supersed|0\.0625|0\.15\s*%|0\.1\s*%|"
                 r"3,?[0-9]{3}|w\.?e\.?f|with effect|September 27|option in securities)", re.IGNORECASE)
REF = re.compile(r"\b(?:NSE/)?(?:FAOP|FA|MRD|CD)\s?/?\s?\d{4,6}\b")
DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b")


def fetch(name: str, url: str, say) -> Path | None:
    dest = OUT_DIR / name
    if dest.exists() and dest.stat().st_size > 0:
        say(f"  {name}: already cached ({dest.stat().st_size} bytes)")
        return dest
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            say(f"  {name}: HTTP {resp.status} {len(body)} bytes")
    except Exception as exc:  # noqa: BLE001 - evidence log must record the failure
        say(f"  {name}: FETCH FAILED {type(exc).__name__}: {exc}")
        return None
    dest.write_bytes(body)
    return dest


def pdf_text(path: Path) -> list[str]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - a bad page must not kill the audit
            pages.append("")
    return pages


def excerpt(name: str, pages: list[str], say) -> None:
    out = LOG_DIR / f"p21_excerpts_{Path(name).stem}.txt"
    lines: list[str] = []
    for no, text in enumerate(pages, 1):
        for raw in text.splitlines():
            line = raw.strip()
            if len(line) > 3 and KEY.search(line):
                lines.append(f"[p{no}] {line}")
    out.write_text("\n".join(lines), encoding="utf-8")
    say(f"  {name}: {len(pages)} pages, {len(lines)} keyword lines -> {out.name}")
    refs = sorted({m for t in pages for m in REF.findall(t)})
    dates = sorted({m for t in pages for m in DATE.findall(t)})
    say(f"    circular refs seen: {refs[:24]}")
    say(f"    dates seen: {dates[:40]}")


def main() -> int:
    lines: list[str] = []

    def say(text=""):
        print(text, flush=True)
        lines.append(text)

    say("P2.1 - official charge document fetch (Rs 0, anonymous)")
    say("=" * 100)
    for name, url in TARGETS:
        say(f"fetching {url}")
        path = fetch(name, url, say)
        if path is None:
            continue
        if name.endswith(".pdf"):
            try:
                excerpt(name, pdf_text(path), say)
            except Exception as exc:  # noqa: BLE001
                say(f"  {name}: TEXT EXTRACTION FAILED {type(exc).__name__}: {exc}")
        else:
            body = path.read_text(encoding="utf-8", errors="replace")
            say(f"  {name}: {len(body)} chars of html saved")
            excerpt(name, html_lines(body), say)
    (LOG_DIR / "p21_fetch.txt").write_text("\n".join(lines), encoding="utf-8")
    return 0


def html_lines(body: str) -> list[str]:
    import html as _html
    import re as _re
    text = _re.sub(r"<script.*?</script>", "", body, flags=_re.S)
    text = _re.sub(r"<style.*?</style>", "", text, flags=_re.S)
    text = _re.sub(r"<[^>]+>", "\n", text)
    text = _html.unescape(text)
    return text.splitlines()


if __name__ == "__main__":
    raise SystemExit(main())
