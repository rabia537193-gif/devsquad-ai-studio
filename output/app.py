"""
rate_limited_client
~~~~~~~~~~~~~~~~~~~

A tiny, production‑ready HTTP client that provides:

* Token‑bucket rate limiting (sync & async)
* Exponential‑backoff retry for transient failures
* Transparent per‑endpoint response caching (in‑memory backend)
* Structured JSON logging via ``structlog``
* Pydantic‑based immutable configuration

Only the public symbols ``RestClient`` and ``AsyncRestClient`` are intended
for external use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from hashlib import sha256
from typing import Any, Callable, Mapping, Optional, Tuple, TypeVar, Union

import requests
import httpx
import structlog
from pydantic import BaseModel, Field, PositiveFloat, PositiveInt, validator

# --------------------------------------------------------------------------- #
# Configuration models
# --------------------------------------------------------------------------- #


class RateLimit(BaseModel):
    """Rate‑limit configuration."""

    max_requests: PositiveInt = Field(..., description="Maximum requests per period")
    period_seconds: PositiveFloat = Field(..., description="Period length in seconds")

    @validator("period_seconds")
    def _period_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("period_seconds must be > 0")
        return v


class CacheSettings(BaseModel):
    """Cache configuration."""

    ttl_seconds: PositiveFloat = Field(300.0, description="Default TTL for cached responses")
    backend: str = Field("memory", description="Backend identifier (currently only 'memory')")
    options: Mapping[str, Union[str, int, float]] = Field(
        default_factory=dict, description="Backend‑specific options"
    )


class RetrySettings(BaseModel):
    """Retry configuration."""

    max_attempts: PositiveInt = Field(5, description="Maximum number of attempts (incl. first)")
    base_delay: PositiveFloat = Field(0.5, description="Base delay in seconds")
    max_delay: PositiveFloat = Field(30.0, description="Maximum back‑off delay")
    jitter: PositiveFloat = Field(0.1, description="Fraction of delay used as random jitter")


class LoggingSettings(BaseModel):
    """Logging configuration."""

    level: str = Field("INFO", description="Logging level")
    json: bool = Field(True, description="Render logs as JSON")
    logger_name: str = Field("rate_limited_client", description="Base logger name")


class ClientSettings(BaseModel):
    """Top‑level client configuration."""

    base_url: str = Field(..., description="Base URL for all requests")
    rate_limit: RateLimit
    cache: CacheSettings = Field(default_factory=CacheSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    timeout: PositiveFloat = Field(10.0, description="Request timeout in seconds")
    auth: Optional[Mapping[str, str]] = Field(
        None, description="Optional static auth mapping (e.g. {'Authorization': 'Bearer …'} )"
    )


# --------------------------------------------------------------------------- #
# Structured logger
# --------------------------------------------------------------------------- #


def configure_logger(settings: LoggingSettings) -> structlog.BoundLogger:
    """
    Initialise ``structlog`` with a JSON or key‑value renderer.

    Returns a bound logger ready for use throughout the library.
    """
    logging.basicConfig(
        level=getattr(logging, settings.level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[logging.StreamHandler()],
    )
    processors = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.processors.KeyValueRenderer())
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger(settings.logger_name)


# --------------------------------------------------------------------------- #
# Cache backend abstraction
# --------------------------------------------------------------------------- #


class CacheBackend(ABC):
    """Abstract cache backend."""

    @abstractmethod
    def get(self, key: str) -> Optional[Tuple[Any, float]]:
        """
        Retrieve a cached entry.

        Returns ``(value, expiry_timestamp)`` or ``None`` if missing/expired.
        """

    @abstractmethod
    def set(self, key: str, value: Any, ttl: float) -> None:
        """Store ``value`` under ``key`` for ``ttl`` seconds."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove a cached entry."""

    @abstractmethod
    def clear(self) -> None:
        """Flush the entire cache."""


class InMemoryCache(CacheBackend):
    """Thread‑safe in‑memory LRU cache."""

    def __init__(self, max_size: int = 10_000):
        self._store: dict[str, Tuple[Any, float]] = {}
        self._max_size = max_size
        self._lock = threading.Lock()
        self._order = OrderedDict()  # key → None (used only for ordering)

    def get(self, key: str) -> Optional[Tuple[Any, float]]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if expiry < time.time():
                # expired – purge
                self._purge(key)
                return None
            # refresh LRU order
            self._order.move_to_end(key)
            return value, expiry

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            if len(self._store) >= self._max_size:
                # evict the oldest entry
                oldest_key, _ = self._order.popitem(last=False)
                self._store.pop(oldest_key, None)
            expiry = time.time() + ttl
            self._store[key] = (value, expiry)
            self._order[key] = None

    def delete(self, key: str) -> None:
        with self._lock:
            self._purge(key)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._order.clear()

    def _purge(self, key: str) -> None:
        self._store.pop(key, None)
        self._order.pop(key, None)


# --------------------------------------------------------------------------- #
# Token bucket implementation (sync)
# --------------------------------------------------------------------------- #


class TokenBucket:
    """
    Simple thread‑safe token bucket.

    ``refill_interval`` is the time (seconds) required to add a single token.
    """

    def __init__(self, max_tokens: int, refill_interval: float):
        self.max_tokens = max_tokens
        self.refill_interval = refill_interval
        self._tokens = max_tokens
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: int = 1) -> None:
        """Block until ``tokens`` are available, then consume them."""
        with self._lock:
            self._refill()
            while self._tokens < tokens:
                needed = tokens - self._tokens
                sleep_time = needed * self.refill_interval
                time.sleep(sleep_time)
                self._refill()
            self._tokens -= tokens

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        added = int(elapsed / self.refill_interval)
        if added:
            self._tokens = min(self.max_tokens, self._tokens + added)
            self._last_refill = now


# --------------------------------------------------------------------------- #
# Rate limiter (sync & async)
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Sync rate limiter based on :class:`TokenBucket`."""

    def __init__(self, limit: RateLimit, logger: structlog.BoundLogger):
        refill_interval = limit.period_seconds / limit.max_requests
        self._bucket = TokenBucket(limit.max_requests, refill_interval)
        self._logger = logger

    def acquire(self) -> None:
        """Block until a token is available."""
        self._logger.debug("rate_limit_wait", tokens=1)
        self._bucket.consume(1)


class AsyncRateLimiter:
    """Async counterpart of :class:`RateLimiter`."""

    def __init__(self, limit: RateLimit, logger: structlog.BoundLogger):
        self._max_tokens = limit.max_requests
        self._refill_interval = limit.period_seconds / limit.max_requests
        self._tokens = limit.max_requests
        self._last = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()
        self._logger = logger

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last
            added = int(elapsed / self._refill_interval)
            if added:
                self._tokens = min(self._max_tokens, self._tokens + added)
                self._last = now
            while self._tokens < 1:
                sleep = (1 - self._tokens) * self._refill_interval
                self._logger.debug("rate_limit_sleep", sleep=sleep)
                await asyncio.sleep(sleep)
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last
                added = int(elapsed / self._refill_interval)
                if added:
                    self._tokens = min(self._max_tokens, self._tokens + added)
                    self._last = now
            self._tokens -= 1


# --------------------------------------------------------------------------- #
# Retry handler (sync & async)
# --------------------------------------------------------------------------- #

T = TypeVar("T")


class RetryHandler:
    """Callable decorator that adds exponential‑backoff retry."""

    def __init__(self, settings: RetrySettings, logger: structlog.BoundLogger):
        self._settings = settings
        self._logger = logger

    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as exc:  # pragma: no cover – exercised via tests
                    attempt += 1
                    if not self._is_retryable(exc) or attempt > self._settings.max_attempts:
                        self._logger.error(
                            "retry_exhausted",
                            attempt=attempt,
                            error=str(exc),
                            exc_info=exc,
                        )
                        raise
                    delay = self._calc_delay(attempt)
                    self._logger.warning(
                        "retry",
                        attempt=attempt,
                        delay=delay,
                        error=str(exc),
                        exc_info=exc,
                    )
                    time.sleep(delay)

        return wrapper

    # ------------------------------------------------------------------- #
    # Helper methods
    # ------------------------------------------------------------------- #

    def _is_retryable(self, exc: Exception) -> bool:
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        if isinstance(exc, requests.exceptions.HTTPError):
            code = exc.response.status_code
            return 500 <= code < 600 or code == 429
        return False

    def _calc_delay(self, attempt: int) -> float:
        base = self._settings.base_delay * (2 ** (attempt - 1))
        jitter = random.uniform(0, self._settings.jitter * base)
        return min(self._settings.max_delay, base + jitter)


class AsyncRetryHandler:
    """Async version of :class:`RetryHandler`."""

    def __init__(self, settings: RetrySettings, logger: structlog.BoundLogger):
        self._settings = settings
        self._logger = logger

    def __call__(self, func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    attempt += 1
                    if not self._is_retryable(exc) or attempt > self._settings.max_attempts:
                        self._logger.error(
                            "retry_exhausted",
                            attempt=attempt,
                            error=str(exc),
                            exc_info=exc,
                        )
                        raise
                    delay = self._calc_delay(attempt)
                    self._logger.warning(
                        "retry",
                        attempt=attempt,
                        delay=delay,
                        error=str(exc),
                        exc_info=exc,
                    )
                    await asyncio.sleep(delay)

        return wrapper

    # ------------------------------------------------------------------- #
    # Helper methods (mirrored from sync version)
    # ------------------------------------------------------------------- #

    def _is_retryable(self, exc: Exception) -> bool: