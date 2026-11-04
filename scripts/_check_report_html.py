"""Sanity checks on a generated HTML report: no leaked NaN, no missing markers."""

import sys

path = sys.argv[1] if len(sys.argv) > 1 else "reports/research_report_smoke.html"
text = open(path, encoding="utf-8").read()

needles = [
    "nan",
    "inf",
    "None",
    "pill supported",
    "pill unsupported",
    "plotly-",
    "<figure>",
    'class="data"',
    "survivorship",
    "No validation report attached",
    "not validated",
    "Validation checks",
    "Strategy inventory",
    "Strategy detail",
]
print("size:", f"{len(text):,} chars")
for needle in needles:
    print(f"  {needle!r}: {text.count(needle)}")

# A raw NaN cell would show up as the literal string inside a <td>.
import re

bad = re.findall(r"<td[^>]*>(nan|None|inf|-inf)</td>", text, flags=re.IGNORECASE)
print("suspicious cells:", len(bad), sorted(set(bad))[:5])
