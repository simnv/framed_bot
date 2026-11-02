import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, ReactionTypeEmoji, Update, User
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ApplicationBuilder, ContextTypes, ExtBot, MessageHandler

import main
from reaction_delivery import DeliverySettings, ReactionDelivery, ReactionIntent


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + delay, future))
        await future

    def advance(self) -> bool:
        self.sleepers = [(due, future) for due, future in self.sleepers if not future.done()]
        if not self.sleepers:
            return False
        self.now = max(self.now, min(due for due, _ in self.sleepers))
        for due, future in self.sleepers:
            if due <= self.now and not future.done():
                future.set_result(None)
        return True


async def turns() -> None:
    for _ in range(25):
        await asyncio.sleep(0)


async def drive(clock: FakeClock, predicate: Callable[[], bool]) -> None:
    for _ in range(2000):
        await turns()
        if predicate():
            return
        assert clock.advance(), 'Dispatcher stalled with unfinished work and no scheduled clock wakeup'
    pytest.fail('Dispatcher did not settle within the fake-clock bound')


class ScriptedBot:
    def __init__(self, clock: FakeClock, callback: Callable[[ReactionIntent, int], Awaitable[None]] | None = None):
        self.clock = clock
        self.callback = callback
        self.starts: list[tuple[float, ReactionIntent, int]] = []
        self.attempts: Counter[tuple[int, int]] = Counter()
        self.applied: dict[tuple[int, int], str] = {}
        self.in_flight = 0
        self.max_global = 0
        self.per_chat: Counter[int] = Counter()
        self.max_per_chat: Counter[int] = Counter()
        self.read_timeouts: list[float] = []

    async def set_message_reaction(
        self, *, chat_id: int, message_id: int, reaction: ReactionTypeEmoji, read_timeout: float
    ) -> bool:
        intent = ReactionIntent(chat_id, message_id, reaction.emoji)
        self.attempts[intent.key] += 1
        attempt = self.attempts[intent.key]
        self.starts.append((self.clock(), intent, attempt))
        self.read_timeouts.append(read_timeout)
        self.in_flight += 1
        self.per_chat[chat_id] += 1
        self.max_global = max(self.max_global, self.in_flight)
        self.max_per_chat[chat_id] = max(self.max_per_chat[chat_id], self.per_chat[chat_id])
        try:
            if self.callback:
                await self.callback(intent, attempt)
            self.applied[intent.key] = intent.emoji
            return True
        finally:
            self.in_flight -= 1
            self.per_chat[chat_id] -= 1


def dispatcher(bot: ScriptedBot, clock: FakeClock, **settings) -> ReactionDelivery:
    return ReactionDelivery(
        bot, replace(DeliverySettings(), **settings), clock=clock, sleep=clock.sleep, jitter=lambda: 0
    )


def update(message_id: int, *, chat_id: int = -101, round_number: int = 42, first_frame: bool = False) -> Update:
    frames = '🟩 ⬛ ⬛ ⬛ ⬛ ⬛' if first_frame else '🟥 🟩 ⬛ ⬛ ⬛ ⬛'
    message = Message(
        message_id,
        datetime.now(UTC),
        Chat(chat_id, 'group'),
        from_user=User(99, 'Synthetic', False),
        text=f'Framed #{round_number}\n🎥 {frames}\n\nhttps://framed.wtf',
    )
    return Update(message_id, message=message)


def context_for(monkeypatch, delivery: ReactionDelivery):
    application = ApplicationBuilder().application_class(main.ReactionApplication).token('123:synthetic-only').build()
    assert isinstance(application, main.ReactionApplication)
    application.reaction_delivery = delivery
    monkeypatch.setattr(main.User, 'update_from_tg_user', AsyncMock())
    no_text = AsyncMock(side_effect=AssertionError('Result delivery must never send text'))
    monkeypatch.setattr(ExtBot, 'send_message', no_text)
    return ContextTypes.DEFAULT_TYPE(application), no_text


@pytest.mark.asyncio
async def test_startup_batch_retries_all_seventeen_without_resaving_or_text(monkeypatch):
    clock = FakeClock()

    async def fail_first(_intent, attempt):
        await clock.sleep(0.1)
        if attempt == 1:
            raise TimedOut()

    bot = ScriptedBot(clock, fail_first)
    delivery = dispatcher(bot, clock)
    context, no_text = context_for(monkeypatch, delivery)
    persisted: Counter[int] = Counter()

    class Results:
        @staticmethod
        async def save_result(user_id, framed_round, won, win_frame, *, chat_id):
            persisted[framed_round] += 1
            return True

    for index in range(17):
        await main.save_results(
            update(index + 1, chat_id=-(index % 5 + 100), round_number=index, first_frame=index % 3 == 0),
            context,
            main.framed_pattern,
            Results,
        )
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.stop()

    assert len(bot.applied) == 17
    assert set(bot.attempts.values()) == {2}
    assert len(persisted) == 17 and set(persisted.values()) == {1}
    assert bot.max_global == 4
    assert set(bot.max_per_chat.values()) == {1}
    starts: dict[int, list[float]] = defaultdict(list)
    for started, intent, _ in bot.starts:
        starts[intent.chat_id].append(started)
    assert all(b - a >= 1 for times in starts.values() for a, b in zip(times, times[1:], strict=False))
    assert set(bot.read_timeouts) == {10}
    assert delivery.counts['succeeded'] == delivery.counts['retry_scheduled'] == 17
    no_text.assert_not_called()


@pytest.mark.asyncio
async def test_slow_chat_and_its_retry_do_not_hold_other_chats():
    clock = FakeClock()

    async def slow_first(intent, attempt):
        if intent.message_id == 1 and attempt == 1:
            await clock.sleep(5)
            raise NetworkError('synthetic outage')

    bot = ScriptedBot(clock, slow_first)
    delivery = dispatcher(bot, clock)
    for message_id in range(1, 9):
        await delivery.submit(ReactionIntent(-1, message_id, '✍'))
    await delivery.submit(ReactionIntent(-2, 99, '🏆'))
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.stop()
    assert next(t for t, intent, _ in bot.starts if intent.chat_id == -2) == 0
    first_chat = [(intent.message_id, attempt) for _, intent, attempt in bot.starts if intent.chat_id == -1]
    assert first_chat[:3] == [(1, 1), (1, 2), (2, 1)]
    assert len(bot.applied) == 9


@pytest.mark.asyncio
async def test_uncertain_applied_timeout_and_same_update_replays_keep_trophy(monkeypatch):
    clock = FakeClock()

    async def apply_then_timeout(intent, attempt):
        bot.applied[intent.key] = intent.emoji
        if attempt == 1:
            raise TimedOut()

    bot = ScriptedBot(clock, apply_then_timeout)
    delivery = dispatcher(bot, clock)
    context, no_text = context_for(monkeypatch, delivery)
    save = AsyncMock(side_effect=[True, False])

    class Results:
        save_result = save

    original = update(1, first_frame=True)
    await main.save_results(original, context, main.framed_pattern, Results)
    await main.save_results(original, context, main.framed_pattern, Results)
    await drive(clock, lambda: delivery.pending_count == 0)
    await main.save_results(original, context, main.framed_pattern, Results)
    assert save.await_count == 1
    await main.save_results(update(2, first_frame=True), context, main.framed_pattern, Results)
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.stop()
    assert save.await_count == 2
    assert bot.applied == {(-101, 1): '🏆', (-101, 2): '👻'}
    assert [intent.emoji for _, intent, _ in bot.starts] == ['🏆', '🏆', '👻', '👻']
    no_text.assert_not_called()


@pytest.mark.parametrize(
    ('error', 'attempts'),
    [
        (BadRequest('private-marker'), 1),
        (Forbidden('private-marker'), 1),
        (TelegramError('private-marker'), 1),
        (TimedOut(), 3),
        (NetworkError('private-marker'), 3),
        (RetryAfter(timedelta(seconds=1)), 3),
    ],
)
@pytest.mark.asyncio
async def test_error_categories_and_terminal_metadata_are_safe(error, attempts, caplog):
    clock = FakeClock()

    async def fail(_intent, _attempt):
        raise error

    bot = ScriptedBot(clock, fail)
    delivery = dispatcher(bot, clock)
    caplog.set_level(logging.INFO, logger='reaction_delivery')
    await delivery.submit(ReactionIntent(-123, 456, '🏆'))
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.stop()
    assert issubclass(BadRequest, NetworkError)
    assert len(bot.starts) == attempts
    assert delivery.counts['failed'] == 1 and not bot.applied
    final = next(record.getMessage() for record in caplog.records if 'event=failed ' in record.getMessage())
    assert 'chat_id=-123 message_id=456 emoji=🏆' in final
    assert f'attempts={attempts}' in final and f'error_class={type(error).__name__}' in final
    assert 'private-marker' not in caplog.text


@pytest.mark.parametrize('retry_after', [2, timedelta(seconds=2)])
@pytest.mark.asyncio
async def test_retry_after_respects_delay_and_shared_unknown_scope(retry_after, monkeypatch):
    if isinstance(retry_after, timedelta):
        monkeypatch.setenv('PTB_TIMEDELTA', '1')
    else:
        monkeypatch.delenv('PTB_TIMEDELTA', raising=False)
    clock = FakeClock()

    async def limited(intent, attempt):
        if intent.chat_id == -1 and attempt == 1:
            raise RetryAfter(retry_after)

    bot = ScriptedBot(clock, limited)
    delivery = dispatcher(bot, clock)
    await delivery.submit(ReactionIntent(-1, 1, '✍'))
    await drive(clock, lambda: delivery.counts['retry_scheduled'] == 1)
    await delivery.submit(ReactionIntent(-2, 2, '🏆'))
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.stop()
    assert all(started >= 2.05 for started, _, _ in bot.starts[1:])
    assert len(bot.applied) == 2


@pytest.mark.asyncio
async def test_retry_after_beyond_lifetime_ends_visibly_without_early_retry(caplog):
    clock = FakeClock()

    async def limited(_intent, _attempt):
        raise RetryAfter(400)

    bot = ScriptedBot(clock, limited)
    delivery = dispatcher(bot, clock)
    await delivery.submit(ReactionIntent(-1, 1, '👻'))
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.stop()
    assert len(bot.starts) == 1 and delivery.counts['expired'] == 1
    assert 'error_class=RetryAfter' in caplog.text


@pytest.mark.asyncio
async def test_full_admission_and_retries_retain_slots_without_deadlock():
    clock = FakeClock()
    gate = asyncio.Event()

    async def first_fails(_intent, attempt):
        await gate.wait()
        if attempt == 1:
            raise TimedOut()

    bot = ScriptedBot(clock, first_fails)
    delivery = dispatcher(bot, clock, capacity=2, global_limit=1)
    await delivery.submit(ReactionIntent(-1, 1, '✍'))
    await delivery.submit(ReactionIntent(-2, 2, '🏆'))
    admission = asyncio.create_task(delivery.submit(ReactionIntent(-3, 3, '👻')))
    await turns()
    assert delivery.pending_count == 2 and delivery.active_count == 1 and not admission.done()
    gate.set()
    await drive(clock, admission.done)
    await admission
    assert delivery.pending_count <= 2
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.stop()
    assert len(bot.applied) == 3 and set(bot.attempts.values()) == {2}


@pytest.mark.asyncio
async def test_completed_failure_replay_preserves_intent_and_attempt_cap_and_cache_evicts():
    clock = FakeClock()

    async def fail(_intent, _attempt):
        raise TimedOut()

    bot = ScriptedBot(clock, fail)
    delivery = dispatcher(bot, clock, receipt_limit=2)
    original = ReactionIntent(-1, 1, '🏆')
    await delivery.submit(original)
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.submit(ReactionIntent(-1, 1, '👻'))
    await turns()
    assert len(bot.starts) == 3 and delivery.known_intent(-1, 1) == original
    for message_id in (2, 3):
        await delivery.submit(ReactionIntent(-1, message_id, '✍'))
        await drive(clock, lambda: delivery.pending_count == 0)
    assert delivery.known_intent(-1, 1) is None
    assert delivery.known_intent(-1, 3) == ReactionIntent(-1, 3, '✍')
    assert len(delivery._receipts) == 2
    await delivery.stop()


@pytest.mark.asyncio
async def test_application_ingestion_is_bounded_and_state_is_not_persisted():
    application = main.build_application()
    assert application.update_queue.maxsize == 100
    assert application.update_processor.max_concurrent_updates == 1
    for group in application.handlers.values():
        for handler in group:
            if handler.callback in (main.new_framed_data, main.new_episode_data, main.update_chat_data):
                assert handler.block is True
    assert application.post_stop is main.stop_reaction_delivery
    assert application.bot_data == {}
    for number in range(100):
        application.update_queue.put_nowait(number)
    blocked = asyncio.create_task(application.update_queue.put(101))
    await turns()
    assert not blocked.done()
    application.update_queue.get_nowait()
    await blocked


@pytest.mark.asyncio
async def test_graceful_stop_drains_before_client_close_and_rejects_new_intents():
    clock = FakeClock()
    gate = asyncio.Event()
    client_open = True

    async def send(_intent, _attempt):
        await gate.wait()
        assert client_open

    bot = ScriptedBot(clock, send)
    delivery = dispatcher(bot, clock)
    await delivery.submit(ReactionIntent(-1, 1, '🏆'))
    await turns()
    stopping = asyncio.create_task(delivery.stop())
    await turns()
    assert not stopping.done()
    gate.set()
    await stopping
    client_open = False
    await delivery.submit(ReactionIntent(-1, 2, '👻'))
    assert len(bot.applied) == 1 and delivery.counts['shutdown_rejected'] == 1
    assert delivery.pending_count == delivery.active_count == 0


@pytest.mark.asyncio
async def test_real_ptb_stop_releases_full_admission_and_full_update_queue_before_client_close(monkeypatch):
    clock = FakeClock()

    async def blocked(_intent, _attempt):
        await asyncio.Event().wait()

    bot = ScriptedBot(clock, blocked)
    delivery = dispatcher(bot, clock, capacity=1, drain_timeout=0.02)
    application = (
        ApplicationBuilder()
        .application_class(main.ReactionApplication)
        .token('123:synthetic-only')
        .updater(None)
        .job_queue(None)
        .update_queue(asyncio.Queue(maxsize=100))
        .post_stop(main.stop_reaction_delivery)
        .build()
    )
    assert isinstance(application, main.ReactionApplication)
    application.reaction_delivery = delivery
    application.add_handler(MessageHandler(main.FRAMED_FILTER, main.new_framed_data, block=True))
    saved = AsyncMock(return_value=True)
    monkeypatch.setattr(main.User, 'update_from_tg_user', AsyncMock())
    monkeypatch.setattr(main.FramedResult, 'save_result', saved)

    async def initialize(fake_bot):
        fake_bot._bot_user = User(1, 'Synthetic bot', True)

    closed = []

    async def shutdown(_fake_bot):
        assert delivery.pending_count == delivery.active_count == 0
        closed.append(True)

    monkeypatch.setattr(ExtBot, 'initialize', initialize)
    monkeypatch.setattr(ExtBot, 'shutdown', shutdown)
    await application.initialize()
    await application.start()
    for message_id in range(1, 101):
        application.update_queue.put_nowait(update(message_id, round_number=message_id))
    await turns()
    assert saved.await_count == 2 and delivery.pending_count == 1
    for message_id in (101, 102):
        application.update_queue.put_nowait(update(message_id, round_number=message_id))
    assert application.update_queue.full()
    await asyncio.wait_for(application.stop(), 1)
    await main.stop_reaction_delivery(application)
    await application.shutdown()
    assert closed and saved.await_count == 102
    assert delivery.counts['shutdown_rejected'] == 101 and delivery.counts['shutdown_dropped'] == 1
    assert not delivery.counts['succeeded'] and not bot.applied
    assert application.update_queue.empty()


@pytest.mark.asyncio
async def test_drain_deadline_reports_every_leftover_and_cleans_tasks(caplog):
    clock = FakeClock()

    async def blocked(_intent, _attempt):
        await asyncio.Event().wait()

    bot = ScriptedBot(clock, blocked)
    delivery = dispatcher(bot, clock, drain_timeout=0.01)
    await delivery.submit(ReactionIntent(-1, 1, '🏆'))
    await delivery.submit(ReactionIntent(-1, 2, '👻'))
    await turns()
    await delivery.stop()
    assert delivery.pending_count == delivery.active_count == 0
    assert delivery.counts['shutdown_dropped'] == 2 and delivery.counts['succeeded'] == 0
    assert 'event=drain_deadline undelivered=2' in caplog.text
    assert 'message_id=1 emoji=🏆 attempts=1' in caplog.text
    assert 'message_id=2 emoji=👻 attempts=0' in caplog.text
    assert not delivery._chats and not delivery._chat_next
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith('reaction-')]


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry_or_success():
    clock = FakeClock()

    async def blocked(_intent, _attempt):
        await asyncio.Event().wait()

    bot = ScriptedBot(clock, blocked)
    delivery = dispatcher(bot, clock, capacity=1)
    await delivery.submit(ReactionIntent(-1, 1, '🏆'))
    waiting = asyncio.create_task(delivery.submit(ReactionIntent(-2, 2, '👻')))
    await turns()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    stopping = asyncio.create_task(delivery.stop())
    await turns()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert len(bot.starts) == 1 and not bot.applied
    assert delivery.counts['admission_cancelled'] == delivery.counts['shutdown_dropped'] == 1
    assert delivery.pending_count == delivery.active_count == 0


@pytest.mark.asyncio
async def test_outer_attempt_deadline_includes_wait_inside_api_and_bounds_retries():
    clock = FakeClock()

    async def blocked(_intent, _attempt):
        await asyncio.Event().wait()

    bot = ScriptedBot(clock, blocked)
    delivery = ReactionDelivery(
        bot, DeliverySettings(chat_gap=0, retry_base=0, attempt_timeout=0.005), jitter=lambda: 0
    )
    await delivery.submit(ReactionIntent(-1, 1, '🏆'))
    await asyncio.wait_for(delivery.wait_idle(), 1)
    await delivery.stop()
    assert len(bot.starts) == 3 and delivery.counts['failed'] == 1
    assert bot.in_flight == 0 and delivery.pending_count == 0


@pytest.mark.asyncio
async def test_lifetime_cancels_active_and_queued_work_without_stale_retry():
    clock = FakeClock()

    async def slow(_intent, _attempt):
        await clock.sleep(1)

    bot = ScriptedBot(clock, slow)
    delivery = dispatcher(bot, clock, intent_ttl=0.5)
    await delivery.submit(ReactionIntent(-1, 1, '🏆'))
    await delivery.submit(ReactionIntent(-1, 2, '👻'))
    await drive(clock, lambda: delivery.pending_count == 0)
    await delivery.stop()
    assert len(bot.starts) == 1 and not bot.applied
    assert delivery.counts['expired'] == 2 and bot.in_flight == 0
