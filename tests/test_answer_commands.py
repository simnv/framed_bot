import asyncio
import time
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ExtBot, Job, JobQueue

import main
from answers.pipeline import Resolver
from answers.ratelimit import RateLimiter
from answers.scraper import BundleEntry
from answers.store import DbAnswerStore
from answers.sync import AnswerSync
from answers.tmdb import TitleData, TmdbClient, TmdbError
from reaction_delivery import DeliverySettings, ReactionDelivery, ReactionIntent

# Framed round 1695: the startup clamp is 1696.
TODAY = date(2026, 10, 31)
NO_MISFIRE = {'misfire_grace_time': None}


def title_data() -> TitleData:
    return TitleData(424139, 'movie', 'Halloween', 'Halloween', 2018, 106, ['US'], None, [], [], [], {})


def build_application() -> main.ReactionApplication:
    application = ApplicationBuilder().application_class(main.ReactionApplication).token('123:ABC').build()
    assert isinstance(application, main.ReactionApplication)
    return application


def enable_tmdb(monkeypatch: pytest.MonkeyPatch, application: main.ReactionApplication, details: AsyncMock) -> None:
    # MockTransport fails every request; the test replaces details() anyway.
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    application.tmdb = TmdbClient('synthetic-tmdb-key', client)
    monkeypatch.setattr(TmdbClient, 'details', details)


def command_context(
    monkeypatch: pytest.MonkeyPatch, application: main.ReactionApplication, args: list[str]
) -> tuple[ContextTypes.DEFAULT_TYPE, list[str]]:
    sent: list[str] = []

    async def send_message(_bot: ExtBot[None], *, chat_id: int, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(ExtBot, 'send_message', send_message)
    context = ContextTypes.DEFAULT_TYPE(application)
    context.args = args
    return context, sent


def private_update(text: str, user_id: int = 1, chat_type: str = 'private') -> Update:
    message = Message(1, datetime.now(UTC), Chat(10, chat_type), from_user=User(user_id, 'Admin', False), text=text)
    return Update(1, message=message)


def find_handler(application: main.ReactionApplication, command: str) -> CommandHandler:
    return next(
        handler
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler) and command in handler.commands
    )


@pytest.mark.parametrize(
    ('chat_type', 'user_id', 'allowed'),
    [('private', 1, True), ('private', 2, False), ('group', 1, False)],
)
def test_set_title_route_requires_private_administrator(chat_type: str, user_id: int, allowed: bool) -> None:
    handler = find_handler(main.build_application(), 'set_title')
    update = private_update('/set_title framed 1 278', user_id, chat_type)
    assert bool(handler.filters.check_update(update)) is allowed


@pytest.mark.asyncio
async def test_help_shows_tmdb_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    application = build_application()
    context, sent = command_context(monkeypatch, application, [])
    assert find_handler(main.build_application(), 'help').callback is main.help_command

    await main.help_command(private_update('/help'), context)

    assert len(sent) == 1
    assert 'This product uses the TMDB API but is not endorsed or certified by TMDB.' in sent[0]


@pytest.mark.parametrize('args', [[], ['framed', '1'], ['movies', '1', '278'], ['framed', 'x', '278']])
@pytest.mark.asyncio
async def test_set_title_rejects_bad_arguments(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    application = build_application()
    enable_tmdb(monkeypatch, application, AsyncMock())
    context, sent = command_context(monkeypatch, application, args)

    await main.set_title(private_update('/set_title'), context)

    assert sent == [main.set_title_usage]


@pytest.mark.parametrize(
    'value',
    ['x', '²', '0', '-1', '2147483648', '9' * 5000],
    ids=['text', 'unicode-digit', 'zero', 'negative', 'overflow', 'huge'],
)
@pytest.mark.parametrize('argument', [1, 2], ids=['round', 'tmdb-id'])
@pytest.mark.asyncio
async def test_set_title_rejects_invalid_numbers(monkeypatch: pytest.MonkeyPatch, value: str, argument: int) -> None:
    application = build_application()
    details = AsyncMock(return_value=title_data())
    enable_tmdb(monkeypatch, application, details)
    save = AsyncMock(return_value=True)
    monkeypatch.setattr(DbAnswerStore, 'set_manual_title', save)
    args = ['framed', '1', '278']
    args[argument] = value
    context, sent = command_context(monkeypatch, application, args)

    await main.set_title(private_update('/set_title'), context)

    assert sent == [main.set_title_usage]
    details.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_title_needs_tmdb_key(monkeypatch: pytest.MonkeyPatch) -> None:
    application = build_application()
    context, sent = command_context(monkeypatch, application, ['framed', '1', '278'])

    await main.set_title(private_update('/set_title framed 1 278'), context)

    assert sent == ['TMDB_API_KEY не задан']


@pytest.mark.asyncio
async def test_post_stop_closes_http_client_when_reaction_drain_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    application = build_application()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    application.http_client = client
    monkeypatch.setattr(ReactionDelivery, 'stop', AsyncMock(side_effect=RuntimeError('drain failed')))

    try:
        with pytest.raises(RuntimeError, match='drain failed'):
            await main.stop_reaction_delivery(application)
        assert client.is_closed
        assert application.http_client is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_set_title_stores_manual_title(monkeypatch: pytest.MonkeyPatch) -> None:
    application = build_application()
    details = AsyncMock(return_value=title_data())
    enable_tmdb(monkeypatch, application, details)
    set_manual_title = AsyncMock(return_value=True)
    monkeypatch.setattr(DbAnswerStore, 'set_manual_title', set_manual_title)
    context, sent = command_context(monkeypatch, application, ['episode', '12', '424139'])

    await main.set_title(private_update('/set_title episode 12 424139'), context)

    details.assert_awaited_once_with('tv', 424139)
    set_manual_title.assert_awaited_once_with('episode', 12, title_data())
    assert sent == ['episode #12: Halloween (2018)']


@pytest.mark.asyncio
async def test_set_title_reports_missing_round(monkeypatch: pytest.MonkeyPatch) -> None:
    application = build_application()
    enable_tmdb(monkeypatch, application, AsyncMock(return_value=title_data()))
    monkeypatch.setattr(DbAnswerStore, 'set_manual_title', AsyncMock(return_value=False))
    context, sent = command_context(monkeypatch, application, ['framed', '9999', '424139'])

    await main.set_title(private_update('/set_title framed 9999 424139'), context)

    assert sent == ['Ответ framed #9999 ещё не сохранён']


@pytest.mark.asyncio
async def test_set_title_reports_tmdb_error(monkeypatch: pytest.MonkeyPatch) -> None:
    application = build_application()
    enable_tmdb(monkeypatch, application, AsyncMock(side_effect=TmdbError('HTTP 404')))
    set_manual_title = AsyncMock()
    monkeypatch.setattr(DbAnswerStore, 'set_manual_title', set_manual_title)
    context, sent = command_context(monkeypatch, application, ['framed', '1', '1'])

    await main.set_title(private_update('/set_title framed 1 1'), context)

    set_manual_title.assert_not_awaited()
    assert sent == ['TMDB не ответил, попробуй позже']


@pytest.mark.parametrize('key', [None, 'synthetic-tmdb-key'])
@pytest.mark.asyncio
async def test_start_answers_schedules_backfill_for_each_game(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, key: str | None
) -> None:
    monkeypatch.setattr(main, 'TMDB_API_KEY', key)
    application = build_application()
    scheduled: list[tuple[object, object, str | None, object]] = []

    def run_once(
        _job_queue: object,
        callback: object,
        when: object,
        data: object = None,
        name: str | None = None,
        job_kwargs: object = None,
    ):
        scheduled.append((callback, data, name, job_kwargs))

    monkeypatch.setattr(JobQueue, 'run_once', run_once)

    main.start_answers(application)

    assert scheduled == [
        (main.sync_answers_job, main.AnswerSyncRequest('framed', None), 'answers-framed', NO_MISFIRE),
        (main.sync_answers_job, main.AnswerSyncRequest('episode', None), 'answers-episode', NO_MISFIRE),
    ]
    assert isinstance(application.answer_sync, AnswerSync)
    assert (application.tmdb is None) is (key is None)
    assert ('TMDB_API_KEY is not set' in caplog.text) is (key is None)
    client = application.http_client
    assert isinstance(client, httpx.AsyncClient)
    await main.stop_answers(application)
    assert client.is_closed and application.http_client is None


@pytest.mark.parametrize(
    ('request_round', 'max_round', 'synced'),
    [(None, 7, [('framed', 7)]), (None, None, []), (3, 7, [('framed', 3)]), (None, 10**12, [('framed', 1696)])],
    ids=['newest', 'no-results', 'requested', 'huge-stored-round'],
)
@pytest.mark.asyncio
async def test_sync_job_runs_up_to_requested_or_newest_round(
    monkeypatch: pytest.MonkeyPatch, request_round: int | None, max_round: int | None, synced: list[tuple[str, int]]
) -> None:
    application = build_application()
    calls: list[tuple[str, int]] = []

    async def run(_sync: AnswerSync, game: str, limit: int) -> None:
        calls.append((game, limit))

    application.answer_sync = AnswerSync(application.answer_store, AsyncMock(), None, AsyncMock(), today=lambda: TODAY)
    monkeypatch.setattr(AnswerSync, '_run', run)
    monkeypatch.setattr(DbAnswerStore, 'max_result_round', AsyncMock(return_value=max_round))
    context = ContextTypes.DEFAULT_TYPE(application)
    context.job = Job(main.sync_answers_job, data=main.AnswerSyncRequest('framed', request_round))

    await main.sync_answers_job(context)

    assert calls == synced


@pytest.mark.asyncio
async def test_startup_jobs_run_when_the_job_queue_starts_late(monkeypatch: pytest.MonkeyPatch) -> None:
    # APScheduler drops a job that is more than misfire_grace_time (default 1 s) late.
    application = build_application()
    ran: list[object] = []
    both = asyncio.Event()

    async def record(context: ContextTypes.DEFAULT_TYPE) -> None:
        ran.append(context.job.data if context.job else None)
        if len(ran) == 2:
            both.set()

    monkeypatch.setattr(main, 'sync_answers_job', record)
    main.start_answers(application)
    job_queue = application.job_queue
    assert job_queue is not None
    await asyncio.sleep(1.2)
    await job_queue.start()
    try:
        await asyncio.wait_for(both.wait(), 2)
    finally:
        await job_queue.stop()
        await main.stop_answers(application)

    assert ran == [main.AnswerSyncRequest('framed', None), main.AnswerSyncRequest('episode', None)]


@pytest.mark.asyncio
async def test_stop_cancels_answer_jobs_and_still_drains_reactions(monkeypatch: pytest.MonkeyPatch) -> None:
    application = (
        ApplicationBuilder()
        .application_class(main.ReactionApplication)
        .token('123:synthetic-only')
        .updater(None)
        .build()
    )
    assert isinstance(application, main.ReactionApplication)
    application.reaction_delivery = ReactionDelivery(
        application.bot, DeliverySettings(chat_gap=0, retry_base=0), jitter=lambda: 0
    )
    reactions: list[int] = []

    async def initialize(fake_bot: ExtBot[None]) -> None:
        fake_bot._bot_user = User(1, 'Synthetic bot', True)

    async def shutdown(_fake_bot: ExtBot[None]) -> None:
        pass

    async def set_message_reaction(_bot: ExtBot[None], *, chat_id: int, message_id: int, **_kwargs: object) -> bool:
        await asyncio.sleep(0.05)
        reactions.append(message_id)
        return True

    monkeypatch.setattr(ExtBot, 'initialize', initialize)
    monkeypatch.setattr(ExtBot, 'shutdown', shutdown)
    monkeypatch.setattr(ExtBot, 'set_message_reaction', set_message_reaction)
    scraping = asyncio.Event()

    async def scrape_forever(_game: str) -> list[BundleEntry]:
        scraping.set()
        await asyncio.Event().wait()
        return []

    store = AsyncMock()
    store.missing_rounds.return_value = [1, 2]
    application.answer_sync = AnswerSync(store, scrape_forever, None, AsyncMock(), today=lambda: TODAY)
    await application.initialize()
    await application.start()
    job_queue = application.job_queue
    assert job_queue is not None
    # An active backfill and a queued sync of the same game.
    for request in (main.AnswerSyncRequest('framed', 5), main.AnswerSyncRequest('framed', 6)):
        job_queue.run_once(main.sync_answers_job, 0, data=request)
    await asyncio.wait_for(scraping.wait(), 2)
    await application.reaction_delivery.submit(ReactionIntent(-1, 77, '✍'))

    started = time.monotonic()
    await asyncio.wait_for(application.stop(), 3)
    await main.stop_reaction_delivery(application)
    await application.shutdown()

    assert time.monotonic() - started < 2
    assert reactions == [77]
    assert application.reaction_delivery.counts['succeeded'] == 1
    assert store.missing_rounds.await_count == 1


@pytest.mark.asyncio
async def test_stop_cancels_a_stalled_newest_round_lookup_and_still_drains_reactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = (
        ApplicationBuilder()
        .application_class(main.ReactionApplication)
        .token('123:synthetic-only')
        .updater(None)
        .build()
    )
    assert isinstance(application, main.ReactionApplication)
    application.reaction_delivery = ReactionDelivery(
        application.bot, DeliverySettings(chat_gap=0, retry_base=0), jitter=lambda: 0
    )
    reactions: list[int] = []

    async def initialize(fake_bot: ExtBot[None]) -> None:
        fake_bot._bot_user = User(1, 'Synthetic bot', True)

    async def shutdown(_fake_bot: ExtBot[None]) -> None:
        pass

    async def set_message_reaction(_bot: ExtBot[None], *, chat_id: int, message_id: int, **_kwargs: object) -> bool:
        await asyncio.sleep(0.05)
        reactions.append(message_id)
        return True

    monkeypatch.setattr(ExtBot, 'initialize', initialize)
    monkeypatch.setattr(ExtBot, 'shutdown', shutdown)
    monkeypatch.setattr(ExtBot, 'set_message_reaction', set_message_reaction)
    looking = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled_lookup(_store: DbAnswerStore, _game: str) -> int | None:
        looking.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return None

    monkeypatch.setattr(DbAnswerStore, 'max_result_round', stalled_lookup)
    application.answer_sync = AnswerSync(AsyncMock(), AsyncMock(), None, AsyncMock(), today=lambda: TODAY)
    await application.initialize()
    await application.start()
    job_queue = application.job_queue
    assert job_queue is not None
    job_queue.run_once(main.sync_answers_job, 0, data=main.AnswerSyncRequest('framed', None))
    await asyncio.wait_for(looking.wait(), 2)
    await application.reaction_delivery.submit(ReactionIntent(-1, 77, '✍'))

    started = time.monotonic()
    await asyncio.wait_for(application.stop(), 3)
    await main.stop_reaction_delivery(application)
    await application.shutdown()

    assert cancelled.is_set()
    assert time.monotonic() - started < 2
    assert reactions == [77]
    assert application.reaction_delivery.counts['succeeded'] == 1


@pytest.mark.parametrize('model_exists', [False, True])
def test_build_resolver_adds_the_visual_step_only_with_the_model_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, model_exists: bool
) -> None:
    model = tmp_path / 'vision_model.onnx'
    if model_exists:
        model.write_bytes(b'not loaded by the constructor')
    monkeypatch.setattr(main, 'CLIP_MODEL_PATH', str(model))
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    tmdb = TmdbClient('synthetic-tmdb-key', client)

    resolver = main.build_resolver(client, tmdb, main.default_limiters())

    assert isinstance(resolver, Resolver)
    assert (resolver._embedder is not None) is model_exists
    assert ('CLIP model file is missing' in caplog.text) is not model_exists
    assert 'synthetic-tmdb-key' not in caplog.text


class Grants(RateLimiter):
    """Counts waits; the transport checks that each request consumed one."""

    def __init__(self) -> None:
        super().__init__(1000)
        self.granted = 0
        self.used = 0

    async def wait(self) -> None:
        self.granted += 1


@pytest.mark.asyncio
async def test_every_answer_request_waits_for_its_host_group_limiter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixtures = Path(__file__).with_name('fixtures')
    model = tmp_path / 'vision_model.onnx'
    model.write_bytes(b'never loaded')
    monkeypatch.setattr(main, 'TMDB_API_KEY', 'synthetic-tmdb-key')
    monkeypatch.setattr(main, 'CLIP_MODEL_PATH', str(model))
    site, api, cdn = Grants(), Grants(), Grants()
    limiters = main.Limiters(site, api, cdn)
    groups = dict.fromkeys(('framed.wtf', 'episode.wtf', 'core.framed.wtf'), site)
    groups |= {'api.themoviedb.org': api, 'image.tmdb.org': cdn}
    routes = {
        'https://framed.wtf/': httpx.Response(200, text=(fixtures / 'framed_home.html').read_text()),
        'https://framed.wtf/_next/static/chunks/pages/_app-073c1f0998963a1e.js': httpx.Response(
            200, text=(fixtures / 'framed_app.js').read_text()
        ),
        'https://api.themoviedb.org/3/movie/7/images': httpx.Response(
            200, json={'backdrops': [{'file_path': '/a.jpg'}, {'file_path': '/b.jpg'}]}
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        limiter = groups[request.url.host]
        limiter.used += 1
        assert limiter.used <= limiter.granted, f'{request.url} did not wait for its limiter'
        return routes.get(str(request.url).split('?')[0], httpx.Response(404))

    application = build_application()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    main.start_answers(application, client, limiters)
    sync = application.answer_sync
    assert sync is not None
    resolver = sync._resolver
    assert isinstance(resolver, Resolver) and resolver._visual is not None

    await sync._scrape('framed')
    assert sync._assets is not None
    await sync._assets.fetch('titleshot', 1)
    await resolver._visual.frames('framed', 40)
    await resolver._visual.candidate_images('movie', 7)
    await main.stop_answers(application)

    assert [(limiter.used, limiter.granted) for limiter in (site, api, cdn)] == [
        (9, 9),
        (1, 1),
        (2, 2),
    ]
