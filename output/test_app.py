import asyncio
import time
from unittest import mock

import pytest
import requests
import structlog
from pydantic import ValidationError

# The module under test – adjust the import path if the file name differs.
import rate_limited_client as rlc


# --------------------------------------------------------------------------- #
# Configuration model validation
# --------------------------------------------------------------------------- #


def test_rate_limit_validation_success():
    rl = rlc.RateLimit(max_requests=10, period_seconds=1.5)
    assert rl.max_requests == 10
    assert rl.period_seconds == 1.5


def test_rate_limit_invalid_period():
    with pytest.raises(ValidationError) as exc:
        rlc.RateLimit(max_requests=5, period_seconds=0)
    assert "period_seconds must be > 0" in str(exc.value)


def test_rate_limit_negative_max_requests():
    with pytest.raises(ValidationError):
        rlc.RateLimit(max_requests=-1, period_seconds=1.0)


def test_cache_settings_default_ttl():
    cs = rlc.CacheSettings()
    assert cs.ttl_seconds == 300.0
    assert cs.backend == "memory"


def test_cache_settings_invalid_ttl():
    with pytest.raises(ValidationError):
        rlc.CacheSettings(ttl_seconds=-10)


def test_retry_settings_invalid_max_attempts():
    with pytest.raises(ValidationError):
        rlc.RetrySettings(max_attempts=0)


# --------------------------------------------------------------------------- #
# Logger configuration
# --------------------------------------------------------------------------- #


def test_configure_logger_returns_bound_logger():
    settings = rlc.LoggingSettings(level="DEBUG", json=False, logger_name="test_logger")
    logger = rlc.configure_logger(settings)
    assert isinstance(logger, structlog.BoundLogger)
    # The logger should have the name we asked for
    assert logger._logger.name == "test_logger"


# --------------------------------------------------------------------------- #
# In‑memory cache behaviour
# --------------------------------------------------------------------------- #


def test_in_memory_cache_set_and_get():
    cache = rlc.InMemoryCache(max_size=2)
    cache.set("k1", "v1", ttl=5)
    assert cache.get("k1") == ("v1", mock.ANY)  # expiry timestamp is dynamic

    # Expiration
    with mock.patch("time.time", return_value=time.time() + 10):
        assert cache.get("k1") is None


def test_in_memory_cache_lru_eviction():
    cache = rlc.InMemoryCache(max_size=2)
    cache.set("a", 1, ttl=30)
    cache.set("b", 2, ttl=30)
    # Access "a" to make it most‑recently used
    _ = cache.get("a")
    # Insert third entry, should evict "b"
    cache.set("c", 3, ttl=30)
    assert cache.get("b") is None
    assert cache.get("a")[0] == 1
    assert cache.get("c")[0] == 3


def test_in_memory_cache_delete_and_clear():
    cache = rlc.InMemoryCache()
    cache.set("x", "y", ttl=30)
    cache.delete("x")
    assert cache.get("x") is None

    cache.set("p", 1, ttl=30)
    cache.set("q", 2, ttl=30)
    cache.clear()
    assert cache.get("p") is None
    assert cache.get("q") is None


# --------------------------------------------------------------------------- #
# Token bucket – sync
# --------------------------------------------------------------------------- #


def test_token_bucket_consumes_immediately_when_tokens_available():
    bucket = rlc.TokenBucket(max_tokens=5, refill_interval=0.1)
    # No sleep should happen because we have tokens
    with mock.patch("time.sleep") as m_sleep:
        bucket.consume(3)
        m_sleep.assert_not_called()
    # Tokens left should be 2
    assert bucket._tokens == 2


def test_token_bucket_blocks_until_refill():
    bucket = rlc.TokenBucket(max_tokens=1, refill_interval=0.05)
    # Consume the only token
    bucket.consume(1)

    # Next consume should block for ~0.05 s
    start = time.monotonic()
    with mock.patch("time.sleep") as m_sleep:
        bucket.consume(1)
        # The wrapper uses time.sleep internally; we let it run normally
        m_sleep.assert_not_called()  # sleep is called directly, not via mock
    elapsed = time.monotonic() - start
    assert elapsed >= 0.045  # allow small tolerance


# --------------------------------------------------------------------------- #
# Rate limiter – sync
# --------------------------------------------------------------------------- #


def test_rate_limiter_calls_logger_debug():
    logger = mock.Mock()
    limit = rlc.RateLimit(max_requests=2, period_seconds=0.1)
    rl = rlc.RateLimiter(limit, logger)
    rl.acquire()
    logger.debug.assert_called_once_with("rate_limit_wait", tokens=1)


# --------------------------------------------------------------------------- #
# Async rate limiter
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_async_rate_limiter_acquire_respects_refill():
    logger = mock.AsyncMock()
    limit = rlc.RateLimit(max_requests=1, period_seconds=0.05)
    arl = rlc.AsyncRateLimiter(limit, logger)

    # First acquire should be immediate
    await arl.acquire()
    logger.debug.assert_not_called()

    # Second acquire must wait for refill (~0.05 s)
    start = asyncio.get_event_loop().time()
    await arl.acquire()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.045  # tolerance
    logger.debug.assert_called()  # called when sleeping


# --------------------------------------------------------------------------- #
# Retry handler – sync
# --------------------------------------------------------------------------- #


def make_flaky_function(exc_sequence, result):
    """Return a callable that raises the given exceptions in order then returns result."""
    calls = {"i": 0}

    def fn(*_args, **_kwargs):
        i = calls["i"]
        calls["i"] += 1
        if i < len(exc_sequence):
            raise exc_sequence[i]
        return result

    return fn


def test_retry_handler_retries_on_retryable_exceptions(monkeypatch):
    logger = mock.Mock()
    settings = rlc.RetrySettings(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=0.0)

    # First two calls raise ConnectionError (retryable), third succeeds
    flaky = make_flaky_function(
        [requests.exceptions.ConnectionError("boom"), requests.exceptions.Timeout("t")],
        result="ok",
    )
    handler = rlc.RetryHandler(settings, logger)
    wrapped = handler(flaky)

    # Patch sleep to avoid real waiting
    with mock.patch("time.sleep") as m_sleep:
        assert wrapped() == "ok"
        # Two retries → two warnings logged, one final success
        assert logger.warning.call_count == 2
        # No error logged because we succeeded before exhausting attempts
        logger.error.assert_not_called()
        # Ensure sleep was called with the calculated delays
        assert m_sleep.call_count == 2
        # Delays should respect base_delay * 2**(attempt-1)
        expected_delays = [0.01, 0.02]  # jitter=0 makes them deterministic
        actual_delays = [c[0][0] for c in m_sleep.call_args_list]
        assert actual_delays == expected_delays


def test_retry_handler_non_retryable_exception_propagates_immediately(monkeypatch):
    logger = mock.Mock()
    settings = rlc.RetrySettings(max_attempts=5, base_delay=0.01, jitter=0.0)

    # Raise a generic ValueError (non‑retryable)
    fn = make_flaky_function([ValueError("bad")], result=None)
    handler = rlc.RetryHandler(settings, logger)
    wrapped = handler(fn)

    with mock.patch("time.sleep") as m_sleep:
        with pytest.raises(ValueError):
            wrapped()
        # No retries → no warning, no sleep
        logger.warning.assert_not_called()
        m_sleep.assert_not_called()
        # Error should be logged as exhausted (even though we never retried)
        logger.error.assert_called_once()


def test_retry_handler_exhausts_after_max_attempts(monkeypatch):
    logger = mock.Mock()
    settings = rlc.RetrySettings(max_attempts=2, base_delay=0.01, jitter=0.0)

    # Always raise a retryable ConnectionError
    fn = make_flaky_function([requests.exceptions.ConnectionError()] * 5, result=None)
    handler = rlc.RetryHandler(settings, logger)
    wrapped = handler(fn)

    with mock.patch("time.sleep") as m_sleep:
        with pytest.raises(requests.exceptions.ConnectionError):
            wrapped()
        # Two attempts → one retry (max_attempts includes the first try)
        assert logger.warning.call_count == 1
        logger.error.assert_called_once()
        assert m_sleep.call_count == 1


# --------------------------------------------------------------------------- #
# Async retry handler
# --------------------------------------------------------------------------- #


def make_async_flaky_function(exc_sequence, result):
    """Async version of make_flaky_function."""
    calls = {"i": 0}

    async def fn(*_args, **_kwargs):
        i = calls["i"]
        calls["i"] += 1
        if i < len(exc_sequence):
            raise exc_sequence[i]
        return result

    return fn


@pytest.mark.asyncio
async def test_async_retry_handler_retries_on_retryable_exceptions(monkeypatch):
    logger = mock.AsyncMock()
    settings = rlc.RetrySettings(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=0.0)

    async_fn = make_async_flaky_function(
        [requests.exceptions.ConnectionError(), requests.exceptions.Timeout()], result="ok"
    )
    handler = rlc.AsyncRetryHandler(settings, logger)
    wrapped = handler(async_fn)

    with mock.patch("asyncio.sleep", new=mock.AsyncMock()) as m_sleep:
        result = await wrapped()
        assert result == "ok"
        # Two retries → two warnings
        assert logger.warning.call_count == 2
        logger.error.assert_not_called()
        # asyncio.sleep called twice with expected delays
        expected = [0.01, 0.02]
        actual = [c.args[0] for c in m_sleep.call_args_list]
        assert actual == expected


@pytest.mark.asyncio
async def test_async_retry_handler_non_retryable_exception(monkeypatch):
    logger = mock.AsyncMock()
    settings = rlc.RetrySettings(max_attempts=5, base_delay=0.01, jitter=0.0)

    async_fn = make_async_flaky_function([ValueError("bad")], result=None)
    handler = rlc.AsyncRetryHandler(settings, logger)
    wrapped = handler(async_fn)

    with mock.patch("asyncio.sleep", new=mock.AsyncMock()) as m_sleep:
        with pytest.raises(ValueError):
            await wrapped()
        logger.warning.assert_not_called()
        m_sleep.assert_not_called()
        logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_async_retry_handler_exhausts_after_max_attempts(monkeypatch):
    logger = mock.AsyncMock()
    settings = rlc.RetrySettings(max_attempts=2, base_delay=0.01, jitter=0.0)

    async_fn = make_async_flaky_function(
        [requests.exceptions.ConnectionError()] * 5, result=None
    )
    handler = rlc.AsyncRetryHandler(settings, logger)
    wrapped