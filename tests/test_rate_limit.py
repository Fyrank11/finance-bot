import asyncio

import pytest

from app.rate_limit import RateLimiter


def test_default_limits_and_per_user_isolation():
    limiter = RateLimiter()
    assert all(limiter.allow(1, now=0) for _ in range(30))
    assert not limiter.allow(1, now=0)
    assert limiter.allow(2, now=0)


def test_expensive_requests_share_total_quota_but_have_own_limit():
    limiter = RateLimiter()
    assert all(limiter.allow(1, expensive=True, now=0) for _ in range(3))
    assert not limiter.allow(1, expensive=True, now=0)
    assert limiter.allow(2, expensive=True, now=0)
    assert all(limiter.allow(1, now=0) for _ in range(27))
    assert not limiter.allow(1, now=0)


def test_ordinary_limit_also_rejects_expensive_requests():
    limiter = RateLimiter(limit=2)
    assert limiter.allow(1, now=0)
    assert limiter.allow(1, now=0)
    assert not limiter.allow(1, expensive=True, now=0)


def test_rolling_expiration_not_fixed_minute_reset():
    limiter = RateLimiter(limit=2)
    assert limiter.allow(1, now=0)
    assert limiter.allow(1, now=30)
    assert not limiter.allow(1, now=59.999)
    assert limiter.allow(1, now=60)
    assert not limiter.allow(1, now=60)
    assert not limiter.allow(1, now=89.999)
    assert limiter.allow(1, now=90)


def test_denials_do_not_consume_quota_or_extend_request_expiry():
    limiter = RateLimiter(limit=3, expensive_limit=1)
    assert limiter.allow(1, expensive=True, now=0)
    for second in range(1, 60):
        assert not limiter.allow(1, expensive=True, now=second)
    assert limiter.allow(1, now=59)
    assert limiter.allow(1, now=59)
    assert not limiter.allow(1, now=59)
    assert limiter.allow(1, expensive=True, now=60)
    assert not limiter.allow(1, now=60)


def test_expensive_limit_expires_independently_with_active_ordinary_traffic():
    limiter = RateLimiter(expensive_limit=1)
    assert limiter.allow(1, expensive=True, now=10)
    assert limiter.allow(1, now=40)
    assert not limiter.allow(1, expensive=True, now=69.999)
    assert limiter.allow(1, expensive=True, now=70)


def test_many_async_events_in_one_loop_cannot_exceed_quota():
    limiter = RateLimiter()

    async def request():
        return limiter.allow(1, expensive=True, now=0)

    async def burst():
        return await asyncio.gather(*(request() for _ in range(100)))

    results = asyncio.run(burst())
    assert results == [True] * 3 + [False] * 97
    assert limiter.tracked_users == 1


def test_cache_is_bounded_and_evicts_least_recently_used_identity():
    limiter = RateLimiter(limit=1, max_users=2)
    assert limiter.allow(1, now=0)
    assert limiter.allow(2, now=1)
    assert not limiter.allow(1, now=2)  # Touch 1, so 2 is now least recent.
    assert limiter.allow(3, now=3)
    assert limiter.tracked_users == 2
    assert not limiter.allow(1, now=4)
    assert limiter.allow(2, now=5)  # Eviction resets a user's limits by design.
    for user_id in range(4, 1_000):
        assert limiter.allow(user_id, now=5)
        assert limiter.tracked_users == 2


def test_idle_cache_entries_are_removed_at_window_boundary():
    limiter = RateLimiter()
    assert limiter.allow(1, now=0)
    assert limiter.allow(2, now=1)
    assert limiter.allow(3, now=60)
    assert limiter.tracked_users == 2
    assert limiter.allow(3, now=61)
    assert limiter.tracked_users == 1


def test_default_clock_is_monotonic(monkeypatch):
    limiter = RateLimiter(limit=1)
    monkeypatch.setattr("app.rate_limit.monotonic", lambda: 100.0)
    assert limiter.allow(1)
    assert not limiter.allow(1)
    monkeypatch.setattr("app.rate_limit.monotonic", lambda: 160.0)
    assert limiter.allow(1)


def test_denial_notifications_are_throttled_without_affecting_request_quota():
    limiter = RateLimiter(limit=1)
    assert limiter.allow(1, now=0)
    assert not limiter.allow(1, now=0)
    assert limiter.should_notify(1, now=0)
    for second in range(1, 10):
        assert not limiter.allow(1, now=second)
        assert not limiter.should_notify(1, now=second)
    assert not limiter.allow(1, now=10)
    assert limiter.should_notify(1, now=10)
    assert not limiter.should_notify(1, now=10)
    assert limiter.allow(1, now=60)


def test_notification_limits_are_per_user_and_cannot_grow_cache():
    limiter = RateLimiter(limit=1, max_users=2)
    for user_id in (1, 2):
        assert limiter.allow(user_id, now=0)
        assert not limiter.allow(user_id, now=0)
        assert limiter.should_notify(user_id, now=0)
    for user_id in range(3, 1_000):
        assert not limiter.should_notify(user_id, now=0)
    assert limiter.tracked_users == 2
    assert limiter.allow(3, now=0)
    assert not limiter.should_notify(1, now=0)  # Evicted with all of user 1's state.


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"expensive_limit": 0},
        {"max_users": 0},
        {"window_seconds": 0},
        {"window_seconds": float("inf")},
        {"window_seconds": float("nan")},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        RateLimiter(**kwargs)
