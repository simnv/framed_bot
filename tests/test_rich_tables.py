# The request adapter records real PTB JSON serialization without network access.
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.error import BadRequest
from telegram.ext import AIORateLimiter, ApplicationBuilder, CallbackQueryHandler, ContextTypes, JobQueue
from telegram.request import BaseRequest
from telegram.warnings import PTBUserWarning

import main
import rich_tables
from insight import GameInsight, Section
from rich_tables import render_top
from stats import Stats
from top_type import TopType


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


@pytest.mark.parametrize('game', ['framed', 'episode'])
@pytest.mark.parametrize(
    ('kind', 'labels', 'value'),
    [
        (TopType.TOP_SCORE, ('Очки', 'Очки'), '123'),
        (TopType.TOP_WIN, ('Фильмы', 'Сериалы'), '123'),
        (TopType.TOP_ROUNDS, ('Участия', 'Участия'), '123'),
        (TopType.TOP_FRAME, ('Средний кадр', 'Средний кадр'), '123.00'),
    ],
)
def test_top_native_table_preserves_order_and_escapes_names(game, kind, labels, value):
    label = labels[game == 'episode']
    name = '👨‍👩‍👧‍👦 <b>A & "B"</b> @someone https://example.org'
    rich = render_top(
        game,
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
    empty = render_top('framed', TopType.TOP_SCORE, [])
    assert TableParser(empty['html']).rows == [['#', 'Имя', 'Очки']]
    assert 'Пока нет результатов' in empty['html']
    rendered = render_top('framed', TopType.TOP_SCORE, [SimpleNamespace(name=None, score=0)])
    assert TableParser(rendered['html']).rows[-1] == ['1', '—', '0']
    assert TableParser(rendered['html']).links == []


TOP_TEXTS = {
    ('framed', TopType.TOP_SCORE): (
        'Топ Framed по очкам',
        'Бот даёт очки за каждый раунд. Победа на кадре N даёт 7 \u2212 N очков. Кадр 1 даёт 6 очков, '
        'кадр 6 даёт 1. Проигрыш даёт 1 очко за участие. '
        'При равных очках выше стоит игрок, у которого больше раундов.',
    ),
    ('framed', TopType.TOP_FRAME): (
        'Топ Framed по среднему кадру',
        'Бот берёт средний кадр по всем раундам игрока. Неугаданный фильм считается как кадр 7. '
        'Чем меньше число, тем лучше. В таблицу попадают игроки, у которых не меньше 10 раундов. '
        'При равенстве выше стоит игрок, у которого больше раундов.',
    ),
    ('framed', TopType.TOP_WIN): (
        'Топ Framed по угаданным фильмам',
        'Число раундов, в которых игрок угадал фильм. Номер кадра не важен. '
        'При равенстве выше стоит игрок, у которого больше раундов.',
    ),
    ('framed', TopType.TOP_ROUNDS): (
        'Топ Framed по участиям',
        'Число раундов, которые игрок прислал боту. Бот считает каждый раунд один раз, повтор не учитывается. '
        'При равенстве выше стоит игрок, у которого больше побед.',
    ),
    ('episode', TopType.TOP_SCORE): (
        'Топ Episode по очкам',
        'Бот даёт очки за каждый раунд. Победа на кадре N даёт 11 \u2212 N очков. Кадр 1 даёт 10 очков, '
        'кадр 10 даёт 1. Проигрыш даёт 1 очко за участие. '
        'При равных очках выше стоит игрок, у которого больше раундов.',
    ),
    ('episode', TopType.TOP_FRAME): (
        'Топ Episode по среднему кадру',
        'Бот берёт средний кадр по всем раундам игрока. Неугаданный сериал считается как кадр 11. '
        'Чем меньше число, тем лучше. В таблицу попадают игроки, у которых не меньше 10 раундов. '
        'При равенстве выше стоит игрок, у которого больше раундов.',
    ),
    ('episode', TopType.TOP_WIN): (
        'Топ Episode по угаданным сериалам',
        'Число раундов, в которых игрок угадал сериал. Номер кадра не важен. '
        'При равенстве выше стоит игрок, у которого больше раундов.',
    ),
    ('episode', TopType.TOP_ROUNDS): (
        'Топ Episode по участиям',
        'Число раундов, которые игрок прислал боту. Бот считает каждый раунд один раз, повтор не учитывается. '
        'При равенстве выше стоит игрок, у которого больше побед.',
    ),
}


@pytest.mark.parametrize(('game', 'kind'), list(TOP_TEXTS))
@pytest.mark.parametrize('results', [[SimpleNamespace(name='User', score=3)], []])
def test_top_title_and_explanation_match_game_and_metric(game, kind, results):
    title, explanation = TOP_TEXTS[game, kind]
    html = render_top(game, kind, results)['html']
    assert html.startswith(f'<p>{title}</p><table ')
    assert html.count(f'<p>{explanation}</p>') == 1
    assert html.index(f'<p>{explanation}</p>') > html.index('</table>')
    assert html.count('<p>') == 2 + (not results)
    assert 'кадров; меньше' not in html


@pytest.mark.parametrize(
    ('frames', 'rounds', 'wins', 'win_frames', 'expected'),
    [
        # Framed: miss = 7. Wins on frames 1 and 3 give 6 + 4 points, 2 misses give 1 point each.
        (6, 4, 2, 4, Stats(4, 2, 2.0, 12, (4 + 2 * 7) / 4)),
        # Episode: miss = 11.
        (10, 3, 1, 2, Stats(3, 1, 2.0, 9 + 2, (2 + 2 * 11) / 3)),
        (10, 2, 0, 0, Stats(2, 0, None, 2, 11.0)),
        (6, 0, 0, 0, Stats(0, 0, None, 0, None)),
    ],
)
def test_stats_from_totals_derive_points_and_average_with_misses(frames, rounds, wins, win_frames, expected):
    assert Stats.from_totals(rounds, wins, win_frames, frames=frames) == expected


class TelegramRequest(BaseRequest):
    def __init__(self, *, private_lookup_fails=False, edit_error=None):
        self.calls = []
        self.private_lookup_fails = private_lookup_fails
        self.edit_error = edit_error

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
        if endpoint == 'editMessageText' and self.edit_error:
            return 400, json.dumps({'ok': False, 'error_code': 400, 'description': self.edit_error}).encode()
        if endpoint == 'getMe':
            result = {'id': 1, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot'}
        elif endpoint == 'getChat':
            if self.private_lookup_fails:
                return 400, json.dumps(
                    {'ok': False, 'error_code': 400, 'description': 'Bad Request: chat not found'}
                ).encode()
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
        elif endpoint == 'getChatMember':
            result = {
                'status': 'member',
                'user': {
                    'id': int(parameters['user_id']),
                    'is_bot': False,
                    'first_name': 'Imported',
                    'username': 'group_owner',
                },
            }
        elif endpoint in ('sendRichMessage', 'sendMessage'):
            result = {'message_id': 777, 'date': 1, 'chat': {'id': -123, 'type': 'group'}}
        else:
            result = True
        return 200, json.dumps({'ok': True, 'result': result}).encode()


def command_update(text, *, chat_type='group'):
    chat_id = 99 if chat_type == 'private' else -123
    message = Message(456, datetime.now(UTC), Chat(chat_id, chat_type), from_user=User(99, 'Test', False), text=text)
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


def keyboard_rows(reply_markup):
    return [[(button['text'], button['callback_data']) for button in row] for row in reply_markup['inline_keyboard']]


@pytest.mark.parametrize(
    ('game', 'kind', 'expected'),
    [
        (
            'framed',
            TopType.TOP_SCORE,
            [
                [
                    ('🎥 Очки', 'top:framed:1'),
                    ('Кадры', 'top:framed:2'),
                    ('Фильмы', 'top:framed:3'),
                    ('Участия', 'top:framed:4'),
                ],
                [('Жанры', 'top:framed:genres'), ('Десятилетия', 'top:framed:decades'), ('Сложные', 'top:framed:hard')],
                [('📺 Переключить на Episode', 'top:episode:1')],
            ],
        ),
        (
            'episode',
            TopType.TOP_WIN,
            [
                [
                    ('Очки', 'top:episode:1'),
                    ('Кадры', 'top:episode:2'),
                    ('📺 Сериалы', 'top:episode:3'),
                    ('Участия', 'top:episode:4'),
                ],
                [
                    ('Жанры', 'top:episode:genres'),
                    ('Десятилетия', 'top:episode:decades'),
                    ('Сложные', 'top:episode:hard'),
                ],
                [('🎥 Переключить на Framed', 'top:framed:3')],
            ],
        ),
    ],
)
def test_top_keyboard_marks_the_active_metric_with_the_game_emoji_and_keeps_the_metric_on_switch(game, kind, expected):
    assert keyboard_rows(main.top_reply_markup(game, kind).to_dict()) == expected


def test_top_callback_pattern_accepts_only_compact_format():
    pattern = re.compile(main.TOP_CALLBACK_PATTERN)
    for game in ('framed', 'episode'):
        for kind in TopType:
            assert pattern.fullmatch(main.top_callback_data(game, kind))
    for data in ('top:framed:0', 'top:framed:5', 'top:other:1', 'top:framed:1 ', 'xtop:framed:1', '{"top": 1}'):
        assert not pattern.search(data)
    legacy = re.compile(main.LEGACY_TOP_CALLBACK_PATTERN)
    assert legacy.search('{"top": 2}')
    assert not legacy.search('top:framed:2')


def test_application_registers_new_and_legacy_top_callbacks(monkeypatch):
    monkeypatch.setattr(main, 'BOT_TOKEN', '123:synthetic-only')
    application = main.build_application()
    handlers = {
        handler.callback: handler.pattern.pattern
        for group in application.handlers.values()
        for handler in group
        if isinstance(handler, CallbackQueryHandler) and isinstance(handler.pattern, re.Pattern)
    }
    assert handlers == {
        main.inline_top: main.TOP_CALLBACK_PATTERN,
        main.inline_top_screen: main.TOP_SCREEN_PATTERN,
        main.ignore_legacy_top: main.LEGACY_TOP_CALLBACK_PATTERN,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('chat_type', 'private_lookup_fails'),
    [('group', False), ('group', True), ('supergroup', True), ('private', True), ('channel', True)],
)
async def test_top_sends_native_table_with_reply_keyboard_and_rate_limiting(
    monkeypatch, chat_type, private_lookup_fails
):
    request = TelegramRequest(private_lookup_fails=private_lookup_fails)
    context = make_context(request)
    results = [SimpleNamespace(name='A < B 👨‍👩‍👧‍👦', score=42, user_id=5_000_000_123)]
    query = AsyncMock(return_value=results)
    monkeypatch.setattr(main, 'top_results', query)
    limiter = context.bot.rate_limiter
    process = AsyncMock(wraps=limiter.process_request)
    monkeypatch.setattr(type(limiter), 'process_request', process)
    async with context.bot:
        await main.top(command_update('/top', chat_type=chat_type), context)
    query.assert_awaited_once_with('framed', TopType.TOP_SCORE)
    endpoint, params = next(call for call in request.calls if call[0] == 'sendRichMessage')
    assert endpoint == 'sendRichMessage'
    uses_group_lookup = private_lookup_fails and chat_type in ('group', 'supergroup')
    profile_url = 'https://t.me/fresh_owner?profile'
    if private_lookup_fails:
        profile_url = 'https://t.me/group_owner?profile' if uses_group_lookup else 'tg://user?id=5000000123'
    expected = render_top('framed', TopType.TOP_SCORE, results)
    expected['html'] = expected['html'].replace('tg://user?id=5000000123', profile_url)
    assert json.loads(params['rich_message']) == expected
    assert TableParser(json.loads(params['rich_message'])['html']).links == [{'href': profile_url}]
    assert json.loads(params['reply_parameters']) == {'message_id': 456}
    assert params['chat_id'] == ('99' if chat_type == 'private' else '-123')
    assert 'text' not in params and 'entities' not in params and 'parse_mode' not in params
    assert json.loads(params['reply_markup']) == main.top_reply_markup('framed', TopType.TOP_SCORE).to_dict()
    assert any(call.kwargs['endpoint'] == 'sendRichMessage' for call in process.await_args_list)
    assert any(call.kwargs['endpoint'] == 'getChat' for call in process.await_args_list)
    assert sum(endpoint == 'getChat' for endpoint, _params in request.calls) == 1
    member_calls = [params for endpoint, params in request.calls if endpoint == 'getChatMember']
    assert len(member_calls) == int(uses_group_lookup)
    if uses_group_lookup:
        assert member_calls[0]['chat_id'] == '-123' and member_calls[0]['user_id'] == '5000000123'
        assert any(call.kwargs['endpoint'] == 'getChatMember' for call in process.await_args_list)


def top_callback(context, data, *, chat_type='group', shown=('framed', TopType.TOP_SCORE)):
    """A callback on a bot message that shows the leaderboard `shown`, or has no keyboard when it is None."""
    chat_id = 99 if chat_type == 'private' else -123
    message = Message(
        456,
        datetime.now(UTC),
        Chat(chat_id, chat_type),
        from_user=User(1, 'Bot', True),
        reply_markup=main.top_reply_markup(*shown) if shown else None,
    )
    callback = CallbackQuery('sort-query', User(99, 'Test', False), 'instance', message=message, data=data)
    callback.set_bot(context.bot)
    return Update(2, callback_query=callback)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('chat_type', 'private_lookup_fails'),
    [('group', False), ('group', True), ('supergroup', True), ('private', True), ('channel', True)],
)
@pytest.mark.parametrize(
    ('game', 'kind', 'shown'),
    [
        ('framed', TopType.TOP_FRAME, ('framed', TopType.TOP_SCORE)),
        ('framed', TopType.TOP_WIN, ('framed', TopType.TOP_SCORE)),
        ('framed', TopType.TOP_ROUNDS, ('framed', TopType.TOP_SCORE)),
        ('episode', TopType.TOP_SCORE, ('framed', TopType.TOP_SCORE)),
        ('framed', TopType.TOP_SCORE, ('episode', TopType.TOP_SCORE)),
        ('episode', TopType.TOP_FRAME, ('episode', TopType.TOP_WIN)),
        ('episode', TopType.TOP_ROUNDS, None),
    ],
)
async def test_sort_callback_edits_native_table_in_place_and_acknowledges(
    monkeypatch, game, kind, shown, chat_type, private_lookup_fails
):
    request = TelegramRequest(private_lookup_fails=private_lookup_fails)
    context = make_context(request)
    results = [SimpleNamespace(name='Имя & 李', score=2, user_id=5_000_000_123)]
    query = AsyncMock(return_value=results)
    monkeypatch.setattr(main, 'top_results', query)
    update = top_callback(context, f'top:{game}:{kind.value}', chat_type=chat_type, shown=shown)
    async with context.bot:
        with pytest.warns(PTBUserWarning, match='editMessageText'):
            await main.inline_top(update, context)
    query.assert_awaited_once_with(game, kind)
    params = next(params for endpoint, params in request.calls if endpoint == 'editMessageText')
    assert params['message_id'] == '456'
    assert params['chat_id'] == ('99' if chat_type == 'private' else '-123')
    uses_group_lookup = private_lookup_fails and chat_type in ('group', 'supergroup')
    profile_url = 'https://t.me/fresh_owner?profile'
    if private_lookup_fails:
        profile_url = 'https://t.me/group_owner?profile' if uses_group_lookup else 'tg://user?id=5000000123'
    expected = render_top(game, kind, results)
    expected['html'] = expected['html'].replace('tg://user?id=5000000123', profile_url)
    assert json.loads(params['rich_message']) == expected
    assert TableParser(json.loads(params['rich_message'])['html']).links == [{'href': profile_url}]
    assert 'text' not in params and 'entities' not in params
    assert json.loads(params['reply_markup']) == main.top_reply_markup(game, kind).to_dict()
    assert [endpoint for endpoint, _ in request.calls][-1] == 'answerCallbackQuery'
    assert not any(endpoint == 'sendRichMessage' for endpoint, _ in request.calls)
    assert sum(endpoint == 'getChat' for endpoint, _params in request.calls) == 1
    member_calls = [params for endpoint, params in request.calls if endpoint == 'getChatMember']
    assert len(member_calls) == int(uses_group_lookup)
    if uses_group_lookup:
        assert member_calls[0]['chat_id'] == '-123' and member_calls[0]['user_id'] == '5000000123'


@pytest.mark.asyncio
@pytest.mark.parametrize('shown', [('framed', TopType.TOP_SCORE), ('episode', TopType.TOP_FRAME)])
async def test_tap_on_active_button_only_acknowledges(monkeypatch, shown):
    request = TelegramRequest()
    context = make_context(request)
    query = AsyncMock()
    monkeypatch.setattr(main, 'top_results', query)
    update = top_callback(context, main.top_callback_data(*shown), shown=shown)
    async with context.bot:
        await main.inline_top(update, context)
    query.assert_not_awaited()
    assert [endpoint for endpoint, _ in request.calls if endpoint != 'getMe'] == ['answerCallbackQuery']


def test_top_type_lives_in_neutral_module_only():
    assert TopType.__module__ == 'top_type'
    assert rich_tables.TopType is TopType


@pytest.mark.parametrize(
    ('rows', 'expected'),
    [
        (
            [
                [('Очки', 'top:framed:1'), ('🎥 Кадры', 'top:framed:2')],
                [('📺 Переключить на Episode', 'top:episode:2')],
            ],
            ('framed', TopType.TOP_FRAME),
        ),
        (
            [[('📺 Сериалы', 'top:episode:3')], [('🎥 Переключить на Framed', 'top:framed:3')]],
            ('episode', TopType.TOP_WIN),
        ),
        ([[('📺 Кадры', 'top:framed:2')]], None),
        ([[('🎥 Очки', 'top:framed:1'), ('🎥 Кадры', 'top:framed:2')]], None),
        ([[('🎥 Кадры', 'top:other:2')]], None),
        ([[('• Очки', 'top:framed:1'), ('Кадры', 'top:framed:2')]], None),
    ],
    ids=['framed', 'episode', 'emoji-of-other-game', 'two-active', 'unparsable', 'no-active'],
)
def test_shown_top_reads_the_one_metric_marked_with_its_game_emoji(rows, expected):
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for text, data in row] for row in rows]
    )
    message = Message(456, datetime.now(UTC), Chat(-123, 'group'), reply_markup=markup)
    assert main.shown_top(message) == expected


@pytest.mark.asyncio
async def test_unmodified_message_error_is_ignored_and_callback_answered(monkeypatch):
    request = TelegramRequest(edit_error='Bad Request: message is not modified')
    context = make_context(request)
    monkeypatch.setattr(main, 'top_results', AsyncMock(return_value=[]))
    update = top_callback(context, 'top:episode:1')
    async with context.bot:
        with pytest.warns(PTBUserWarning, match='editMessageText'):
            await main.inline_top(update, context)
    endpoints = [endpoint for endpoint, _ in request.calls]
    assert endpoints[-2:] == ['editMessageText', 'answerCallbackQuery']


@pytest.mark.asyncio
async def test_other_edit_error_propagates_after_callback_answer(monkeypatch):
    request = TelegramRequest(edit_error='Bad Request: message to edit not found')
    context = make_context(request)
    monkeypatch.setattr(main, 'top_results', AsyncMock(return_value=[]))
    update = top_callback(context, 'top:episode:1')
    async with context.bot:
        with pytest.warns(PTBUserWarning, match='editMessageText'), pytest.raises(BadRequest, match='not found'):
            await main.inline_top(update, context)
    endpoints = [endpoint for endpoint, _ in request.calls]
    assert endpoints[-2:] == ['editMessageText', 'answerCallbackQuery']


@pytest.mark.asyncio
async def test_legacy_top_button_only_acknowledges(monkeypatch):
    request = TelegramRequest()
    context = make_context(request)
    query = AsyncMock()
    monkeypatch.setattr(main, 'top_results', query)
    update = top_callback(context, json.dumps({'top': 2}), shown=None)
    async with context.bot:
        await main.ignore_legacy_top(update, context)
    query.assert_not_awaited()
    assert [endpoint for endpoint, _ in request.calls if endpoint != 'getMe'] == ['answerCallbackQuery']


@pytest.mark.asyncio
async def test_stats_sends_one_kept_message_per_played_game_that_names_its_owner(monkeypatch):
    request = TelegramRequest()
    context = make_context(request)
    aggregates = (Stats(9, 5, 2.4, 29, 4.0), Stats(4, 0, None, 4, 11.0))
    get_stats = AsyncMock(return_value=aggregates)
    monkeypatch.setattr(main, 'get_user_stats', get_stats)
    empty = GameInsight(Section(()), Section(()), Section(()), 0, 0, ())
    get_insight = AsyncMock(return_value=empty)
    monkeypatch.setattr(main, 'game_insight', get_insight)
    update_user = AsyncMock()
    monkeypatch.setattr(main.User, 'update_from_tg_user', update_user)
    schedule = Mock()
    monkeypatch.setattr(JobQueue, 'run_once', schedule)
    async with context.bot:
        await main.stats(command_update('/stats'), context)
    get_stats.assert_awaited_once_with(99)
    assert [call.args[:2] for call in get_insight.await_args_list] == [(99, 'framed'), (99, 'episode')]
    update_user.assert_not_awaited()
    sent = [params for endpoint, params in request.calls if endpoint == 'sendRichMessage']
    headers = [json.loads(params['rich_message'])['html'].split('</h3>', 1)[0] for params in sent]
    # Each header names the game with its emoji and links the owner to the profile, like a name in /top.
    assert headers[0].startswith('<h3>🎥 <a href="https://framed.wtf/">Framed</a> · ')
    assert headers[1].startswith('<h3>📺 <a href="https://episode.wtf/">Episode</a> · ')
    assert all(header.endswith('?profile">Test</a>') for header in headers)
    assert [json.loads(params['reply_parameters']) for params in sent] == [{'message_id': 456}] * 2
    # The messages stay: the bot does not delete the command or the replies.
    schedule.assert_not_called()
    assert not any(endpoint == 'deleteMessage' for endpoint, _ in request.calls)


@pytest.mark.asyncio
async def test_stats_skip_a_game_without_rounds_and_say_so_when_there_are_none(monkeypatch):
    request = TelegramRequest()
    context = make_context(request)
    zero = Stats(0, 0, None, 0, None)
    monkeypatch.setattr(main, 'get_user_stats', AsyncMock(return_value=(zero, Stats(4, 0, None, 4, 11.0))))
    monkeypatch.setattr(main, 'game_insight', AsyncMock(return_value=None))
    async with context.bot:
        await main.stats(command_update('/stats'), context)
    sent = [
        json.loads(params['rich_message'])['html']
        for endpoint, params in request.calls
        if endpoint == 'sendRichMessage'
    ]
    assert len(sent) == 1 and sent[0].startswith('<h3>📺')
    request.calls.clear()
    monkeypatch.setattr(main, 'get_user_stats', AsyncMock(return_value=(zero, zero)))
    async with context.bot:
        await main.stats(command_update('/stats'), context)
    texts = [params['text'] for endpoint, params in request.calls if endpoint == 'sendMessage']
    assert texts == ['У тебя пока нет результатов']
    assert not any(endpoint == 'sendRichMessage' for endpoint, _ in request.calls)


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
    rich = render_top('framed', TopType.TOP_SCORE, [SimpleNamespace(name=name, score=1, user_id=user_id)])
    table = TableParser(rich['html'])
    assert table.rows[-1] == ['1', name, '1']
    assert table.links == []
    assert '<a ' not in rich['html']


def test_profile_link_escapes_name_instead_of_accepting_markup():
    name = '</a><script>evil()</script><a href="https://evil.invalid/">'
    rich = render_top('framed', TopType.TOP_SCORE, [SimpleNamespace(name=name, score=1, user_id=42)])
    table = TableParser(rich['html'])
    assert table.rows[-1] == ['1', name, '1']
    assert table.links == [{'href': 'tg://user?id=42'}]
    assert 'script' not in table.tags
