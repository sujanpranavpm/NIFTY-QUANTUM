"""Live market-data providers.

``base`` holds the provider-independent interface; ``upstox`` is the first
implementation.  Import a concrete provider through
:func:`nifty_quant.market_data.providers.base.build_provider` so adding a second
provider never requires touching a caller.
"""

from __future__ import annotations

from nifty_quant.market_data.providers.base import (
    AuthenticationError,
    ConnectionState,
    InstrumentKind,
    MarketDataError,
    MarketDataProvider,
    MarketPhase,
    MarketStatusSnapshot,
    ModeLimit,
    ProviderAPIError,
    ProviderCapabilities,
    ProviderUnsupported,
    RateLimitError,
    Subscription,
    SubscriptionLimitError,
    SubscriptionMode,
    build_provider,
    enforce_subscription_limits,
)

__all__ = [
    "AuthenticationError",
    "ConnectionState",
    "InstrumentKind",
    "MarketDataError",
    "MarketDataProvider",
    "MarketPhase",
    "MarketStatusSnapshot",
    "ModeLimit",
    "ProviderAPIError",
    "ProviderCapabilities",
    "ProviderUnsupported",
    "RateLimitError",
    "Subscription",
    "SubscriptionLimitError",
    "SubscriptionMode",
    "build_provider",
    "enforce_subscription_limits",
]
