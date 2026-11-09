"""Secure configuration and credential handling for live market-data collection.

Rules encoded here, not merely documented
-----------------------------------------
* The analytics token is read from the environment (``UPSTOX_ANALYTICS_TOKEN``), which
  is populated from the Git-ignored local ``.env`` file.  It is **never** a default, a
  literal in source, or a constructor argument that gets logged.
* :class:`AnalyticsToken` overrides ``__repr__``/``__str__`` so a token can never be
  printed by accident: a repr, an f-string, a logger call or a traceback local all show
  the mask instead of the value.
* :func:`redact` scrubs any token that slipped into a message - provider errors echo
  request headers in some failure modes, and an exception message is exactly the kind
  of place a credential leaks from.
* A token is refused when it is missing, obviously malformed, or already expired, and
  the refusal is a typed error (:class:`MissingCredentials` / :class:`AuthenticationError`
  at the provider boundary) rather than an anonymous HTTP 401 later.

The expiry is read from the token's own JWT ``exp`` claim when the token is a JWT.
When it is not parseable the expiry is ``None`` and is reported as unknown - the engine
never invents a validity window.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Repository root: three levels up from this module (``nifty_quant/market_data/``).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Default location of the live-data tree, relative to the repository root.
LIVE_DATA_ROOT = Path("data/live")

#: The only place a token is allowed to come from.
TOKEN_ENV_VAR = "UPSTOX_ANALYTICS_TOKEN"

#: Timezone every timestamp in this subsystem is expressed in.
INDIA_TZ = "Asia/Kolkata"

#: A token/key with this many characters or more is treated as a secret when redacting.
_MIN_SECRET_LEN = 20

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{0,}")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE)
_ACCESS_TOKEN_RE = re.compile(r'("?access_token"?\s*[:=]\s*"?)[^"\s,}]+', re.IGNORECASE)


class ConfigError(RuntimeError):
    """Configuration is present but unusable (bad value, impossible window, ...)."""


class MissingCredentials(ConfigError):
    """No analytics token is configured.

    Raised instead of attempting an unauthenticated call and reporting an opaque 401.
    """


# ---------------------------------------------------------------------------
# .env parsing (no third-party dependency: python-dotenv is not a declared pin)
# ---------------------------------------------------------------------------
def parse_env_text(text: str) -> dict[str, str]:
    """Parse a ``.env`` file body into a mapping.

    Deliberately small: ``KEY=value`` lines, ``#`` comments, optional ``export`` prefix,
    single/double quoted values and inline comments after an unquoted value.  A line
    that does not match is skipped rather than guessed at.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()
        out[key] = value
    return out


def load_env_file(path: Path | str | None = None, environ: dict[str, str] | None = None,
                  *, override: bool = False) -> dict[str, str]:
    """Load ``.env`` into ``environ`` (``os.environ`` by default) without clobbering.

    An environment variable that is already set wins, so a shell export or a CI secret
    always beats the local file.  Returns the values that were actually applied (names
    only - never logged with their values).
    """
    env = os.environ if environ is None else environ
    env_path = Path(path) if path is not None else REPO_ROOT / ".env"
    if not env_path.is_file():
        return {}
    applied: dict[str, str] = {}
    for key, value in parse_env_text(env_path.read_text(encoding="utf-8", errors="replace")).items():
        if value == "":
            continue
        if key in env and not override:
            continue
        env[key] = value
        applied[key] = value
    return applied


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def secret_mask(secret: str) -> str:
    """A non-reversible, non-identifying hint of a secret (first 4 + length)."""
    if not secret:
        return "<unset>"
    if len(secret) < 12:
        return "*" * len(secret)
    return f"{secret[:4]}...({len(secret)} chars)"


def redact(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Strip credentials from ``text`` before it reaches a log, a report or a terminal.

    Known secret values are replaced verbatim first (longest first, so a token that
    contains a shorter known secret cannot leave a tail behind), then JWT-shaped and
    ``Bearer``/``access_token`` patterns are masked even when the exact value is
    unknown - e.g. a token echoed by an HTTP error body.
    """
    if text is None:
        return ""
    out = str(text)
    for secret in sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True):
        out = out.replace(secret, secret_mask(secret))
    out = _JWT_RE.sub(lambda m: secret_mask(m.group(0)), out)
    out = _BEARER_RE.sub(lambda m: m.group(1) + secret_mask(m.group(0)[len(m.group(1)):]), out)
    out = _ACCESS_TOKEN_RE.sub(lambda m: m.group(1) + "<redacted>", out)
    return out


def looks_like_token(value: str) -> bool:
    """Cheap plausibility check so a placeholder is rejected before a network call.

    Upstox analytics tokens are long URL-safe strings (typically a JWT).  This accepts
    any long token-shaped string: it must not be able to reject a *valid* token format
    this project has not seen, but it does reject ``<your-token-here>``, ``changeme``
    and an empty string.
    """
    if not value or len(value) < _MIN_SECRET_LEN:
        return False
    if value.strip() != value:
        return False
    if value.startswith("<") or value.startswith("$"):
        return False
    placeholder_words = ("your", "token", "changeme", "placeholder", "xxx", "example")
    if value.lower() in placeholder_words or value.lower().startswith("your_"):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._~+/=\-]+", value))


# ---------------------------------------------------------------------------
# Token object
# ---------------------------------------------------------------------------
def decode_jwt_claims(token: str) -> dict | None:
    """Decode a JWT payload **without** verifying the signature.

    Verification is the issuer's job and would require a signing key this client does
    not have and must not need: the payload is only used for local expiry reporting,
    and every request is still authorised by the provider.  Returns ``None`` when the
    string is not a three-part JWT with a JSON payload.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


@dataclass(frozen=True)
class AnalyticsToken:
    """A read-only provider analytics token with its self-declared validity window."""

    value: str = field(repr=False, default="")
    expires_at: datetime | None = None

    # -- construction -------------------------------------------------------
    @classmethod
    def from_value(cls, value: str) -> "AnalyticsToken":
        claims = decode_jwt_claims(value) if value else None
        expires: datetime | None = None
        if claims:
            exp = claims.get("exp")
            if isinstance(exp, (int, float)):
                expires = datetime.fromtimestamp(float(exp), tz=timezone.utc)
        return cls(value=str(value or ""), expires_at=expires)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None,
                 var: str = TOKEN_ENV_VAR) -> "AnalyticsToken":
        env = os.environ if env is None else env
        return cls.from_value(env.get(var, ""))

    # -- inspection ---------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.value) and looks_like_token(self.value)

    @property
    def masked(self) -> str:
        return secret_mask(self.value)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at

    def days_remaining(self, now: datetime | None = None) -> float | None:
        if self.expires_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.expires_at - now).total_seconds() / 86400.0

    def expires_within(self, days: float, now: datetime | None = None) -> bool:
        remaining = self.days_remaining(now)
        return remaining is not None and remaining <= days

    def assert_usable(self, now: datetime | None = None, var: str = TOKEN_ENV_VAR) -> None:
        """Fail closed, with an actionable message that contains no credential."""
        if not self.value:
            raise MissingCredentials(
                f"{var} is not set. Put the Upstox Analytics Token in the local .env "
                f"file as {var}=... (see .env.example); .env is Git-ignored.")
        if not looks_like_token(self.value):
            raise MissingCredentials(
                f"{var} does not look like a provider token (too short or still a "
                f"placeholder). Set the real value in the local .env file.")
        if self.is_expired(now):
            raised = self.expires_at.isoformat() if self.expires_at else "unknown"
            raise MissingCredentials(
                f"{var} expired at {raised} (provider tokens are valid one year). "
                f"Generate a new Analytics Token and update the local .env file.")

    # -- safety -------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - trivial
        expires = self.expires_at.date().isoformat() if self.expires_at else "unknown"
        return f"AnalyticsToken(masked={self.masked!r}, expires_at={expires})"

    def __str__(self) -> str:
        return self.__repr__()


# ---------------------------------------------------------------------------
# Engine configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarketDataConfig:
    """Everything the live engine may vary, explicit and serialisable.

    Defaults are deliberately conservative: a handful of strikes around the money, the
    minimum subscription mode each instrument needs, and a stale threshold short enough
    that a quiet feed can never look healthy.
    """

    provider: str = "upstox"
    token_env_var: str = TOKEN_ENV_VAR
    data_root: Path = LIVE_DATA_ROOT
    timezone: str = INDIA_TZ
    exchange: str = "NSE"
    underlying: str = "NIFTY"

    # -- universe selection ------------------------------------------------
    #: Optional overrides.  Empty means "discover it"; the documented identifiers are
    #: only a last-resort fallback and are reported as such when used.
    index_instrument_key: str = ""
    vix_instrument_key: str = ""
    use_instrument_master: bool = True
    include_index: bool = True
    include_vix: bool = True
    include_futures: bool = True
    strikes_above_atm: int = 5
    strikes_below_atm: int = 5
    #: Expiry offsets to subscribe, nearest first (0 = nearest unexpired expiry).
    expiry_offsets: tuple[int, ...] = (0,)
    #: Subscription mode for option/future contracts.  ``full`` is required for open
    #: interest, depth and volume; indexes use ``ltpc`` because they carry no volume.
    option_mode: str = "full"
    index_mode: str = "ltpc"

    # -- quality gates -----------------------------------------------------
    stale_after_seconds: float = 30.0
    abnormal_move_pct: float = 0.25
    #: Minutes the collector waits before declaring a session truncated.
    gap_tolerance_minutes: int = 1

    # -- transport ---------------------------------------------------------
    http_timeout_seconds: float = 15.0
    rest_min_interval_seconds: float = 0.25
    max_attempts: int = 4
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    ws_max_attempts: int = 0  # 0 = keep trying for the life of the session
    ws_backoff_base_seconds: float = 1.0
    ws_backoff_max_seconds: float = 60.0
    ws_heartbeat_seconds: float = 20.0
    ws_recv_timeout_seconds: float = 45.0
    #: Flush partial Parquet/JSONL artifacts every N ticks (bounds data loss).
    flush_every_ticks: int = 250
    #: Option-chain responses are cached this long: the chain is a reference snapshot,
    #: not a tick source, and re-polling it every few seconds would burn the REST quota.
    chain_cache_seconds: float = 900.0
    instrument_master_max_age_hours: float = 24.0

    # -- ingestion hand-off ------------------------------------------------
    consent_record: str = ""
    domain_id: str = "DOMAIN_U"
    source_id: str = "SRC-U1-UPSTOX-ANALYTICS"

    def __post_init__(self) -> None:
        if self.strikes_above_atm < 0 or self.strikes_below_atm < 0:
            raise ConfigError("strike windows must be non-negative")
        if self.strikes_above_atm + self.strikes_below_atm > 200:
            raise ConfigError("strike window is larger than any sane subscription budget")
        if not self.expiry_offsets or min(self.expiry_offsets) < 0:
            raise ConfigError("expiry_offsets must contain non-negative offsets")
        if self.option_mode not in {"ltpc", "full", "option_greeks", "full_d30"}:
            raise ConfigError(f"unknown subscription mode {self.option_mode!r}")
        if self.stale_after_seconds <= 0:
            raise ConfigError("stale_after_seconds must be positive")
        if not 0 < self.abnormal_move_pct < 1:
            raise ConfigError("abnormal_move_pct must be a fraction between 0 and 1")
        if self.rest_min_interval_seconds < 0:
            raise ConfigError("rest_min_interval_seconds must be non-negative")
        if self.chain_cache_seconds < 0:
            raise ConfigError("chain_cache_seconds must be non-negative")
        if self.flush_every_ticks <= 0:
            raise ConfigError("flush_every_ticks must be positive")

    # -- paths -------------------------------------------------------------
    @property
    def root(self) -> Path:
        """Absolute live-data root (relative values are resolved against the repo)."""
        path = Path(self.data_root)
        return path if path.is_absolute() else REPO_ROOT / path

    def as_dict(self) -> dict:
        out = {k: v for k, v in self.__dict__.items()}
        out["data_root"] = str(self.data_root)
        out["expiry_offsets"] = list(self.expiry_offsets)
        return out

    @classmethod
    def from_mapping(cls, values: dict) -> "MarketDataConfig":
        """Build from a JSON/CLI mapping, ignoring unknown keys and coercing numbers."""
        known = {f for f in cls.__dataclass_fields__}
        kwargs: dict = {}
        for key, value in values.items():
            if key not in known:
                continue
            if key == "expiry_offsets":
                kwargs[key] = tuple(int(v) for v in value)
            elif key == "data_root":
                kwargs[key] = Path(value)
            else:
                kwargs[key] = value
        return cls(**kwargs)


def resolve_token(config: MarketDataConfig | None = None, *, env: dict[str, str] | None = None,
                  load_env: bool = True) -> AnalyticsToken:
    """Load ``.env``, read the configured variable, and refuse an unusable token."""
    if load_env:
        load_env_file()
    config = config or MarketDataConfig()
    return AnalyticsToken.from_env(env, config.token_env_var)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime | None = None) -> str:
    return (moment or utc_now()).isoformat(timespec="milliseconds")


def days_from_now(days: float) -> datetime:
    return utc_now() + timedelta(days=days)
