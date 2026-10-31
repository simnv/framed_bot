# The request adapter records real PTB JSON serialization without network access.
import json
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import AIORateLimiter, ApplicationBuilder, ContextTypes, JobQueue
from telegram.request import BaseRequest
from telegram.warnings import PTBUserWarning

import main
from rich_tables import TopType, render_stats, render_top
from stats import Stats


class TableParser(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.cell_attributes = []
        self.links = []
        self.tags = []
        self.cell = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'a':
            self.links.append(dict(attrs))
        if tag == 'tr':
            self.rows.append([])
        if tag in ('th', 'td'):
            self.cell_attributes.append(dict(attrs))
            self.cell = ''

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in ('th', 'td'):
            self.rows[-1].append(self.cell)
            self.cell = None


@pytest.mark.parametrize(
    ('kind', 'label', 'value'),
    [
        (TopType.TOP_SCORE, 'Очки', '123'),
        (TopType.TOP_WIN, 'Фильмы', '123'),
        (TopType.TOP_ROUNDS, 'Участия', '123'),
        (TopType.TOP_FRAME, 'Средний кадр', '123.00'),
    ],
)
def test_top_native_table_preserves_order_and_escapes_names(kind, label, value):
    name = '👨‍👩‍👧‍👦 <b>A & "B"</b> @someone https://example.org'
    rich = render_top(
        kind,
        [
            SimpleNamespace(name=name, score=123, user_id=5_000_000_123),
            SimpleNamespace(name='李', score=None, user_id=99),
        ],
    )
    table = TableParser(rich['html'])
    assert table.rows == [['#', 'Имя', label], ['1', name, value], ['2', '李', '—']]
    assert (
        table.cell_attributes
        == [
            {'align': 'center'},
            {'align': 'left'},
            {'align': 'center'},
        ]
        * 3
    )
    assert table.links == [{'href': 'tg://user?id=5000000123'}, {'href': 'tg://user?id=99'}]
    assert table.tags.count('b') == 0
    assert '<table bordered striped compact>' in rich['html']
    assert rich['skip_entity_detection'] is True


def test_empty_top_and_missing_name_have_explicit_display():
    empty = render_top(TopType.TOP_SCORE, [])
    assert TableParser(empty['html']).rows == [['#', 'Имя', 'Очки']]
    assert 'Пока нет результатов' in empty['html']
    rendered = render_top(TopType.TOP_SCORE, [SimpleNamespace(name=None, score=0)])
    assert TableParser(rendered['html']).rows[-1] == ['1', '—', '0']
    assert TableParser(rendered['html']).links == []


@pytest.mark.parametrize('kind', list(TopType))
def test_average_frame_explanation_only_appears_for_frame_leaderboard(kind):
    rich = render_top(kind, [SimpleNamespace(name='User', score=3)])
    explanation = '<p>Неугаданный фильм = 7 кадров; меньше — лучше.</p>'
    assert (explanation in rich['html']) is (kind == TopType.TOP_FRAME)
    if kind == TopType.TOP_FRAME:
        assert rich['html'].index(explanation) > rich['html'].index('</table>')


def test_personal_stats_show_both_games_and_no_wins_without_fabricated_average():
    framed = SimpleNamespace(rounds_count=123, rounds_won_count=99, average_frame=1.23456)
    episode = SimpleNamespace(rounds_count=0, rounds_won_count=0, average_frame=None)
    rich = render_stats(framed, episode)
    assert TableParser(rich['html']).rows == [
        ['Игра', 'Раунды', 'Победы', 'Средний кадр'],
        ['framed.wtf', '123', '99', '1.23'],
        ['episode.wtf', '0', '0', '—'],
    ]
    assert 'Средний кадр — только по победам' in rich['html']


class TelegramRequest(BaseRequest):
    def __init__(self):
        self.calls = []

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(
        self,
        url,
        method,
        request_data=None,
        read_timeout=None,
        write_timeout=None,
        connect_timeout=None,
        pool_timeout=None,
    ):
        endpoint = url.rsplit('/', 1)[-1]
        parameters = request_data.json_parameters if request_data else {}
        self.calls.append((endpoint, parameters))
        if endpoint == 'getMe':
            result = {'id': 1, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot'}
        elif endpoint == 'getChat':
            result = {
                'id': int(parameters['chat_id']),
                'type': 'private',
                'username': 'fresh_owner',
                'accent_color_id': 1,
                'max_reaction_count': 10,
                'accepted_gift_types': {
                    'unlimited_gifts': True,
                    'limited_gifts': True,
                    'unique_gifts': True,
                    'premium_subscription': True,
                    'gifts_from_channels': False,
                },
            }
        elif endpoint in ('sendRichMessage', 'sendMessage'):
            result = {'message_id': 777, 'date': 1, 'chat': {'id': -123, 'type': 'group'}}
        else:
            result = True
        return 200, json.dumps({'ok': True, 'result': result}).encode()


def command_update(text):
    message = Message(456, datetime.now(UTC), Chat(-123, 'group'), from_user=User(99, 'Test', False), text=text)
    return Update(1, message=message)


def make_context(request, *, job_queue=True):
    builder = (
        ApplicationBuilder()
        .application_class(main.ReactionApplication)
        .token('123:synthetic-only')
        .request(request)
        .rate_limiter(AIORateLimiter())
    )
    if not job_queue:
        builder.job_queue(None)
    application = builder.build()
    assert isinstance(application, main.ReactionApplication)
    return ContextTypes.DEFAULT_TYPE(application)


@pytest.mark.asyncio
async def test_top_sends_native_table_with_reply_keyboard_and_rate_limiting(monkeypatch):
    request = TelegramRequest()
    context = make_context(request)
    results = [SimpleNamespace(name='A < B 👨‍👩‍👧‍👦', score=42, user_id=5_000_000_123)]
    query = AsyncMock(return_value=results)
    monkeypatch.setattr(main.FramedResult, 'top_score', query)
    limiter = context.bot.rate_limiter
    process = AsyncMock(wraps=limiter.process_request)
    monkeypatch.setattr(type(limiter), 'process_request', process)
    async with context.bot:
        await main.top(command_update('/top'), context)
    query.assert_awaited_once_with()
    endpoint, params = next(call for call in request.calls if call[0] == 'sendRichMessage')
    assert endpoint == 'sendRichMessage'
    expected = render_top(TopType.TOP_SCORE, results)
    expected['html'] = expected['html'].replace('tg://user?id=5000000123', 'https://t.me/fresh_owner?profile')
    assert json.loads(params['rich_message']) == expected
    assert TableParser(json.loads(params['rich_message'])['html']).links == [
        {'href': 'https://t.me/fresh_owner?profile'}
    ]
    assert json.loads(params['reply_parameters']) == {'message_id': 456}
    assert params['chat_id'] == '-123'
    assert 'text' not in params and 'entities' not in params and 'parse_mode' not in params
    buttons = json.loads(params['reply_markup'])['inline_keyboard'][0]
    assert {json.loads(button['callback_data'])['top'] for button in buttons} == {2, 3, 4}
    assert any(call.kwargs['endpoint'] == 'sendRichMessage' for call in process.await_args_list)
    assert any(call.kwargs['endpoint'] == 'getChat' for call in process.await_args_list)
    assert sum(endpoint == 'getChat' for endpoint, _params in request.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('kind', 'query_name'),
    [
        (TopType.TOP_SCORE, 'top_score'),
        (TopType.TOP_FRAME, 'top_average_frame'),
        (TopType.TOP_WIN, 'top_won'),
        (TopType.TOP_ROUNDS, 'top_rounds'),
    ],
)
async def test_sort_callback_edits_native_table_in_place_and_acknowledges(monkeypatch, kind, query_name):
    request = TelegramRequest()
    context = make_context(request)
    results = [SimpleNamespace(name='Имя & 李', score=2, user_id=5_000_000_123)]
    queries = {}
    for name in ('top_score', 'top_average_frame', 'top_won', 'top_rounds'):
        queries[name] = AsyncMock(return_value=results)
        monkeypatch.setattr(main.FramedResult, name, queries[name])
    command = command_update('/top')
    callback = CallbackQuery(
        'sort-query', command.effective_user, 'instance', message=command.message, data=json.dumps({'top': kind})
    )
    callback.set_bot(context.bot)
    async with context.bot:
        with pytest.warns(PTBUserWarning, match='editMessageText'):
            await main.inline_top(Update(2, callback_query=callback), context)
    for name, query in queries.items():
        if name == query_name:
            query.assert_awaited_once_with()
        else:
            query.assert_not_awaited()
    params = next(params for endpoint, params in request.calls if endpoint == 'editMessageText')
    assert params['message_id'] == '456' and params['chat_id'] == '-123'
    expected = render_top(kind, results)
    expected['html'] = expected['html'].replace('tg://user?id=5000000123', 'https://t.me/fresh_owner?profile')
    assert json.loads(params['rich_message']) == expected
    assert TableParser(json.loads(params['rich_message'])['html']).links == [
        {'href': 'https://t.me/fresh_owner?profile'}
    ]
    assert 'text' not in params and 'entities' not in params
    buttons = json.loads(params['reply_markup'])['inline_keyboard'][0]
    assert {json.loads(button['callback_data'])['top'] for button in buttons} == set(TopType) - {kind}
    assert [endpoint for endpoint, _ in request.calls][-1] == 'answerCallbackQuery'
    assert not any(endpoint == 'sendRichMessage' for endpoint, _ in request.calls)
    assert sum(endpoint == 'getChat' for endpoint, _params in request.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('has_job_queue', [True, False])
async def test_stats_uses_read_only_aggregate_and_preserves_thirty_second_deletion(monkeypatch, has_job_queue):
    request = TelegramRequest()
    context = make_context(request, job_queue=has_job_queue)
    aggregates = (Stats(9, 5, 2.4), Stats(4, 0, None))
    get_stats = AsyncMock(return_value=aggregates)
    monkeypatch.setattr(main, 'get_user_stats', get_stats)
    update_user = AsyncMock()
    monkeypatch.setattr(main.User, 'update_from_tg_user', update_user)
    schedule = Mock()
    monkeypatch.setattr(JobQueue, 'run_once', schedule)
    async with context.bot:
        if has_job_queue:
            await main.stats(command_update('/stats'), context)
        else:
            with pytest.warns(PTBUserWarning, match='No `JobQueue`'):
                await main.stats(command_update('/stats'), context)
    get_stats.assert_awaited_once_with(99)
    update_user.assert_not_awaited()
    params = next(params for endpoint, params in request.calls if endpoint == 'sendRichMessage')
    assert json.loads(params['rich_message']) == render_stats(*aggregates)
    assert json.loads(params['reply_parameters']) == {'message_id': 456}
    if has_job_queue:
        assert schedule.call_count == 2
        assert {call.args[2] for call in schedule.call_args_list} == {456, 777}
        for call in schedule.call_args_list:
            assert call.args[:2] == (main.delete_message_task, timedelta(seconds=30))
            assert call.kwargs == {'chat_id': -123}
    else:
        schedule.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_stats_user_keeps_existing_reply(monkeypatch):
    request = TelegramRequest()
    context = make_context(request)
    monkeypatch.setattr(main, 'get_user_stats', AsyncMock(return_value=None))
    schedule = Mock()
    monkeypatch.setattr(JobQueue, 'run_once', schedule)
    async with context.bot:
        await main.stats(command_update('/stats'), context)
    params = next(params for endpoint, params in request.calls if endpoint == 'sendMessage')
    assert params['text'] == 'Я тебя не знаю'
    assert json.loads(params['reply_parameters']) == {'message_id': 456}
    assert not any(endpoint == 'sendRichMessage' for endpoint, _ in request.calls)
    schedule.assert_not_called()


@pytest.mark.parametrize('user_id', [None, 0, -1, True, False, '123', '1" onclick="evil', 1.5])
def test_profile_link_invalid_ids_remain_escaped_plain_names(user_id):
    name = '<a href="https://evil.invalid/">name</a> & "quoted" 👩‍💻'
    rich = render_top(TopType.TOP_SCORE, [SimpleNamespace(name=name, score=1, user_id=user_id)])
    table = TableParser(rich['html'])
    assert table.rows[-1] == ['1', name, '1']
    assert table.links == []
    assert '<a ' not in rich['html']


def test_profile_link_escapes_name_instead_of_accepting_markup():
    name = '</a><script>evil()</script><a href="https://evil.invalid/">'
    rich = render_top(TopType.TOP_SCORE, [SimpleNamespace(name=name, score=1, user_id=42)])
    table = TableParser(rich['html'])
    assert table.rows[-1] == ['1', name, '1']
    assert table.links == [{'href': 'tg://user?id=42'}]
    assert 'script' not in table.tags
