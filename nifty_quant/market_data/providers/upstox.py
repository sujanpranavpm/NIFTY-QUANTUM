"""Upstox Analytics-API market-data provider (read-only).

Endpoints used (all read-only; no order, margin or fund endpoint is reachable from this
module by construction):

=====================================  =========================================
``GET /v2/option/contract``            unexpired expiries for an underlying
``GET /v2/option/chain``               strikes, keys, OI, volume, bid/ask, greeks
``GET /v3/feed/market-data-feed/authorize``  single-use WebSocket URL
``GET /v2/market/status/{exchange}``   authoritative exchange status
``GET /v3/market-quote/ltp``           snapshot last prices
``GET /v3/historical-candle/...``      historical / intraday candles
``GET https://assets.upstox.com/.../complete.json.gz``  instrument master
=====================================  =========================================

Constraints that shape this client
-----------------------------------
* **Rate limits are real.**  Every request passes a minimum-interval gate, and 429 / 5xx
  / transport failures are retried with exponential backoff that honours ``Retry-After``.
  A 401/403 is never retried: it is an authentication fact, not a transient one.
* **The free Analytics Token cannot read expired F&O history.**  ``historical_candles``
  exists for eligible instruments; :attr:`UpstoxProvider.CAPABILITY` says so explicitly
  and the engine documents the coverage it actually obtains rather than implying
  unlimited history.
* **A credential is never printed.**  Every error string passes through
  :func:`nifty_quant.market_data.config.redact`.
* **No implicit fallback.**  When a capability is absent the client raises rather than
  scraping NSE or silently returning an empty frame.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from nifty_quant.market_data.config import (
    AnalyticsToken,
    MarketDataConfig,
    redact,
    secret_mask,
)
from nifty_quant.market_data.instruments import CHAIN_COLUMNS, normalise_expiry
from nifty_quant.market_data.providers.base import (
    AuthenticationError,
    DEFAULT_MODE_LIMITS,
    MarketDataProvider,
    MarketPhase,
    MarketStatusSnapshot,
    ProviderAPIError,
    ProviderCapabilities,
    RateLimitError,
)

API_BASE_URL = "https://api.upstox.com"
INSTRUMENT_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz")

#: Exchange-status strings the provider publishes -> coarse phase.
_PHASE_BY_STATUS = {
    "PRE_OPEN_START": MarketPhase.PRE_OPEN,
    "PRE_OPEN_END": MarketPhase.PRE_OPEN,
    "NORMAL_OPEN": MarketPhase.OPEN,
    "NORMAL_CLOSE": MarketPhase.CLOSED,
    "CLOSING_START": MarketPhase.CLOSED,
    "CLOSING_END": MarketPhase.CLOSED,
}

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HttpResponse:
    """A minimal transport response; bodies stay bytes so gzip is explicit."""

    status: int
    headers: dict
    body: bytes
    url: str = ""

    @property
    def content_type(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return str(value)
        return ""

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderAPIError(f"response is not JSON ({exc.__class__.__name__})",
                                   status=self.status, url=self.url) from exc


class UrllibTransport:
    """Stdlib HTTP transport (no third-party HTTP dependency is added for this)."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def request(self, method: str, url: str, headers: dict | None = None,
                body: bytes | None = None) -> HttpResponse:
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={**(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return HttpResponse(status=int(response.status),
                                    headers=dict(response.headers.items()),
                                    body=response.read(),
                                    url=url)
        except urllib.error.HTTPError as exc:
            # urllib raises on >=400 but still carries the body and headers, which is
            # exactly what the retry/auth logic needs to classify the failure.
            try:
                payload = exc.read()
            except Exception:  # pragma: no cover - defensive
                payload = b""
            return HttpResponse(status=int(exc.code),
                                headers=dict(exc.headers.items()) if exc.headers else {},
                                body=payload, url=url)
        except urllib.error.URLError as exc:
            raise OSError(f"transport failure: {exc.reason}") from exc

    def close(self) -> None:  # pragma: no cover - nothing to release
        return None


def _maybe_gunzip(body: bytes, url: str, headers: dict) -> bytes:
    encoding = ""
    for key, value in (headers or {}).items():
        if key.lower() == "content-encoding":
            encoding = str(value).lower()
    if encoding == "gzip" or body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    return body


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------
class RestClient:
    """Rate-limited, retrying, caching JSON client for one read-only credential."""

    def __init__(
        self,
        token: AnalyticsToken | str,
        *,
        base_url: str = API_BASE_URL,
        transport=None,
        timeout: float = 15.0,
        min_interval: float = 0.25,
        max_attempts: int = 4,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token if isinstance(token, AnalyticsToken) else AnalyticsToken.from_value(token)
        self.base_url = base_url.rstrip("/")
        self.transport = transport if transport is not None else UrllibTransport(timeout)
        self.timeout = timeout
        self.min_interval = max(0.0, float(min_interval))
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(0.0, float(backoff_max))
        self._sleep = sleeper
        self._monotonic = monotonic
        self._last_call: float | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        #: Number of HTTP requests actually issued (cache hits excluded); reported by the
        #: CLI so rate-limit pressure is observable, not guessed.
        self.request_count = 0
        self.cache_hits = 0

    # -- credential --------------------------------------------------------
    @property
    def token_masked(self) -> str:
        return self._token.masked

    def _auth_error(self, message: str) -> AuthenticationError:
        return AuthenticationError(redact(message, (self._token.value,)))

    def _headers(self, authenticated: bool) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "nifty-quant/2.2.10.2"}
        if authenticated:
            if not self._token.value:
                raise self._auth_error(
                    "no Upstox Analytics Token configured; set UPSTOX_ANALYTICS_TOKEN "
                    "in the local .env file")
            headers["Authorization"] = f"Bearer {self._token.value}"
        return headers

    # -- pacing ------------------------------------------------------------
    def _pace(self) -> None:
        if self.min_interval <= 0:
            return
        now = self._monotonic()
        if self._last_call is not None:
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
        self._last_call = self._monotonic()

    def _backoff(self, attempt: int) -> float:
        return min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))

    # -- requests ----------------------------------------------------------
    def get_json(self, path_or_url: str, params: dict | None = None, *,
                 authenticated: bool = True, cache_ttl: float = 0.0,
                 gzipped: bool = False) -> Any:
        """GET JSON with pacing, retries and optional caching."""
        url = self._build_url(path_or_url, params)
        cache_key = f"{url}|auth={authenticated}|gz={gzipped}"
        if cache_ttl > 0:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[0] > self._monotonic():
                self.cache_hits += 1
                return cached[1]

        headers = self._headers(authenticated)
        if gzipped:
            headers["Accept-Encoding"] = "identity"

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            try:
                response = self.transport.request("GET", url, headers=headers)
            except OSError as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    raise ProviderAPIError(
                        redact(f"network failure after {attempt} attempts: {exc}",
                               (self._token.value,)), url=url) from exc
                self._sleep(self._backoff(attempt))
                continue

            self.request_count += 1
            if 200 <= response.status < 300:
                payload = _maybe_gunzip(response.body, url, response.headers) if gzipped \
                    else response.body
                parsed = self._parse(payload, response)
                if cache_ttl > 0:
                    self._cache[cache_key] = (self._monotonic() + cache_ttl, parsed)
                return parsed

            if response.status in (401, 403):
                raise self._auth_error(
                    f"provider rejected the credential (HTTP {response.status}); "
                    f"the Analytics Token is invalid or expired. "
                    f"Response: {self._snippet(response.body)}")
            if response.status == 429:
                retry_after = self._retry_after(response.headers)
                if attempt >= self.max_attempts:
                    raise RateLimitError(
                        redact(f"rate limit still active after {attempt} attempts",
                               (self._token.value,)), retry_after=retry_after)
                self._sleep(retry_after if retry_after is not None else self._backoff(attempt))
                continue
            if response.status in _RETRYABLE_STATUS:
                if attempt >= self.max_attempts:
                    last_error = ProviderAPIError(
                        f"provider error HTTP {response.status} after {attempt} attempts: "
                        f"{self._snippet(response.body)}", status=response.status, url=url)
                    break
                self._sleep(self._backoff(attempt))
                continue
            raise ProviderAPIError(
                redact(f"provider returned HTTP {response.status}: "
                       f"{self._snippet(response.body)}", (self._token.value,)),
                status=response.status, url=url)

        if isinstance(last_error, ProviderAPIError):
            raise last_error
        raise ProviderAPIError(
            redact(f"request failed: {last_error}", (self._token.value,)), url=url)

    # -- helpers -----------------------------------------------------------
    def _build_url(self, path_or_url: str, params: dict | None) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
        return url

    def _parse(self, payload: bytes, response: HttpResponse) -> Any:
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderAPIError(
                f"provider response was not JSON (HTTP {response.status})", status=response.status,
                url=response.url) from exc

    def _snippet(self, body: bytes, limit: int = 240) -> str:
        text = body[:limit].decode("utf-8", errors="replace")
        return redact(text, (self._token.value,))

    @staticmethod
    def _retry_after(headers: dict) -> float | None:
        for key, value in (headers or {}).items():
            if key.lower() == "retry-after":
                try:
                    return max(0.0, float(value))
                except (TypeError, ValueError):
                    return None
        return None

    def close(self) -> None:
        closer = getattr(self.transport, "close", None)
        if callable(closer):
            closer()


def instrument_path(key: str) -> str:
    """URL-encode an instrument key exactly once (``NSE_INDEX|Nifty 50`` -> %7C/%20)."""
    return urllib.parse.quote(str(key), safe="")


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class UpstoxProvider(MarketDataProvider):
    """Upstox Analytics-Token market data: discovery, snapshots, feed authorisation."""

    CAPABILITY = ProviderCapabilities(
        name="upstox",
        websocket=True,
        rest_snapshots=True,
        option_chain=True,
        option_greeks=True,
        historical_intraday=True,
        historical_expired_contracts=False,
        market_status=True,
        mode_limits=dict(DEFAULT_MODE_LIMITS),
        notes=("Free Analytics Token: read-only, one-year validity, no static IP for the "
               "market-data APIs. Expired F&O historical candles require Upstox Plus, so "
               "this provider reports that capability as unavailable."),
    )

    def __init__(self, token: AnalyticsToken | str | None = None, *,
                 config: MarketDataConfig | None = None, rest: RestClient | None = None,
                 transport=None) -> None:
        self.config = config or MarketDataConfig()
        if token is None:
            token = AnalyticsToken.from_env(var=self.config.token_env_var)
        self._token = token if isinstance(token, AnalyticsToken) else AnalyticsToken.from_value(token)
        self.capability = self.CAPABILITY
        self._rest = rest or RestClient(
            self._token,
            timeout=self.config.http_timeout_seconds,
            min_interval=self.config.rest_min_interval_seconds,
            max_attempts=self.config.max_attempts,
            backoff_base=self.config.backoff_base_seconds,
            backoff_max=self.config.backoff_max_seconds,
            transport=transport,
        )
        self._master: list[dict] | None = None

    # -- plumbing ----------------------------------------------------------
    @property
    def rest(self) -> RestClient:
        return self._rest

    def _data(self, payload: Any, what: str) -> Any:
        """Unwrap the provider's ``{"status": ..., "data": ...}`` envelope."""
        if not isinstance(payload, dict):
            raise ProviderAPIError(f"{what}: expected a JSON object")
        if str(payload.get("status", "")).lower() not in {"", "success"}:
            raise ProviderAPIError(
                redact(f"{what}: provider status {payload.get('status')!r}", (self._token.value,)))
        if "data" not in payload:
            raise ProviderAPIError(f"{what}: response has no 'data' field")
        return payload["data"]

    # -- discovery ---------------------------------------------------------
    def instrument_master(self, *, force: bool = False) -> list[dict]:
        """The provider's full instrument reference, cached on disk for a day.

        The master is a large public gzip (a few hundred thousand rows); it is fetched at
        most once per ``instrument_master_max_age_hours`` and cached under
        ``data/live/reference``.  A stale cache is *replaced* rather than appended to, and
        each file is written to a temporary sibling first, so an interrupted download
        cannot leave a truncated master that parses as "no NIFTY contracts".
        """
        if self._master is not None and not force:
            return self._master
        cache_path = self._master_cache_path()
        if not force and cache_path.is_file():
            age_hours = (time.time() - cache_path.stat().st_mtime) / 3600.0
            if age_hours <= self.config.instrument_master_max_age_hours:
                try:
                    self._master = json.loads(gzip.decompress(cache_path.read_bytes()))
                    return self._master
                except (OSError, json.JSONDecodeError, gzip.BadGzipFile):
                    pass  # fall through to a fresh download
        payload = self._rest.get_json(INSTRUMENT_MASTER_URL, authenticated=False,
                                      gzipped=True)
        if not isinstance(payload, list):
            raise ProviderAPIError("instrument master is not a JSON array")
        self._write_master_cache(cache_path, payload)
        self._master = payload
        return payload

    def _master_cache_path(self) -> Path:
        stamp = datetime.now(timezone.utc).date().isoformat()
        return self.config.root / "reference" / f"upstox-instruments-{stamp}.json.gz"

    def _write_master_cache(self, path: Path, payload: list[dict]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".tmp_{os.getpid()}")
            tmp.write_bytes(gzip.compress(json.dumps(payload).encode("utf-8")))
            os.replace(tmp, path)
        except OSError:
            # A cache write failure must not stop collection; the master is already in
            # memory and the next run simply downloads it again.
            pass

    def option_expiries(self, underlying_key: str) -> list[str]:
        payload = self._rest.get_json(
            "/v2/option/contract", {"instrument_key": underlying_key},
            cache_ttl=self.config.chain_cache_seconds)
        rows = self._data(payload, "option/contract")
        if not isinstance(rows, list):
            raise ProviderAPIError("option/contract: 'data' is not a list")
        expiries = sorted({normalise_expiry(row.get("expiry")) for row in rows
                           if isinstance(row, dict)} - {""})
        return expiries

    def option_chain(self, underlying_key: str, expiry: str) -> pd.DataFrame:
        payload = self._rest.get_json(
            "/v2/option/chain",
            {"instrument_key": underlying_key, "expiry_date": normalise_expiry(expiry)},
            cache_ttl=self.config.chain_cache_seconds)
        entries = self._data(payload, "option/chain")
        rows = parse_option_chain(entries, default_expiry=expiry)
        if not rows:
            return pd.DataFrame(columns=[*CHAIN_COLUMNS, "underlying_spot_price"])
        return pd.DataFrame(rows)

    # -- live feed ---------------------------------------------------------
    def authorize_feed(self) -> str:
        """A single-use ``wss://`` URL for the V3 market-data feed.

        Never cached (the URL is single-use) and never logged in full: it carries an
        authorisation token in its path, so only the scheme and host are ever reported.
        """
        payload = self._rest.get_json("/v3/feed/market-data-feed/authorize")
        data = self._data(payload, "feed authorize")
        uri = ""
        if isinstance(data, dict):
            uri = str(data.get("authorizedRedirectUri") or data.get("authorized_redirect_uri") or "")
        if not uri:
            raise ProviderAPIError("feed authorize: response carried no redirect URI")
        scheme = urllib.parse.urlsplit(uri).scheme.lower()
        if scheme not in {"wss", "ws"}:
            raise ProviderAPIError(
                f"feed authorize returned an unsupported scheme {scheme!r}; refusing to "
                f"connect")
        return uri

    # -- status ------------------------------------------------------------
    def market_status(self) -> MarketStatusSnapshot:
        exchange = self.config.exchange
        payload = self._rest.get_json(f"/v2/market/status/{urllib.parse.quote(exchange)}",
                                      cache_ttl=30.0)
        data = self._data(payload, "market/status")
        status_text = ""
        segments: dict = {}
        if isinstance(data, dict):
            status_text = str(data.get("status") or data.get("marketStatus") or "")
            raw_segments = data.get("segments") or data.get("segmentStatus") or {}
            if isinstance(raw_segments, dict):
                segments = {str(k): str(v) for k, v in raw_segments.items()}
        elif isinstance(data, list) and data:
            status_text = str(data[0].get("status") or data[0].get("marketStatus") or "")
        phase = _PHASE_BY_STATUS.get(status_text.upper(), MarketPhase.UNKNOWN)
        return MarketStatusSnapshot(
            exchange=exchange, phase=phase, raw_status=status_text,
            observed_at=datetime.now(timezone.utc), source="provider", estimated=False,
            segments=segments)

    # -- candles -----------------------------------------------------------
    def historical_candles(self, instrument_key: str, unit: str = "minutes",
                           interval: str = "1", from_date: str = "",
                           to_date: str = "") -> pd.DataFrame:
        """Provider historical candles.

        Available for instruments the free Analytics Token can read; it is **not** a
        source of expired option history (that needs Upstox Plus), and the engine never
        presents it as one.
        """
        target = normalise_expiry(to_date) if to_date else date.today().isoformat()
        start = normalise_expiry(from_date) if from_date else target
        payload = self._rest.get_json(
            f"/v3/historical-candle/{instrument_path(instrument_key)}/{unit}/{interval}/"
            f"{target}/{start}")
        return self._candles_frame(payload, instrument_key, unit, interval)

    def intraday_candles(self, instrument_key: str, unit: str = "minutes",
                         interval: str = "1") -> pd.DataFrame:
        payload = self._rest.get_json(
            f"/v3/historical-candle/intraday/{instrument_path(instrument_key)}/{unit}/{interval}")
        return self._candles_frame(payload, instrument_key, unit, interval)

    def _candles_frame(self, payload: Any, instrument_key: str, unit: str,
                       interval: str) -> pd.DataFrame:
        """Provider candles -> the repo's candle columns, explicitly labelled
        ``candle_origin='provider_supplied'`` so they are never confused with locally
        aggregated candles."""
        data = self._data(payload, "historical-candle")
        raw = data.get("candles") if isinstance(data, dict) else None
        rows: list[dict] = []
        for row in raw or []:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            stamp = pd.Timestamp(row[0])
            stamp = (stamp.tz_convert("Asia/Kolkata") if stamp.tz is not None
                     else stamp.tz_localize("Asia/Kolkata"))
            rows.append({
                "instrument_key": instrument_key,
                "timestamp_ist": stamp,
                "unit": unit,
                "interval": str(interval),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if row[5] is not None else float("nan"),
                "open_interest": float(row[6]) if len(row) > 6 and row[6] is not None
                else float("nan"),
                "source_provider": "upstox",
                "candle_origin": "provider_supplied",
            })
        columns = ["instrument_key", "timestamp_ist", "unit", "interval", "open",
                   "high", "low", "close", "volume", "open_interest",
                   "source_provider", "candle_origin"]
        return pd.DataFrame(rows, columns=columns)

    # -- snapshots ---------------------------------------------------------
    def quotes(self, instrument_keys: list[str]) -> dict:
        """Snapshot last prices for a set of instruments (one REST call)."""
        if not instrument_keys:
            return {}
        joined = ",".join(instrument_keys)
        payload = self._rest.get_json("/v3/market-quote/ltp",
                                      {"instrument_key": joined}, cache_ttl=2.0)
        data = self._data(payload, "market-quote/ltp")
        return data if isinstance(data, dict) else {}

    def close(self) -> None:
        self._rest.close()

    # -- reporting ---------------------------------------------------------
    def describe(self) -> dict:
        out = super().describe()
        out["token"] = self._token.masked
        out["token_expires_at"] = (self._token.expires_at.isoformat()
                                   if self._token.expires_at else "unknown")
        out["token_days_remaining"] = (round(self._token.days_remaining(), 1)
                                       if self._token.days_remaining() is not None else None)
        return out


# ---------------------------------------------------------------------------
# Option-chain parsing (pure, unit-tested without the network)
# ---------------------------------------------------------------------------
def _num(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return number


def parse_option_chain(entries: Any, *, default_expiry: str = "") -> list[dict]:
    """Flatten the provider's nested chain into one row per (strike, option type).

    A CE and a PE at the same strike are two separate instruments with two separate
    instrument keys; collapsing them would lose the key the feed subscribes with.  Fields
    the provider omitted become ``NaN`` - never ``0``, which would read as "zero open
    interest" rather than "not reported".
    """
    if isinstance(entries, dict):
        for key in ("data", "strikes", "chain", "option_chain"):
            if key in entries:
                entries = entries[key]
                break
    if not isinstance(entries, (list, tuple)):
        raise ProviderAPIError("option chain: 'data' is neither a list nor a wrapped list")

    rows: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        expiry = normalise_expiry(entry.get("expiry") or default_expiry)
        strike = entry.get("strike_price")
        if strike is None:
            continue
        spot = _num(entry.get("underlying_spot_price"))
        for option_type, field in (("CE", "call_options"), ("PE", "put_options")):
            option = entry.get(field) or {}
            if not isinstance(option, dict):
                continue
            market = option.get("market_data") or {}
            greeks = option.get("option_greeks") or {}
            key = option.get("instrument_key")
            if not key:
                continue
            rows.append({
                "instrument_key": str(key),
                "expiry": expiry,
                "strike": int(round(_num(strike))),
                "option_type": option_type,
                "ltp": _num(market.get("ltp")),
                "close_price": _num(market.get("close_price")),
                "volume": _num(market.get("volume")),
                "oi": _num(market.get("oi")),
                "prev_oi": _num(market.get("prev_oi")),
                "bid_price": _num(market.get("bid_price")),
                "bid_qty": _num(market.get("bid_qty")),
                "ask_price": _num(market.get("ask_price")),
                "ask_qty": _num(market.get("ask_qty")),
                "delta": _num(greeks.get("delta")),
                "theta": _num(greeks.get("theta")),
                "gamma": _num(greeks.get("gamma")),
                "vega": _num(greeks.get("vega")),
                "rho": _num(greeks.get("rho")),
                "iv": _num(greeks.get("iv")),
                "underlying_spot_price": spot,
                "source_provider": "upstox",
            })
    rows.sort(key=lambda r: (r["expiry"], r["strike"], r["option_type"]))
    return rows
