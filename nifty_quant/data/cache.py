"""Parquet cache with explicit provenance metadata.

Every cached file carries the provider name, fetch timestamp, row count and
coverage window. That metadata is what makes a result set reproducible: a CSV
export can point back at the exact bars that produced it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from nifty_quant.config import PATHS
from nifty_quant.data.providers.base import DataProvider
from nifty_quant.data.schema import normalise_bars
from nifty_quant.utils import get_logger

LOG = get_logger("nifty_quant.cache")


@dataclass(frozen=True)
class CacheEntry:
    symbol: str
    interval: str
    source: str
    fetched_at: str
    rows: int
    start: str
    end: str

    def as_row(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "rows": self.rows,
            "start": self.start,
            "end": self.end,
        }


class BarStore:
    """Read/write canonical bar frames under ``data/cache``."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else PATHS.cache
        self.root.mkdir(parents=True, exist_ok=True)
        # Explicit provenance overrides (e.g. merged vendor files).
        self._overrides: dict[tuple[str, str], str] = {}

    # ---- paths ----------------------------------------------------------
    def _dir(self, interval: str) -> Path:
        path = self.root / interval
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path_for(self, symbol: str, interval: str) -> Path:
        safe = symbol.replace("/", "_").replace("^", "IDX_").replace("&", "_")
        return self._dir(interval) / f"{safe}.parquet"

    def _meta_path(self, symbol: str, interval: str) -> Path:
        safe = symbol.replace("/", "_").replace("^", "IDX_").replace("&", "_")
        return self._dir(interval) / f"{safe}.json"

    # ---- io -------------------------------------------------------------
    def exists(self, symbol: str, interval: str) -> bool:
        return self.path_for(symbol, interval).exists()

    def save(self, symbol: str, interval: str, frame: pd.DataFrame, source: str) -> None:
        if frame.empty:
            raise ValueError(f"refusing to cache an empty frame for {symbol} {interval}")
        frame = normalise_bars(frame, symbol=symbol)
        frame.to_parquet(self.path_for(symbol, interval), compression="snappy")
        meta = {
            "symbol": symbol,
            "interval": interval,
            "source": self._overrides.get((symbol, interval), source),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows": int(len(frame)),
            "start": frame.index[0].isoformat(),
            "end": frame.index[-1].isoformat(),
        }
        self._meta_path(symbol, interval).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def load(self, symbol: str, interval: str) -> pd.DataFrame:
        path = self.path_for(symbol, interval)
        if not path.exists():
            raise FileNotFoundError(f"no cached data for {symbol} {interval} ({path})")
        frame = pd.read_parquet(path)
        return normalise_bars(frame, symbol=symbol)

    def metadata(self, symbol: str, interval: str) -> CacheEntry | None:
        path = self._meta_path(symbol, interval)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return CacheEntry(**payload)

    def manifest(self, interval: str = "1d") -> pd.DataFrame:
        rows = []
        for path in sorted(self._dir(interval).glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows.append(CacheEntry(**payload).as_row())
            except Exception:  # noqa: BLE001 - a corrupt sidecar must not break the manifest
                LOG.warning("unreadable cache metadata: %s", path)
        if not rows:
            return pd.DataFrame(columns=list(CacheEntry("", "", "", "", 0, "", "").as_row()))
        return pd.DataFrame(rows).sort_values("symbol")

    # ---- fetch-through --------------------------------------------------
    def get(
        self,
        symbol: str,
        interval: str = "1d",
        provider: DataProvider | None = None,
        start: str | None = None,
        end: str | None = None,
        force: bool = False,
        max_age_days: int = 30,
    ) -> pd.DataFrame:
        """Return cached bars, downloading only when needed.

        A cache hit is only reused when it already covers the requested window;
        otherwise the provider is queried and the cache is refreshed.
        """
        if not force and self.exists(symbol, interval):
            try:
                frame = self.load(symbol, interval)
                meta = self.metadata(symbol, interval)
                fresh = True
                if meta is not None:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(meta.fetched_at)
                    fresh = age.days <= max_age_days
                covers_start = start is None or frame.index[0] <= pd.Timestamp(start) + pd.Timedelta(days=7)
                covers_end = end is None or frame.index[-1] >= pd.Timestamp(end) - pd.Timedelta(days=7)
                if fresh and covers_start and covers_end:
                    return self.slice(frame, start, end)
            except Exception as exc:  # noqa: BLE001 - fall through to refetch
                LOG.warning("cache read failed for %s %s (%s); refetching", symbol, interval, exc)

        if provider is None:
            raise RuntimeError(f"{symbol} {interval} not cached and no provider supplied")
        frame = provider.fetch(symbol, interval, start, end)
        self._overrides[(symbol, interval)] = provider.capability.name
        self.save(symbol, interval, frame, provider.capability.name)
        return self.slice(frame, start, end)

    def get_many(
        self,
        symbols: list[str],
        interval: str = "1d",
        provider: DataProvider | None = None,
        start: str | None = None,
        end: str | None = None,
        force: bool = False,
        max_age_days: int = 30,
    ) -> dict[str, pd.DataFrame]:
        """Cache-first batch load; one provider call for whatever is missing."""
        out: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for symbol in symbols:
            if force or not self.exists(symbol, interval):
                missing.append(symbol)
                continue
            try:
                frame = self.load(symbol, interval)
                meta = self.metadata(symbol, interval)
                if meta is not None:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(meta.fetched_at)
                    if age.days > max_age_days:
                        missing.append(symbol)
                        continue
                out[symbol] = self.slice(frame, start, end)
            except Exception:  # noqa: BLE001
                missing.append(symbol)

        if missing:
            if provider is None:
                LOG.warning("no provider available for %d uncached symbols", len(missing))
            else:
                fetched = provider.fetch_many(missing, interval, start, end)
                for symbol, frame in fetched.items():
                    if frame is None or frame.empty:
                        continue
                    self._overrides[(symbol, interval)] = provider.capability.name
                    self.save(symbol, interval, frame, provider.capability.name)
                    out[symbol] = self.slice(frame, start, end)
        return {symbol: frame for symbol, frame in out.items() if not frame.empty}

    @staticmethod
    def slice(frame: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
        out = frame
        if start is not None:
            out = out[out.index >= pd.Timestamp(start)]
        if end is not None:
            out = out[out.index < pd.Timestamp(end)]
        return out
