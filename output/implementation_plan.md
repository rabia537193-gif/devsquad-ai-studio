## 1. Overview  

| Goal | What it means for the client |
|------|------------------------------|
| **Rate‑limited** | Guarantees we never exceed a configurable number of requests per time‑window (e.g., 100 req/60 s). Implemented with a token‑bucket algorithm that works for both sync and async callers. |
| **Exponential‑backoff retry** | On transient failures (HTTP 5xx, network errors, 429 Too‑Many‑Requests) the client automatically retries up to *N* attempts, waiting `base * 2**attempt + jitter`. |
| **Response caching** | Transparent, per‑endpoint cache with configurable TTL, optional `Cache‑Control` respect, and a pluggable backend (in‑memory, file, Redis, etc.). |
| **Structured logging** | All events (request start, response, retries, cache‑hits/misses, rate‑limit throttles) are emitted as JSON‑compatible dicts (or via `structlog`) so they can be ingested by log aggregators. |
| **Pythonic API** | A thin, ergonomic wrapper around `requests` (sync) and `httpx` (async) that feels like a normal `Session`/`Client`. |

The implementation will be delivered as a **pip‑installable library** (`rate_limited_client`) with the following public entry‑points:

```python
from rate_limited_client import RestClient, AsyncRestClient

client = RestClient(base_url="https://api.example.com", rate_limit=RateLimit(100, 60))
response = client.get("/resource", params={"q": "test"})
```

---

## 2. High‑level Architecture  

```
+-------------------+          +-------------------+          +-------------------+
|   User Code       |  uses    |   RestClient      |  uses    |   Transport Layer |
| (sync/async)      |--------->| (public façade)   |--------->| (requests/httpx) |
+-------------------+          +-------------------+          +-------------------+
          |                               |                               |
          |                               |                               |
          v                               v                               v
+-------------------+          +-------------------+          +-------------------+
| RateLimiter       |<-------->| RetryHandler      |<-------->| CacheBackend      |
| (token bucket)   |          | (exponential)    |          | (memory/redis…)   |
+-------------------+          +-------------------+          +-------------------+
          ^                               ^                               ^
          |                               |                               |
          +-------------------+-----------+-----------+-------------------+
                              |                       |
                              v                       v
                     +-------------------+   +-------------------+
                     | StructuredLogger  |   | Config/Settings   |
                     +-------------------+   +-------------------+
```

* **RestClient / AsyncRestClient** – public façade that composes the lower‑level components.  
* **RateLimiter** – token‑bucket implementation that blocks (or sleeps) until a token is available.  
* **RetryHandler** – decorates the transport call, catches retryable errors, and re‑invokes after a back‑off delay.  
* **CacheBackend** – abstract base class; concrete implementations (`InMemoryCache`, `FileCache`, `RedisCache`).  
* **StructuredLogger** – thin wrapper around `structlog` (or the std‑lib `logging` with a JSON formatter).  
* **Config/Settings** – `pydantic`‑based immutable configuration object that can be built from a dict, env‑vars, or a `.yaml` file.

All components are **thread‑safe** (or async‑safe) and **pluggable** – the user can replace any piece with a custom implementation that respects the defined protocol.

---

## 3. Core Data Structures  

### 3.1 Configuration objects (pydantic)

```python
from pydantic import BaseModel, Field, PositiveInt, PositiveFloat, validator
from typing import Optional, Literal, Union, Mapping

class RateLimit(BaseModel):
    max_requests: PositiveInt          # e.g. 100
    period_seconds: PositiveFloat      # e.g. 60.0

class CacheSettings(BaseModel):
    ttl_seconds: PositiveFloat = 300.0
    backend: Literal["memory", "file", "redis"] = "memory"
    # backend‑specific options
    options: Mapping[str, Union[str, int, float]] = {}

class RetrySettings(BaseModel):
    max_attempts: PositiveInt = 5
    base_delay: PositiveFloat = 0.5   # seconds
    max_delay: PositiveFloat = 30.0
    jitter: PositiveFloat = 0.1       # random fraction of delay

class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json: bool = True
    logger_name: str = "rate_limited_client"

class ClientSettings(BaseModel):
    base_url: str
    rate_limit: RateLimit
    cache: CacheSettings = CacheSettings()
    retry: RetrySettings = RetrySettings()
    logging: LoggingSettings = LoggingSettings()
    timeout: PositiveFloat = 10.0
    # optional auth hook
    auth: Optional[Mapping[str, str]] = None
```

### 3.2 Cache key

A deterministic, hashable key derived from request method, URL (including query string), and a **canonicalized** representation of the request body (if any).  

```python
from hashlib import sha256
from urllib.parse import urljoin, urlencode

def make_cache_key(method: str, url: str, params: Mapping = None, json_body: Mapping = None) -> str:
    parts = [method.upper(), url]
    if params:
        parts.append(urlencode(sorted(params.items()), doseq=True))
    if json_body:
        # deterministic JSON string
        import json
        parts.append(json.dumps(json_body, sort_keys=True, separators=(',', ':')))
    raw = "|".join(parts)
    return sha256(raw.encode("utf-8")).hexdigest()
```

### 3.3 Token bucket state (sync)

```python
from threading import Lock
import time

class TokenBucket:
    def __init__(self, max_tokens: int, refill_interval: float):
        self.max_tokens = max_tokens
        self.refill_interval = refill_interval  # seconds per token
        self._tokens = max_tokens
        self._last_refill = time.monotonic()
        self._lock = Lock()

    def consume(self, tokens: int = 1) -> None:
        with self._lock:
            self._refill()
            while self._tokens < tokens:
                # sleep until next token arrives
                sleep_time = (tokens - self._tokens) * self.refill_interval
                time.sleep(sleep_time)
                self._refill()
            self._tokens -= tokens

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        added = int(elapsed / self.refill_interval)
        if added > 0:
            self._tokens = min(self.max_tokens, self._tokens + added)
            self._last_refill = now
```

*Async version* uses `asyncio.Lock` and `await asyncio.sleep`.

---

## 4. Component Design  

### 4.1 StructuredLogger  

```python
import structlog
import logging
import sys

def configure_logger(settings: LoggingSettings) -> structlog.BoundLogger:
    logging.basicConfig(
        level=settings.level,
        format="%(message)s",
        stream=sys.stdout,
    )
    processors = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer() if settings.json else structlog.processors.KeyValueRenderer(),
    ]
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger(settings.logger_name)
```

All internal modules receive a `logger: structlog.BoundLogger` instance and log with:

```python
logger.info("request_start", method=method, url=url, attempt=attempt)
logger.debug("cache_hit", key=cache_key)
logger.warning("retry", attempt=attempt, delay=delay, reason=str(exc))
```

### 4.2 CacheBackend Interface  

```python
from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple
import datetime

class CacheBackend(ABC):
    @abstractmethod
    def get(self, key: str) -> Optional[Tuple[Any, datetime.datetime]]:
        """Return (value, expiry) or None if missing/expired."""

    @abstractmethod
    def set(self, key: str, value: Any, ttl: float) -> None:
        """Store value with TTL (seconds)."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove entry."""

    @abstractmethod
    def clear(self) -> None:
        """Flush entire cache."""
```

#### 4.2.1 In‑Memory implementation  

```python
from collections import OrderedDict
import threading
import time

class InMemoryCache(CacheBackend):
    def __init__(self, max_size: int = 10_000):
        self._store: dict[str, Tuple[Any, float]] = {}
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if expiry < time.time():
                del self._store[key]
                return None
            return value, expiry

    def set(self, key: str, value: Any, ttl: float):
        with self._lock:
            if len(self._store) >= self._max_size:
                # simple LRU eviction
                oldest_key = min(self._store.items(), key=lambda kv: kv[1][1])[0]
                del self._store[oldest_key]
            expiry = time.time() + ttl
            self._store[key] = (value, expiry)

    # delete & clear omitted for brevity
```

*File* and *Redis* backends follow the same contract, using `pickle` or `json` for serialization.

### 4.3 RetryHandler  

```python
import random
import time
from typing import Callable, TypeVar, Any

T = TypeVar("T")

class RetryHandler:
    def __init__(self, settings: RetrySettings, logger: structlog.BoundLogger):
        self.settings = settings
        self.logger = logger

    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args, **kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    attempt += 1
                    if not self._is_retryable(exc) or attempt > self.settings.max_attempts:
                        self.logger.error("retry_exhausted", attempt=attempt, error=str(exc))
                        raise
                    delay = self._calc_delay(attempt)
                    self.logger.warning(
                        "retry",
                        attempt=attempt,
                        delay=delay,
                        error=str(exc),
                        exc_info=exc,
                    )
                    time.sleep(delay)
        return wrapper

    def _is_retryable(self, exc: Exception) -> bool:
        # network errors, 5xx, 429
        if isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout)):
            return True
        if isinstance(exc, requests.exceptions.HTTPError):
            return 500 <= exc.response.status_code < 600 or exc.response.status_code == 429
        return False

    def _calc_delay(self, attempt: int) -> float:
        base = self.settings.base_delay * (2 ** (attempt - 1))
        jitter = random.uniform(0, self.settings.jitter * base)
        return min(self.settings.max_delay, base + jitter)
```

*Async* version uses `await asyncio.sleep`.

### 4.4 RateLimiter (sync & async)

```python
class RateLimiter:
    def __init__(self, limit: RateLimit, logger: structlog.BoundLogger):
        self.bucket = TokenBucket(limit.max_requests, limit.period_seconds / limit.max_requests)
        self.logger = logger

    def acquire(self):
        self.logger.debug("rate_limit_wait", tokens=1)
        self.bucket.consume(1)
```

Async variant:

```python
class AsyncRateLimiter:
    def __init__(self, limit: RateLimit, logger: structlog.BoundLogger):
        self.max_tokens = limit.max_requests
        self.refill_interval = limit.period_seconds / limit.max_requests
        self._tokens = limit.max_requests
        self._last = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()
        self.logger = logger

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last
            added = int(elapsed / self.refill_interval)
            if added:
                self._tokens = min(self.max_tokens, self._tokens + added)
                self._last = now
            while self._tokens < 1:
                sleep = (1 - self._tokens) * self.refill_interval
                self.logger.debug("rate_limit_sleep", sleep=sleep)
                await asyncio.sleep(sleep)
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last
                added = int(elapsed / self.refill