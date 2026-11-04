"""yfinance provider for NSE equities.

Reality checks baked in (and reported in the UI/limitations):

* Yahoo is a *free consumer* source. It is split/dividend adjusted but it is
  not exchange-grade: the archive may contain late starts, missing sessions and
  it restates history when corporate actions occur.
* Intraday history is capped by Yahoo (roughly 30 days for 5m, 60 days for
  15m/30m/60m), so intraday strategies can only be evaluated on a short window.
"""

from __future__ import annotations

import time
from typing import Iterable

import pandas as pd

from nifty_quant.data.providers.base import DataProvider, ProviderCapability
from nifty_quant.data.schema import apply_corporate_actions, normalise_bars
from nifty_quant.utils import get_logger

LOG = get_logger("nifty_quant.provider.yfinance")

YAHOO_INTERVAL = {"1d": "1d", "1wk": "1wk", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m"}


class YFinanceProvider(DataProvider):
    """Download NSE (``*.NS``) bars through Yahoo Finance."""

    capability = ProviderCapability(
        name="yfinance",
        daily=True,
        intraday_intervals=("5m", "15m", "30m", "60m"),
        max_intraday_history_days=60,
        corporate_actions=True,
        adjusted_prices=True,
        survivorship_free_universe=False,
        notes="Free consumer feed; adjusted for splits/dividends; intraday window is ~60 days.",
    )

    def __init__(
        self,
        suffix: str = ".NS",
        retries: int = 3,
        retry_sleep: float = 1.5,
        auto_adjust: bool = False,
    ) -> None:
        self.suffix = suffix
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.auto_adjust = auto_adjust
        self._last_errors: dict[str, str] = {}

    def _yahoo_symbol(self, symbol: str) -> str:
        if symbol.startswith("^") or symbol.endswith("=X") or symbol.endswith("=F"):
            return symbol
        return symbol if symbol.endswith(self.suffix) else f"{symbol}{self.suffix}"

    def fetch(
        self,
        symbol: str,
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        import yfinance as yf  # imported lazily so the package works offline

        if interval not in YAHOO_INTERVAL:
            raise ValueError(f"unsupported interval {interval!r}")

        ticker = self._yahoo_symbol(symbol)
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                raw = yf.Ticker(ticker).history(
                    start=start,
                    end=end,
                    interval=YAHOO_INTERVAL[interval],
                    auto_adjust=self.auto_adjust,
                    actions=True,
                    raise_errors=True,
                )
                if raw is None or raw.empty:
                    raise ValueError(f"empty response for {ticker}")
                frame = normalise_bars(raw, symbol=symbol)
                frame = apply_corporate_actions(frame)
                LOG.debug("%s %s: %d bars %s..%s", symbol, interval, len(frame), frame.index[0].date(), frame.index[-1].date())
                return frame
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.retry_sleep * attempt)
        message = f"{ticker}: {last_error}"
        self._last_errors[symbol] = message
        raise RuntimeError(message) from last_error

    def fetch_many(
        self,
        symbols: Iterable[str],
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Batch download, falling back to per-symbol retries.

        A batch call is one HTTP request for ~50 tickers, which matters because
        the free endpoint rate-limits aggressively.
        """
        import yfinance as yf

        symbols = list(symbols)
        out: dict[str, pd.DataFrame] = {}
        tickers = {self._yahoo_symbol(s): s for s in symbols}
        try:
            batch = yf.download(
                list(tickers),
                start=start,
                end=end,
                interval=YAHOO_INTERVAL[interval],
                auto_adjust=self.auto_adjust,
                actions=True,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to sequential
            LOG.warning("batch download failed (%s); falling back to sequential", exc)
            batch = None

        if batch is not None and not batch.empty:
            for ticker, symbol in tickers.items():
                try:
                    sub = batch[ticker] if isinstance(batch.columns, pd.MultiIndex) else batch
                    sub = sub.dropna(how="all")
                    if sub.empty:
                        raise ValueError("no rows in batch response")
                    frame = apply_corporate_actions(normalise_bars(sub, symbol=symbol))
                    out[symbol] = frame
                except Exception as exc:  # noqa: BLE001
                    self._last_errors[symbol] = f"batch: {exc}"

        missing = [s for s in symbols if s not in out]
        for symbol in missing:
            try:
                out[symbol] = self.fetch(symbol, interval, start, end)
            except Exception as exc:  # noqa: BLE001
                self._last_errors[symbol] = str(exc)
                out[symbol] = pd.DataFrame()

        LOG.info(
            "batch %s: %d/%d symbols ok, %d failed",
            interval,
            sum(1 for f in out.values() if not f.empty),
            len(symbols),
            sum(1 for f in out.values() if f.empty),
        )
        return out
