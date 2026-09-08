"""Small per-user request limits for the single-process Telegram worker.

Each accepted event counts toward the ordinary limit; costly requests also count
toward a smaller limit. Rejected requests consume neither quota. ``allow`` is
synchronous, with no suspension points, so calls from one asyncio event loop are
atomic. It is not intended for concurrent threads or multiple worker processes.

Idle entries expire after one window. LRU eviction caps tracked identities even
when many new accounts arrive. Eviction and process restarts reset an identity's
counters: these limits reduce accidental/spam bursts, not distributed abuse, and
must never be used as an authorization boundary.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from math import isfinite
from time import monotonic


@dataclass
class _Window:
    last_seen: float
    events: deque[float] = field(default_factory=deque)
    expensive_events: deque[float] = field(default_factory=deque)
    last_notified: float | None = None


class RateLimiter:
    def __init__(
        self,
        *,
        limit: int = 30,
        expensive_limit: int = 3,
        window_seconds: float = 60.0,
        max_users: int = 10_000,
    ) -> None:
        if limit < 1 or expensive_limit < 1 or max_users < 1:
            raise ValueError("Request limits and max_users must be positive")
        if not isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        self.limit = limit
        self.expensive_limit = expensive_limit
        self.window_seconds = window_seconds
        self.max_users = max_users
        self._users: OrderedDict[int, _Window] = OrderedDict()

    @property
    def tracked_users(self) -> int:
        """Number of identities currently retained (including idle until next call)."""
        return len(self._users)

    def allow(
        self, user_id: int, *, expensive: bool = False, now: float | None = None
    ) -> bool:
        """Consume quotas only if the event fits every applicable rolling limit.

        ``now`` is a test hook; supplied timestamps must be nondecreasing and use
        one consistent clock. Production callers should leave it unset, using
        the monotonic clock rather than wall-clock time.
        """
        current = monotonic() if now is None else now
        cutoff = current - self.window_seconds

        # Access order is also last_seen order for a monotonic clock. A user idle
        # for the full window cannot have an accepted event still in that window.
        while self._users:
            oldest = next(iter(self._users.values()))
            if oldest.last_seen > cutoff:
                break
            self._users.popitem(last=False)

        entry = self._users.get(user_id)
        if entry is None:
            if len(self._users) >= self.max_users:
                self._users.popitem(last=False)
            entry = _Window(last_seen=current)
            self._users[user_id] = entry
        else:
            entry.last_seen = current
            self._users.move_to_end(user_id)

        for events in (entry.events, entry.expensive_events):
            while events and events[0] <= cutoff:
                events.popleft()

        if len(entry.events) >= self.limit:
            return False
        if expensive and len(entry.expensive_events) >= self.expensive_limit:
            return False

        entry.events.append(current)
        if expensive:
            entry.expensive_events.append(current)
        return True

    def should_notify(self, user_id: int, now: float | None = None) -> bool:
        """Allow at most one denial notice per user per 10 seconds.

        Call only after ``allow`` returns False, with the same clock. Untracked
        identities return False without creating state. The caller sends any
        notice; this limiter performs no Telegram requests.
        """
        entry = self._users.get(user_id)
        if entry is None:
            return False
        current = monotonic() if now is None else now
        if entry.last_notified is not None and current - entry.last_notified < 10:
            return False
        entry.last_notified = current
        return True
