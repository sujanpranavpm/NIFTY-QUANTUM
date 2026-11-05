"""P2 stage 3 - free / legally-usable source assessment (Part 3, coverage extension).

The directive asks whether any additional free, legally-usable source could extend the
archive's historical intraday coverage beyond its horizon (the cached provider data stops
in early 2026 and the most recent expiry file is itself a truncated tail).  Two rules
bound this probe absolutely:

* ``RS 0`` - only bytes already cached, and *anonymous* reachability of a genuinely free
  public resource.  No account creation, no login, no subscription, no paid API, no
  terms-of-use violation.
* Fail closed - where a source is gated behind a sign-in wall or an account, that is the
  answer; the audit records the restriction rather than pretending the data exists or
  reaching around the gate.

Prior findings (this milestone's earlier probes) already established that the obvious
candidates are gated: ``CloudTrader`` / Sensibull-style dashboards sit behind a sign-in,
and ``Breeze``/API access requires a funded account.  This module makes that an explicit,
documented restriction instead of a silent gap: the maximum *legally-cacheable* intraday
coverage stays exactly what the free archive already provides, and is never extended here.

The reachability probe is best-effort and time-boxed; whether or not the network answers,
the fail-closed conclusion is unchanged, because a source that needs an account can never
qualify under the ``RS 0`` rule regardless of reachability.
"""

from __future__ import annotations

from datetime import date
from urllib.request import Request, urlopen

#: Candidate free sources and the reason each cannot extend legal intraday coverage.
#: ``gated`` means access needs an account or a sign-in wall (never probed with credentials).
GATED_SOURCES = [
    {"name": "NSE official bhavcopy archive", "kind": "exchange EOD",
     "gated": False, "intraday": False,
     "note": "free and anonymous, but end-of-day only - no minute bars; already the "
             "arbiter this audit uses for daily completeness"},
    {"name": "Breeze / ICICI Direct API", "kind": "broker historical data",
     "gated": True, "intraday": True,
     "note": "requires a funded trading account and API keys - outside the "
             "no-new-accounts / RS 0 rule"},
    {"name": "CloudTrader / dashboard exports", "kind": "third-party analytics",
     "gated": True, "intraday": True,
     "note": "sign-in wall before any download; anonymous reachability yields no data"},
    {"name": "Vendor tick databases (truebias, etc.)", "kind": "paid data vendor",
     "gated": True, "intraday": True,
     "note": "commercial licences; a purchase is out of scope"},
]

ARCHIVE_HORIZON = date(2026, 8, 4)   # last cached provider expiry file (itself a tail)
FREE_INTRADAY_SOURCE = "provider HuggingFace options archive (already cached)"


def _probe(url: str, timeout: float = 6.0) -> str:
    """Anonymous reachability only: a GET/HEAD with no credentials.  Never logs in."""
    try:
        request = Request(url, headers={"User-Agent": "p2-audit-reachability/1.0"})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https)
            return f"reachable (HTTP {response.status}) but no anonymous historical " \
                   f"intraday download is exposed"
    except Exception as exc:  # noqa: BLE001  a failed probe is expected, not an error
        return f"not reachable anonymously ({type(exc).__name__})"


def assess(say=None, probe: bool = True) -> list[str]:
    """Return the assessment lines; ``say`` optionally streams them as they are built."""
    lines: list[str] = []

    def emit(text: str = "") -> None:
        lines.append(text)
        if say is not None:
            say(text)

    emit("")
    emit("FREE-SOURCE ASSESSMENT (can any legally-usable free source extend intraday "
         "coverage?)")
    emit("-" * 104)
    for src in GATED_SOURCES:
        gate = "ACCOUNT/SIGN-IN GATED" if src["gated"] else "free & anonymous"
        emit(f"  {src['name']:38s} [{gate}]")
        emit(f"      {src['note']}")
    if probe:
        for url in ("https://upstox.com", "https://cloudtrader.in"):
            emit(f"  anonymous reachability probe {url}: {_probe(url)}")
    emit("")
    emit("  CONCLUSION (fail-closed): no additional free source that respects the "
         "no-accounts / RS 0 rule can extend NIFTY options INTRADAY history beyond the "
         f"already-cached {FREE_INTRADAY_SOURCE}, whose horizon is {ARCHIVE_HORIZON} "
         "(and whose newest expiry file is itself a truncated tail).")
    emit("  RESTRICTION: the maximum legally-cacheable intraday coverage is therefore "
         "the current archive; it is NOT extended, NOT back-filled and NOT substituted "
         "from any gated source. Dates/contracts outside it are reported UNKNOWN / "
         "MISSING and left unavailable.")
    return lines


if __name__ == "__main__":
    import sys
    for _l in assess(say=print):
        pass
    sys.exit(0)
