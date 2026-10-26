"""Bounded, ephemeral reaction delivery. Settings are policy defaults, not API limits."""

import asyncio
import logging
import random
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from telegram import ReactionTypeEmoji
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

logger = logging.getLogger(__name__)


def _retry_jitter() -> float:
    return random.uniform(0, 0.25)  # noqa: S311 - Backoff jitter.


class ReactionBot(Protocol):
    async def set_message_reaction(
        self, *, chat_id: int, message_id: int, reaction: ReactionTypeEmoji, read_timeout: float
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReactionIntent:
    chat_id: int
    message_id: int
    emoji: str

    @property
    def key(self) -> tuple[int, int]:
        return self.chat_id, self.message_id


@dataclass(frozen=True, slots=True)
class DeliverySettings:
    capacity: int = 64
    global_limit: int = 4
    chat_gap: float = 1.0
    max_attempts: int = 3
    retry_base: float = 1.0
    read_timeout: float = 10.0
    attempt_timeout: float = 20.0
    intent_ttl: float = 300.0
    drain_timeout: float = 45.0
    receipt_limit: int = 512


@dataclass(slots=True, eq=False)
class _Work:
    intent: ReactionIntent
    admitted: float
    due: float
    attempts: int = 0
    active: bool = False


@dataclass(frozen=True, slots=True)
class _Outcome:
    error_class: str | None = None
    retry_delay: float | None = None
    shared_cooldown: bool = False


class ReactionDelivery:
    def __init__(
        self,
        bot: ReactionBot,
        settings: DeliverySettings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = _retry_jitter,
    ) -> None:
        settings = settings or DeliverySettings()
        if min(settings.capacity, settings.global_limit, settings.max_attempts, settings.receipt_limit) < 1:
            raise ValueError('Delivery bounds must be positive')
        self.bot = bot
        self.settings = settings
        self.clock = clock
        self.sleep = sleep
        self.jitter = jitter
        self._items: dict[tuple[int, int], _Work] = {}
        self._chats: OrderedDict[int, deque[_Work]] = OrderedDict()
        self._chat_next: OrderedDict[int, float] = OrderedDict()
        self._receipts: OrderedDict[tuple[int, int], tuple[ReactionIntent, float]] = OrderedDict()
        self._active: dict[asyncio.Task[_Outcome], _Work] = {}
        self._cooldown = 0.0
        self._wake = asyncio.Event()
        self._capacity_changed = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing = False
        self._runner: asyncio.Task[None] | None = None
        self._stop_complete = False
        self.counts: Counter[str] = Counter()

    @property
    def pending_count(self) -> int:
        """Includes in-flight attempts and delayed retries in their original slots."""
        return len(self._items)

    @property
    def active_count(self) -> int:
        return len(self._active)

    def start(self) -> None:
        if self._closing:
            raise RuntimeError('Reaction delivery is stopping')
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name='reaction-delivery')

    def known_intent(self, chat_id: int, message_id: int) -> ReactionIntent | None:
        """Same-process replay protection; deliberately not a durable delivery receipt."""
        self._prune()
        key = chat_id, message_id
        if key in self._items:
            return self._items[key].intent
        receipt = self._receipts.get(key)
        return receipt[0] if receipt is not None else None

    async def submit(self, intent: ReactionIntent) -> None:
        """Await capacity, never await delivery. The blocking handler bounds waiters."""
        try:
            while True:
                self._prune()
                if intent.key in self._items:
                    return
                receipt = self._receipts.get(intent.key)
                if receipt is not None:
                    return
                if self._closing:
                    self._log('shutdown_rejected', _Work(intent, self.clock(), self.clock()), 'Shutdown')
                    return
                if len(self._items) < self.settings.capacity:
                    self.start()
                    now = self.clock()
                    work = _Work(intent, now, now)
                    self._items[intent.key] = work
                    self._chats.setdefault(intent.chat_id, deque()).append(work)
                    self._idle.clear()
                    self._log('submitted', work)
                    self._wake.set()
                    return
                self._capacity_changed.clear()
                await self._capacity_changed.wait()
        except asyncio.CancelledError:
            self._log('admission_cancelled', _Work(intent, self.clock(), self.clock()), 'CancelledError')
            raise

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def close_admissions(self) -> None:
        self._closing = True
        self._capacity_changed.set()
        self._wake.set()

    async def stop(self) -> None:
        """Called by PTB post_stop, while its initialized Bot client is still open."""
        if self._stop_complete:
            return
        self.close_admissions()
        if self._runner is None:
            self._stop_complete = True
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._runner), self.settings.drain_timeout)
        except TimeoutError:
            logger.warning('reaction_delivery event=drain_deadline undelivered=%s', self.pending_count)
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        except asyncio.CancelledError:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
            raise
        finally:
            self._stop_complete = True

    def _log(self, event: str, work: _Work, error_class: str = '-') -> None:
        self.counts[event] += 1
        level = (
            logging.WARNING
            if event in {'failed', 'expired', 'shutdown_dropped', 'shutdown_rejected', 'admission_cancelled'}
            else logging.INFO
        )
        logger.log(
            level,
            'reaction_delivery event=%s chat_id=%s message_id=%s emoji=%s attempts=%s age_seconds=%.3f error_class=%s',
            event,
            work.intent.chat_id,
            work.intent.message_id,
            work.intent.emoji,
            work.attempts,
            max(0.0, self.clock() - work.admitted),
            error_class,
        )

    def _prune(self) -> None:
        now = self.clock()
        for key, (_, completed) in list(self._receipts.items()):
            if now - completed >= self.settings.intent_ttl:
                del self._receipts[key]
        for chat, due in list(self._chat_next.items()):
            if chat not in self._chats and due <= now:
                del self._chat_next[chat]

    def _finish(self, work: _Work, event: str, error_class: str = '-') -> None:
        self._log(event, work, error_class)
        self._items.pop(work.intent.key)
        queue = self._chats[work.intent.chat_id]
        queue.remove(work)
        if not queue:
            del self._chats[work.intent.chat_id]
        self._receipts[work.intent.key] = work.intent, self.clock()
        self._receipts.move_to_end(work.intent.key)
        while len(self._receipts) > self.settings.receipt_limit:
            self._receipts.popitem(last=False)
        self._capacity_changed.set()
        if not self._items:
            self._idle.set()

    async def _attempt(self, work: _Work) -> _Outcome:
        try:
            remaining = self.settings.intent_ttl - (self.clock() - work.admitted)
            async with asyncio.timeout(min(self.settings.attempt_timeout, remaining)):
                await self.bot.set_message_reaction(
                    chat_id=work.intent.chat_id,
                    message_id=work.intent.message_id,
                    reaction=ReactionTypeEmoji(work.intent.emoji),
                    read_timeout=self.settings.read_timeout,
                )
            return _Outcome()
        except (BadRequest, Forbidden) as error:
            # BadRequest subclasses NetworkError: permanent errors must come first.
            return _Outcome(type(error).__name__)
        except RetryAfter as error:
            delay = error.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
            return _Outcome(type(error).__name__, max(0, seconds) + 0.05 + self.jitter(), True)
        except (TimedOut, NetworkError, TimeoutError) as error:
            delay = self.settings.retry_base * 2 ** (work.attempts - 1) + self.jitter()
            return _Outcome(type(error).__name__, delay)
        except TelegramError as error:
            return _Outcome(type(error).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Log only the category, never exception text/request URLs.
            return _Outcome(type(error).__name__)

    def _complete_attempts(self) -> None:
        for task, work in list(self._active.items()):
            if not task.done():
                continue
            del self._active[task]
            work.active = False
            outcome = task.result()
            if outcome.error_class is None:
                self._finish(work, 'succeeded')
                continue
            now = self.clock()
            if outcome.shared_cooldown and outcome.retry_delay is not None:
                # RetryAfter has no reliable scope: conservatively pause all reactions.
                self._cooldown = max(self._cooldown, now + outcome.retry_delay)
            if now - work.admitted >= self.settings.intent_ttl:
                self._finish(work, 'expired', outcome.error_class)
            elif outcome.retry_delay is None or work.attempts >= self.settings.max_attempts:
                self._finish(work, 'failed', outcome.error_class)
            elif now + outcome.retry_delay >= work.admitted + self.settings.intent_ttl:
                self._finish(work, 'expired', outcome.error_class)
            else:
                work.due = now + outcome.retry_delay
                self._log('retry_scheduled', work, outcome.error_class)

    async def _wait(self, delay: float | None) -> None:
        if delay is None:
            await self._wake.wait()
            return
        waiter = asyncio.create_task(self._wake.wait())
        timer = asyncio.ensure_future(self.sleep(max(0.0, delay)))
        try:
            await asyncio.wait((waiter, timer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            timer.cancel()
            await asyncio.gather(waiter, timer, return_exceptions=True)

    async def _run(self) -> None:
        try:
            while not self._closing or self._items:
                self._wake.clear()
                self._complete_attempts()
                self._prune()
                now = self.clock()
                for work in list(self._items.values()):
                    if now - work.admitted >= self.settings.intent_ttl:
                        for task, active_work in list(self._active.items()):
                            if active_work is work:
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                                del self._active[task]
                        self._finish(work, 'expired', 'IntentExpired')
                for chat, queue in list(self._chats.items()):
                    if len(self._active) >= self.settings.global_limit:
                        break
                    work = queue[0]
                    due = max(work.due, self._chat_next.get(chat, 0), self._cooldown)
                    if work.active or due > now:
                        continue
                    work.active = True
                    work.attempts += 1
                    self._chat_next[chat] = now + self.settings.chat_gap
                    self._chat_next.move_to_end(chat)
                    # Bound idle pacing state too. A conservative shared pause preserves
                    # the start gap even if the oldest chat's pacing record is evicted.
                    while len(self._chat_next) > self.settings.receipt_limit + self.settings.capacity:
                        _, evicted_due = self._chat_next.popitem(last=False)
                        self._cooldown = max(self._cooldown, evicted_due)
                    self._chats.move_to_end(chat)
                    self._log('attempt', work)
                    task = asyncio.create_task(self._attempt(work), name='reaction-attempt')
                    self._active[task] = work
                    task.add_done_callback(lambda _: self._wake.set())
                if self._closing and not self._items:
                    break
                deadlines = [work.admitted + self.settings.intent_ttl for work in self._items.values()]
                if len(self._active) < self.settings.global_limit:
                    deadlines += [
                        max(queue[0].due, self._chat_next.get(chat, 0), self._cooldown)
                        for chat, queue in self._chats.items()
                        if not queue[0].active
                    ]
                await self._wait(min(deadlines) - self.clock() if deadlines else None)
        finally:
            self._closing = True
            for task in self._active:
                task.cancel()
            await asyncio.gather(*self._active, return_exceptions=True)
            self._active.clear()
            for work in list(self._items.values()):
                self._finish(work, 'shutdown_dropped', 'CancelledError')
            self._chat_next.clear()
            self._capacity_changed.set()
