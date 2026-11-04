"""NIFTY 50 universe handling, including the survivorship-bias boundary.

The committed snapshot in ``assets/nifty50_snapshot.csv`` was retrieved from the
NSE archives constituent file. It is a *point-in-time* snapshot: it tells us who
is a member today, not who was a member in 2012. Historical membership is
therefore an explicit, unresolved data requirement of this project:

* ``index_membership_from`` / ``index_membership_to`` are empty in the snapshot.
* When a caller supplies a populated membership file, :meth:`Universe.tradable_window`
  uses it and the engine restricts each symbol to its actual membership window
  (avoiding survivorship bias for that symbol).
* When it is empty, every symbol is tradable over its own price history and the
  run is flagged ``survivorship_bias_exposed=True`` in the validation report.
"""

from __future__ import annotations

import csv
import io
import urllib.request
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import pandas as pd

from nifty_quant.config import PATHS
from nifty_quant.utils import get_logger

LOG = get_logger("nifty_quant.universe")

NSE_CONSTITUENT_URL = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"
DEFAULT_SNAPSHOT = PATHS.universe_dir / "nifty50_snapshot.csv"
YAHOO_SUFFIX = ".NS"


@dataclass(frozen=True)
class Constituent:
    """One index member as recorded in the snapshot."""

    symbol: str
    name: str
    industry: str
    isin: str
    snapshot_date: str
    membership_from: str | None = None
    membership_to: str | None = None
    source: str = ""

    @property
    def yahoo_symbol(self) -> str:
        return f"{self.symbol}{YAHOO_SUFFIX}"

    @property
    def has_known_membership(self) -> bool:
        return bool(self.membership_from)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class Universe:
    """A named, point-in-time set of index constituents."""

    def __init__(self, constituents: list[Constituent], name: str = "NIFTY50") -> None:
        if not constituents:
            raise ValueError("universe must contain at least one constituent")
        duplicate_symbols = {c.symbol for c in constituents if [x.symbol for x in constituents].count(c.symbol) > 1}
        if duplicate_symbols:
            raise ValueError(f"duplicate symbols in universe: {sorted(duplicate_symbols)}")
        self._members: dict[str, Constituent] = {c.symbol: c for c in constituents}
        self.name = name

    # ---- construction ---------------------------------------------------
    @classmethod
    def from_snapshot(cls, path: Path | str | None = None, name: str = "NIFTY50") -> "Universe":
        path = Path(path) if path is not None else DEFAULT_SNAPSHOT
        if not path.exists():
            raise FileNotFoundError(f"universe snapshot not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        members = [
            Constituent(
                symbol=row["symbol"].strip(),
                name=row.get("name", "").strip(),
                industry=row.get("industry", "").strip(),
                isin=(row.get("isin") or "").strip(),
                snapshot_date=(row.get("snapshot_date") or "").strip(),
                membership_from=_clean(row.get("index_membership_from")),
                membership_to=_clean(row.get("index_membership_to")),
                source=(row.get("source") or "").strip(),
            )
            for row in rows
        ]
        return cls(members, name=name)

    def with_membership(self, path: Path | str) -> "Universe":
        """Return a copy enriched with an externally supplied membership file.

        The file must contain ``symbol,from_date,to_date`` columns. Symbols not
        present in the file keep their existing (unknown) window.
        """
        table = pd.read_csv(path)
        required = {"symbol", "from_date"}
        if not required.issubset(table.columns):
            raise ValueError(f"membership file must contain {sorted(required)}")
        windows = {
            str(row.symbol).strip(): (
                _clean(str(row.get("from_date", ""))),
                _clean(str(row.get("to_date", ""))) if "to_date" in table.columns else None,
            )
            for row in table.itertuples()
        }
        members = []
        for member in self._members.values():
            start, end = windows.get(member.symbol, (member.membership_from, member.membership_to))
            members.append(replace(member, membership_from=start, membership_to=end))
        return Universe(members, name=self.name)

    # ---- accessors ------------------------------------------------------
    @property
    def symbols(self) -> list[str]:
        return sorted(self._members)

    def __len__(self) -> int:
        return len(self._members)

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._members

    def member(self, symbol: str) -> Constituent:
        return self._members[symbol]

    def sectors(self) -> dict[str, str]:
        return {symbol: member.industry for symbol, member in self._members.items()}

    def yahoo_symbol(self, symbol: str) -> str:
        return self._members[symbol].yahoo_symbol

    def tradable_window(self, symbol: str) -> tuple[str | None, str | None]:
        """Inclusive membership window, or ``(None, None)`` when unknown."""
        member = self._members[symbol]
        return member.membership_from, member.membership_to

    def membership_at(self, day: str | date | pd.Timestamp) -> set[str]:
        """Symbols whose known membership window contains ``day``.

        Symbols with unknown windows are always included and flagged separately:
        excluding them silently would hide the bias instead of reporting it.
        """
        stamp = pd.Timestamp(day)
        active: set[str] = set()
        for symbol, member in self._members.items():
            if not member.has_known_membership:
                active.add(symbol)
                continue
            start = pd.Timestamp(member.membership_from)
            end = pd.Timestamp(member.membership_to) if member.membership_to else pd.Timestamp.max
            if start <= stamp <= end:
                active.add(symbol)
        return active

    @property
    def survivorship_bias_exposed(self) -> bool:
        """True when at least one symbol's membership window is unknown."""
        return any(not m.has_known_membership for m in self._members.values())

    def survivorship_report(self) -> pd.DataFrame:
        rows = []
        for symbol, member in self._members.items():
            rows.append(
                {
                    "symbol": symbol,
                    "name": member.name,
                    "industry": member.industry,
                    "membership_from": member.membership_from or "unknown",
                    "membership_to": member.membership_to or "unknown",
                    "known_membership": member.has_known_membership,
                    "snapshot_date": member.snapshot_date,
                }
            )
        return pd.DataFrame(rows).sort_values("symbol")

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": m.symbol,
                    "name": m.name,
                    "industry": m.industry,
                    "isin": m.isin,
                    "yahoo_symbol": m.yahoo_symbol,
                }
                for m in self._members.values()
            ]
        ).sort_values("symbol")


def refresh_snapshot(path: Path | str | None = None, timeout: int = 30) -> Path:
    """Download the current NIFTY 50 constituent list from the NSE archive.

    The NSE file has no membership dates, so the refresh only updates the
    point-in-time roster. It refuses to overwrite the committed asset with an
    empty or malformed payload.
    """
    target = Path(path) if path is not None else DEFAULT_SNAPSHOT
    request = urllib.request.Request(NSE_CONSTITUENT_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed NSE URL
        payload = response.read().decode("utf-8")

    rows = list(csv.DictReader(io.StringIO(payload)))
    if len(rows) < 40:
        raise RuntimeError(f"refusing to write snapshot: only {len(rows)} rows returned")

    stamp = date.today().isoformat()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "symbol",
            "name",
            "industry",
            "series",
            "isin",
            "snapshot_date",
            "index_membership_from",
            "index_membership_to",
            "source",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("Symbol", "").strip(),
                row.get("Company Name", "").strip(),
                row.get("Industry", "").strip(),
                row.get("Series", "").strip(),
                row.get("ISIN Code", "").strip(),
                stamp,
                "",
                "",
                "NSE archives ind_nifty50list.csv",
            ]
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(out.getvalue(), encoding="utf-8")
    LOG.info("refreshed universe snapshot: %s (%d rows)", target, len(rows))
    return target
