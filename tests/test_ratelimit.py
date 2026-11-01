import asyncio
from datetime import UTC, datetime

import pytest

from answers.ratelimit import MAX_COOLDOWN, RateLimiter, retry_after


class FakeClock:
    def __init__(self, late: float = 0.0) -> None:
        self.now = 100.0
        self.late = late
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds + self.late
        await asyncio.sleep(0)


async def starts(limiter: RateLimiter, clock: FakeClock, count: int) -> list[float]:
    times = []
    for _ in range(count):
        await limiter.wait()
        times.append(round(clock.now - 100.0, 4))
    return times


@pytest.mark.asyncio
async def test_limiter_spaces_request_starts() -> None:
    clock = FakeClock()
    limiter = RateLimiter(4.0, clock=clock, sleep=clock.sleep)

    assert await starts(limiter, clock, 3) == [0.0, 0.25, 0.5]


@pytest.mark.asyncio
async def test_a_late_wake_up_does_not_start_two_requests_together() -> None:
    clock = FakeClock(late=0.8)
    limiter = RateLimiter(3.0, clock=clock, sleep=clock.sleep)

    times = await starts(limiter, clock, 4)

    assert all(later - earlier >= 1 / 3 - 1e-6 for earlier, later in zip(times, times[1:], strict=False))


@pytest.mark.asyncio
async def test_limiter_does_not_sleep_after_a_pause() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2.0, clock=clock, sleep=clock.sleep)

    await limiter.wait()
    clock.now += 5
    await limiter.wait()

    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_parallel_callers_share_one_limit() -> None:
    clock = FakeClock()
    limiter = RateLimiter(8.0, clock=clock, sleep=clock.sleep)

    await asyncio.gather(*(limiter.wait() for _ in range(4)))

    assert clock.sleeps == [0.125, 0.125, 0.125]


@pytest.mark.asyncio
async def test_a_cancelled_waiter_gives_its_turn_to_the_next() -> None:
    gate = asyncio.Event()
    clock = FakeClock()

    async def blocked_sleep(seconds: float) -> None:
        await gate.wait()

    limiter = RateLimiter(1.0, clock=clock, sleep=blocked_sleep)
    await limiter.wait()
    waiter = asyncio.create_task(limiter.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    clock.now += 1.0

    await asyncio.wait_for(limiter.wait(), 1)


@pytest.mark.asyncio
async def test_throttling_pauses_every_caller_of_the_group() -> None:
    clock = FakeClock()
    limiter = RateLimiter(8.0, clock=clock, sleep=clock.sleep)
    await limiter.wait()

    assert limiter.throttled(12.0) == 12.0
    await asyncio.gather(limiter.wait(), limiter.wait())

    assert clock.now - 100.0 == pytest.approx(12.0 + 0.125)


def test_backoff_doubles_without_retry_after_and_stays_bounded() -> None:
    clock = FakeClock()
    limiter = RateLimiter(8.0, clock=clock, sleep=clock.sleep)

    delays = [limiter.throttled(None) for _ in range(5)]

    assert delays == [30.0, 60.0, 120.0, 240.0, MAX_COOLDOWN]
    assert limiter.throttled(10_000.0) == MAX_COOLDOWN
    assert limiter.throttled(0.0) == 1.0


def test_backoff_stays_at_the_cap_after_many_strikes_without_a_quiet_period() -> None:
    clock = FakeClock()
    limiter = RateLimiter(8.0, clock=clock, sleep=clock.sleep)

    delays = [limiter.throttled(None) for _ in range(2000)]

    assert delays[-1] == MAX_COOLDOWN
    assert limiter.throttled(None) == MAX_COOLDOWN


def test_backoff_starts_again_after_a_quiet_period() -> None:
    clock = FakeClock()
    limiter = RateLimiter(8.0, clock=clock, sleep=clock.sleep)
    limiter.throttled(None)
    limiter.throttled(None)
    clock.now += 1000

    assert limiter.throttled(None) == 30.0


@pytest.mark.parametrize(
    ('value', 'seconds'),
    [
        ('120', 120.0),
        (' 7 ', 7.0),
        ('Sun, 01 Nov 2026 12:01:00 GMT', 60.0),
        ('soon', None),
        ('-5', None),
        ('²', None),
        (None, None),
    ],
)
def test_retry_after_reads_seconds_and_http_dates(value: str | None, seconds: float | None) -> None:
    assert retry_after(value, datetime(2026, 11, 1, 12, 0, tzinfo=UTC)) == seconds


def test_limiter_rejects_a_rate_that_is_not_positive() -> None:
    with pytest.raises(ValueError, match='positive'):
        RateLimiter(0)
