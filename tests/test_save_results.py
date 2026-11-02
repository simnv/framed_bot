from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, ReactionTypeEmoji, Update, User
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import ApplicationBuilder, ContextTypes, ExtBot, JobQueue

import main
from answers import Game
from answers.store import DbAnswerStore
from answers.sync import AnswerSync
from models.episode_result import EpisodeResult
from models.framed_result import FramedResult
from models.user import User as BotUser
from reaction_delivery import DeliverySettings, ReactionDelivery


@dataclass(frozen=True, slots=True)
class ReactionCall:
    chat_id: int
    message_id: int
    reaction: ReactionTypeEmoji


@dataclass(frozen=True, slots=True)
class SendMessageCall:
    chat_id: int
    text: str
    reply_to_message_id: int


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    callback: Callable[[ContextTypes.DEFAULT_TYPE], Awaitable[None]]
    when: timedelta
    data: int
    chat_id: int


@dataclass(slots=True)
class BotRecorder:
    reaction_error: TelegramError | None = None
    reaction_calls: list[ReactionCall] = field(default_factory=list)
    send_message_calls: list[SendMessageCall] = field(default_factory=list)

    async def set_message_reaction(
        self, *, chat_id: int, message_id: int, reaction: ReactionTypeEmoji, read_timeout: float
    ) -> bool:
        self.reaction_calls.append(ReactionCall(chat_id=chat_id, message_id=message_id, reaction=reaction))
        if self.reaction_error is not None:
            raise self.reaction_error
        return True

    async def send_message(self, *, chat_id: int, text: str, reply_to_message_id: int) -> Message:
        self.send_message_calls.append(
            SendMessageCall(chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id)
        )
        user = User(id=1, first_name='Bot', is_bot=True)
        chat = Chat(id=chat_id, type='group')
        return Message(
            message_id=777,
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text=text,
        )


@dataclass(slots=True)
class JobQueueRecorder:
    calls: list[ScheduledJob] = field(default_factory=list)

    def run_once(
        self,
        callback: Callable[[ContextTypes.DEFAULT_TYPE], Awaitable[None]],
        when: timedelta,
        data: int,
        *,
        chat_id: int,
    ) -> ScheduledJob:
        job = ScheduledJob(callback=callback, when=when, data=data, chat_id=chat_id)
        self.calls.append(job)
        return job


@dataclass(frozen=True, slots=True)
class RecordedContext:
    context: ContextTypes.DEFAULT_TYPE
    bot: BotRecorder
    job_queue: JobQueueRecorder


def make_update(text: str = 'Framed #42\n🎥 🟥 🟩 ⬛ ⬛ ⬛ ⬛\n\nhttps://framed.wtf') -> Update:
    user = User(id=99, first_name='Test', is_bot=False)
    chat = Chat(id=123, type='group')
    message = Message(
        message_id=456,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=1, message=message)


def make_context(monkeypatch: pytest.MonkeyPatch, *, reaction_error: TelegramError | None = None) -> RecordedContext:
    application = ApplicationBuilder().application_class(main.ReactionApplication).token('123:ABC').build()
    assert isinstance(application, main.ReactionApplication)
    application.reaction_delivery = ReactionDelivery(
        application.bot, DeliverySettings(chat_gap=0, retry_base=0), jitter=lambda: 0
    )
    context = ContextTypes.DEFAULT_TYPE(application)
    bot = BotRecorder(reaction_error=reaction_error)
    job_queue = JobQueueRecorder()
    assert context.job_queue is not None

    async def set_message_reaction(
        _bot: ExtBot[None], *, chat_id: int, message_id: int, reaction: ReactionTypeEmoji, read_timeout: float
    ) -> bool:
        return await bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id, reaction=reaction, read_timeout=read_timeout
        )

    async def send_message(_bot: ExtBot[None], *, chat_id: int, text: str, reply_to_message_id: int) -> Message:
        return await bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=reply_to_message_id)

    def run_once(
        _job_queue: JobQueue[ContextTypes.DEFAULT_TYPE],
        callback: Callable[[ContextTypes.DEFAULT_TYPE], Awaitable[None]],
        when: timedelta,
        data: int,
        *,
        chat_id: int,
    ) -> ScheduledJob:
        return job_queue.run_once(callback, when, data, chat_id=chat_id)

    monkeypatch.setattr(type(context.bot), 'set_message_reaction', set_message_reaction)
    monkeypatch.setattr(type(context.bot), 'send_message', send_message)
    monkeypatch.setattr(type(context.job_queue), 'run_once', run_once)
    return RecordedContext(context=context, bot=bot, job_queue=job_queue)


async def finish_delivery(test_context: RecordedContext) -> None:
    delivery = main.reaction_delivery_for(test_context.context.application)
    await delivery.wait_idle()
    await delivery.stop()


def make_result_model(saved: bool):
    class ResultModel:
        calls: list[tuple[int, int, bool, int | None, int]] = []

        @staticmethod
        async def save_result(
            user_id: int, framed_round: int, won: bool, win_frame: int | None, *, chat_id: int
        ) -> bool:
            ResultModel.calls.append((user_id, framed_round, won, win_frame, chat_id))
            return saved

    return ResultModel


@pytest.mark.asyncio
async def test_save_results_uses_saved_reaction_when_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    update = make_update()
    test_context = make_context(monkeypatch)
    result_model = make_result_model(True)
    update_from_user = AsyncMock()
    monkeypatch.setattr(BotUser, 'update_from_tg_user', update_from_user)

    await main.save_results(update, test_context.context, main.framed_pattern, result_model)
    await finish_delivery(test_context)

    update_from_user.assert_awaited_once_with(update.effective_user)
    assert result_model.calls == [(99, 42, True, 2, 123)]
    assert test_context.bot.reaction_calls == [
        ReactionCall(chat_id=123, message_id=456, reaction=main.saved_result_reaction)
    ]
    assert test_context.bot.send_message_calls == []
    assert test_context.job_queue.calls == []


@pytest.mark.asyncio
async def test_save_results_uses_trophy_reaction_when_saved_on_first_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = make_update('Framed #42\n🎥 🟩 ⬛ ⬛ ⬛ ⬛ ⬛\n\nhttps://framed.wtf')
    test_context = make_context(monkeypatch)
    result_model = make_result_model(True)
    update_from_user = AsyncMock()
    monkeypatch.setattr(BotUser, 'update_from_tg_user', update_from_user)

    await main.save_results(update, test_context.context, main.framed_pattern, result_model)
    await finish_delivery(test_context)

    assert result_model.calls == [(99, 42, True, 1, 123)]
    assert test_context.bot.reaction_calls == [
        ReactionCall(chat_id=123, message_id=456, reaction=main.first_frame_saved_result_reaction)
    ]
    assert test_context.bot.send_message_calls == []
    assert test_context.job_queue.calls == []


@pytest.mark.asyncio
async def test_save_results_uses_duplicate_reaction_when_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    update = make_update()
    test_context = make_context(monkeypatch)
    result_model = make_result_model(False)
    update_from_user = AsyncMock()
    monkeypatch.setattr(BotUser, 'update_from_tg_user', update_from_user)

    await main.save_results(update, test_context.context, main.framed_pattern, result_model)
    await finish_delivery(test_context)

    assert result_model.calls == [(99, 42, True, 2, 123)]
    assert test_context.bot.reaction_calls == [
        ReactionCall(chat_id=123, message_id=456, reaction=main.duplicate_result_reaction)
    ]
    assert test_context.bot.send_message_calls == []
    assert test_context.job_queue.calls == []


@pytest.mark.parametrize('game', ['framed', 'episode'])
@pytest.mark.parametrize(
    ('saved', 'win_frame', 'reaction'),
    [
        (True, 2, main.saved_result_reaction),
        (True, 1, main.first_frame_saved_result_reaction),
        (False, 2, main.duplicate_result_reaction),
    ],
    ids=['saved', 'first-frame', 'duplicate'],
)
@pytest.mark.parametrize(
    'reaction_error',
    [None, TelegramError('reactions are unavailable'), TimedOut(), NetworkError('connection failed')],
    ids=['reaction-succeeds', 'reaction-fails', 'reaction-times-out', 'reaction-network-error'],
)
@pytest.mark.asyncio
async def test_save_results_uses_only_reactions_for_acknowledgment(
    monkeypatch: pytest.MonkeyPatch,
    game: str,
    saved: bool,
    win_frame: int,
    reaction: ReactionTypeEmoji,
    reaction_error: TelegramError | None,
) -> None:
    if game == 'framed':
        title, icon, frame_count, pattern = 'Framed', '🎥', 6, main.framed_pattern
    else:
        title, icon, frame_count, pattern = 'Episode', '📺', 10, main.episode_pattern
    frames = ['🟥'] * (win_frame - 1) + ['🟩'] + ['⬛'] * (frame_count - win_frame)
    update = make_update(f'{title} #42\n{icon} {" ".join(frames)}\n\nhttps://{game}.wtf')
    test_context = make_context(monkeypatch, reaction_error=reaction_error)
    result_model = make_result_model(saved)
    update_from_user = AsyncMock()
    monkeypatch.setattr(BotUser, 'update_from_tg_user', update_from_user)

    await main.save_results(update, test_context.context, pattern, result_model)
    await finish_delivery(test_context)

    assert result_model.calls == [(99, 42, True, win_frame, 123)]
    expected_attempts = 3 if isinstance(reaction_error, NetworkError) else 1
    assert (
        test_context.bot.reaction_calls
        == [ReactionCall(chat_id=123, message_id=456, reaction=reaction)] * expected_attempts
    )
    assert test_context.bot.send_message_calls == []
    assert test_context.job_queue.calls == []


@pytest.mark.asyncio
async def test_new_framed_data_delegates_to_save_results(monkeypatch: pytest.MonkeyPatch) -> None:
    update = make_update()
    test_context = make_context(monkeypatch)
    save_results = AsyncMock()
    monkeypatch.setattr(main, 'save_results', save_results)

    await main.new_framed_data(update, test_context.context)

    save_results.assert_awaited_once_with(update, test_context.context, main.framed_pattern, FramedResult)


@pytest.mark.asyncio
async def test_new_episode_data_delegates_to_save_results(monkeypatch: pytest.MonkeyPatch) -> None:
    update = make_update('Episode #7\n📺 🟥 🟥 🟩 ⬛ ⬛ ⬛ ⬛ ⬛ ⬛ ⬛\n\nhttps://episode.wtf')
    test_context = make_context(monkeypatch)
    save_results = AsyncMock()
    monkeypatch.setattr(main, 'save_results', save_results)

    await main.new_episode_data(update, test_context.context)

    save_results.assert_awaited_once_with(update, test_context.context, main.episode_pattern, EpisodeResult)


@dataclass(slots=True)
class AnswerJobRecorder:
    calls: list[tuple[object, object, str | None]] = field(default_factory=list)


def enable_answers(
    monkeypatch: pytest.MonkeyPatch, test_context: RecordedContext, has_answer: AsyncMock
) -> AnswerJobRecorder:
    application = main.answer_application_for(test_context.context.application)
    application.answer_sync = AnswerSync(application.answer_store, AsyncMock(), None, AsyncMock())
    monkeypatch.setattr(DbAnswerStore, 'has_answer', has_answer)
    recorder = AnswerJobRecorder()

    def run_once(_job_queue: object, callback: object, when: object, data: object = None, name: str | None = None):
        recorder.calls.append((callback, data, name))

    monkeypatch.setattr(type(test_context.context.job_queue), 'run_once', run_once)
    return recorder


@pytest.mark.parametrize(
    ('text', 'pattern', 'game'),
    [
        ('Framed #42\n🎥 🟥 🟩 ⬛ ⬛ ⬛ ⬛\n\nhttps://framed.wtf', main.framed_pattern, 'framed'),
        ('Episode #42\n📺 🟥 🟩 ⬛ ⬛ ⬛ ⬛ ⬛ ⬛ ⬛ ⬛\n\nhttps://episode.wtf', main.episode_pattern, 'episode'),
    ],
    ids=['framed', 'episode'],
)
@pytest.mark.asyncio
async def test_new_round_schedules_answer_sync(
    monkeypatch: pytest.MonkeyPatch, text: str, pattern: str, game: Game
) -> None:
    test_context = make_context(monkeypatch)
    has_answer = AsyncMock(return_value=False)
    recorder = enable_answers(monkeypatch, test_context, has_answer)
    monkeypatch.setattr(BotUser, 'update_from_tg_user', AsyncMock())

    await main.save_results(make_update(text), test_context.context, pattern, make_result_model(True))
    await finish_delivery(test_context)

    has_answer.assert_awaited_once_with(game, 42)
    assert recorder.calls == [(main.sync_answers_job, main.AnswerSyncRequest(game, 42), f'answers-{game}')]
    assert test_context.bot.reaction_calls == [
        ReactionCall(chat_id=123, message_id=456, reaction=main.saved_result_reaction)
    ]


@pytest.mark.asyncio
async def test_known_round_does_not_schedule_answer_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    test_context = make_context(monkeypatch)
    recorder = enable_answers(monkeypatch, test_context, AsyncMock(return_value=True))
    monkeypatch.setattr(BotUser, 'update_from_tg_user', AsyncMock())

    await main.save_results(make_update(), test_context.context, main.framed_pattern, make_result_model(True))
    await finish_delivery(test_context)

    assert recorder.calls == []


@pytest.mark.asyncio
async def test_duplicate_result_does_not_check_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    test_context = make_context(monkeypatch)
    has_answer = AsyncMock(return_value=False)
    recorder = enable_answers(monkeypatch, test_context, has_answer)
    monkeypatch.setattr(BotUser, 'update_from_tg_user', AsyncMock())

    await main.save_results(make_update(), test_context.context, main.framed_pattern, make_result_model(False))
    await finish_delivery(test_context)

    has_answer.assert_not_awaited()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_answer_check_error_does_not_change_reaction(monkeypatch: pytest.MonkeyPatch) -> None:
    test_context = make_context(monkeypatch)
    recorder = enable_answers(monkeypatch, test_context, AsyncMock(side_effect=OSError('database is down')))
    monkeypatch.setattr(BotUser, 'update_from_tg_user', AsyncMock())
    result_model = make_result_model(True)

    await main.save_results(make_update(), test_context.context, main.framed_pattern, result_model)
    await finish_delivery(test_context)

    assert result_model.calls == [(99, 42, True, 2, 123)]
    assert recorder.calls == []
    assert test_context.bot.reaction_calls == [
        ReactionCall(chat_id=123, message_id=456, reaction=main.saved_result_reaction)
    ]
