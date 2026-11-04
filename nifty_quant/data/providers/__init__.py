"""Provider registry: name -> constructor."""

from __future__ import annotations

from typing import Callable

from nifty_quant.data.providers.base import DataProvider
from nifty_quant.data.providers.csv_provider import CSVProvider
from nifty_quant.data.providers.yfinance_provider import YFinanceProvider

PROVIDERS: dict[str, Callable[..., DataProvider]] = {
    "yfinance": YFinanceProvider,
    "csv": CSVProvider,
}


def build_provider(name: str, **kwargs) -> DataProvider:
    try:
        factory = PROVIDERS[name]
    except KeyError as exc:
        raise ValueError(f"unknown provider {name!r}; available: {sorted(PROVIDERS)}") from exc
    return factory(**kwargs)
