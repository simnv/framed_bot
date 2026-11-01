"""Space the requests to one host group, and pause the whole group after the server throttles it."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

# Request starts per second for each host group.
SITE_RATE: Final = 3.0  # framed.wtf, episode.wtf and core.framed.wtf together
API_RATE: Final = 8.0  # api.themoviedb.org
CDN_RATE: Final = 8.0  # image.tmdb.org
# Cooldown after HTTP 429: Retry-After when it is valid, else a doubling backoff; always in these bounds.
MIN_COOLDOWN: Final = 1.0
FIRST_BACKOFF: Final = 30.0
MAX_COOLDOWN: Final = 300.0


class Throttled(RuntimeError):
    """A server answered HTTP 429. The run that got it stops; the limiter keeps the host group paused."""


def retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP date), or None when the value is not valid."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())


class RateLimiter:
    """Allow at most `per_second` request starts each second. Callers wait in arrival order."""

    def __init__(
        self,
        per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if per_second <= 0:
            raise ValueError('per_second must be positive')
        self._interval = 1.0 / per_second
        self._clock = clock
        self._sleep = sleep
        self._next: float | None = None
        self._paused_until: float | None = None
        self._strikes = 0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                start = max(now, self._next or now, self._paused_until or now)
                if start <= now:
                    break
                await self._sleep(start - now)
            # Count from the real grant time: a late wake-up must not let the next caller start at once.
            self._next = now + self._interval

    def throttled(self, retry_after_seconds: float | None) -> float:
        """Pause the host group after HTTP 429 and return the pause in seconds."""
        now = self._clock()
        if self._paused_until is not None and now - self._paused_until > MAX_COOLDOWN:
            self._strikes = 0
        if retry_after_seconds is None:
            delay = FIRST_BACKOFF * 2**self._strikes
            self._strikes += 1
        else:
            delay = retry_after_seconds
        delay = min(max(delay, MIN_COOLDOWN), MAX_COOLDOWN)
        self._paused_until = max(self._paused_until or now, now + delay)
        return delay
