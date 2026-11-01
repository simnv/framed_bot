"""Telegram native rich tables, independent of configuration and database access.

HTML tags and attributes follow https://core.telegram.org/bots/api#rich-messages.
Use the public do_api_request bridge until PTB exposes sendRichMessage directly.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape, unescape
from typing import Literal, Protocol, TypedDict, TypeGuard

from telegram import Bot, InlineKeyboardMarkup, Message, ReplyParameters
from telegram.error import TelegramError

from answers import FRAMES, Game
from insight_types import (
    MIN_LEAGUE_PLAYERS,
    MIN_LEAGUE_ROUNDS,
    MIN_STATS_BUCKET,
    MIN_STATS_PERSON,
    MIN_TITLE_PLAYERS,
    SMOOTHING,
    Bucket,
    Choice,
    Dimension,
    GameInsight,
    HardTitles,
    League,
    Rank,
    Section,
)
from top_type import TopType

_PROFILE_LOOKUP_DEADLINE = 8
# Telegram limits a rich message to 32768 characters of text and 500 blocks. render_game_stats drops
# sections to fit.
STATS_TEXT_LIMIT = 32768
STATS_BLOCK_LIMIT = 500


class TopResult(Protocol):
    @property
    def user_id(self) -> int | None: ...

    @property
    def name(self) -> str | None: ...

    @property
    def score(self) -> int | float | None: ...


class GameStats(Protocol):
    @property
    def rounds_count(self) -> int: ...

    @property
    def rounds_won_count(self) -> int: ...

    @property
    def average_frame(self) -> float | None: ...

    @property
    def points(self) -> int: ...

    @property
    def average_frame_all(self) -> float | None: ...


class RichMessage(TypedDict):
    html: str
    skip_entity_detection: bool


@dataclass(frozen=True, slots=True)
class _ResolvedTopResult:
    user_id: int | None
    name: str | None
    score: int | float | None
    public_username: str | None


def _valid_username(username: object) -> TypeGuard[str]:
    return isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9_]{1,32}', username) is not None


async def enrich_top_profiles(
    bot: Bot, results: Sequence[TopResult], *, group_chat_id: int | None = None
) -> tuple[_ResolvedTopResult, ...]:
    """Resolve fresh, identity-checked public usernames for at most ten leaderboard rows.

    If a private chat lookup fails, try a fresh member lookup in the current group.
    No stored username is trusted or cached. The eight-second batch deadline
    includes both lookup methods and SDK rate limiter waits. Unfinished lookups
    fall back to ID mentions. Original row order, names and scores are preserved.
    """
    user_ids: dict[int, None] = {}
    for result in results[:10]:
        user_id = getattr(result, 'user_id', None)
        if type(user_id) is int and user_id > 0:
            user_ids[user_id] = None
    usernames: dict[int, str] = {}
    semaphore = asyncio.Semaphore(3)

    async def resolve(user_id: int) -> None:
        async with semaphore:
            try:
                chat = await bot.get_chat(
                    user_id,
                    read_timeout=2,
                    write_timeout=2,
                    connect_timeout=2,
                    pool_timeout=1,
                )
            except TelegramError, TypeError, ValueError, AttributeError:
                chat = None
            username = getattr(chat, 'username', None)
            chat_id = getattr(chat, 'id', None)
            if (
                type(chat_id) is int
                and chat_id == user_id
                and getattr(chat, 'type', None) == 'private'
                and _valid_username(username)
            ):
                usernames[user_id] = username
                return
            if type(group_chat_id) is not int or group_chat_id >= 0:
                return
            try:
                member = await bot.get_chat_member(
                    group_chat_id,
                    user_id,
                    read_timeout=2,
                    write_timeout=2,
                    connect_timeout=2,
                    pool_timeout=1,
                )
            except TelegramError, TypeError, ValueError, AttributeError:
                return
            user = getattr(member, 'user', None)
            username = getattr(user, 'username', None)
            member_user_id = getattr(user, 'id', None)
            if (
                type(member_user_id) is int
                and member_user_id == user_id
                and getattr(user, 'is_bot', None) is False
                and _valid_username(username)
            ):
                usernames[user_id] = username

    tasks = [asyncio.create_task(resolve(user_id)) for user_id in user_ids]
    try:
        if tasks:
            async with asyncio.timeout(_PROFILE_LOOKUP_DEADLINE):
                await asyncio.gather(*tasks)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    resolved = []
    for result in results:
        user_id = getattr(result, 'user_id', None)
        resolved.append(
            _ResolvedTopResult(
                user_id=user_id,
                name=result.name,
                score=result.score,
                public_username=usernames.get(user_id) if type(user_id) is int and user_id > 0 else None,
            )
        )
    return tuple(resolved)


@dataclass(frozen=True, slots=True)
class _ProfileName:
    name: str | None
    user_id: int | None
    public_username: str | None = None


@dataclass(frozen=True, slots=True)
class _Link:
    text: str
    url: str


def _cell(value: str | int | float | _ProfileName | _Link | None) -> str:
    if isinstance(value, _Link):
        return f'<a href="{escape(value.url, quote=True)}">{_cell(value.text)}</a>'
    if isinstance(value, _ProfileName):
        name = _cell(value.name)
        # bool is an int subclass, but is not a Telegram user identifier.
        if type(value.user_id) is int and value.user_id > 0:
            if _valid_username(value.public_username):
                return f'<a href="https://t.me/{value.public_username}?profile">{name}</a>'
            return f'<a href="tg://user?id={value.user_id}">{name}</a>'
        return name
    return escape(str(value) if value is not None and value != '' else '—')


def _average(value: float | None) -> str:
    return f'{value:.2f}' if value is not None else '—'


def _table(
    headers: Sequence[str | _Link],
    rows: Sequence[Sequence[str | int | float | _ProfileName | _Link | None]],
    *,
    alignments: Sequence[Literal['left', 'center', 'right']] = (),
) -> str:
    attributes = tuple(f' align="{alignment}"' for alignment in alignments) if alignments else ('',) * len(headers)
    html = '<table bordered striped compact><tr>'
    html += (
        ''.join(f'<th{attribute}>{_cell(header)}</th>' for header, attribute in zip(headers, attributes, strict=True))
        + '</tr>'
    )
    for row in rows:
        html += (
            '<tr>'
            + ''.join(f'<td{attribute}>{_cell(value)}</td>' for value, attribute in zip(row, attributes, strict=True))
            + '</tr>'
        )
    return html + '</table>'


def _profile_name(result: TopResult) -> _ProfileName:
    return _ProfileName(
        result.name,
        getattr(result, 'user_id', None),
        result.public_username if isinstance(result, _ResolvedTopResult) else None,
    )


_GAME_TITLES: dict[Game, str] = {'framed': 'Framed', 'episode': 'Episode'}
# Genitive plural and nominative singular of the answer noun of each game.
_ANSWER_WORDS: dict[Game, tuple[str, str]] = {'framed': ('фильмам', 'фильм'), 'episode': ('сериалам', 'сериал')}
TOP_WIN_LABELS: dict[Game, str] = {'framed': 'Фильмы', 'episode': 'Сериалы'}


def _top_explanation(game: Game, top_type: TopType) -> str:
    frames = FRAMES[game]
    miss = frames + 1
    answer = _ANSWER_WORDS[game][1]
    more_rounds = 'При равенстве выше стоит игрок, у которого больше раундов.'
    match top_type:
        case TopType.TOP_SCORE:
            return (
                f'Бот даёт очки за каждый раунд. Победа на кадре N даёт {miss} \u2212 N очков. '
                f'Кадр 1 даёт {frames} очков, кадр {frames} даёт 1. Проигрыш даёт 1 очко за участие. '
                'При равных очках выше стоит игрок, у которого больше раундов.'
            )
        case TopType.TOP_FRAME:
            return (
                'Бот берёт средний кадр по всем раундам игрока. '
                f'Неугаданный {answer} считается как кадр {miss}. Чем меньше число, тем лучше. '
                'В таблицу попадают игроки, у которых не меньше 10 раундов. ' + more_rounds
            )
        case TopType.TOP_WIN:
            return f'Число раундов, в которых игрок угадал {answer}. Номер кадра не важен. ' + more_rounds
        case TopType.TOP_ROUNDS:
            return (
                'Число раундов, которые игрок прислал боту. Бот считает каждый раунд один раз, повтор не учитывается. '
                'При равенстве выше стоит игрок, у которого больше побед.'
            )


def render_top(game: Game, top_type: TopType, results: Sequence[TopResult]) -> RichMessage:
    """Render already ranked query results without changing their order or values."""
    title, column = {
        TopType.TOP_SCORE: ('по очкам', 'Очки'),
        TopType.TOP_FRAME: ('по среднему кадру', 'Средний кадр'),
        TopType.TOP_WIN: (f'по угаданным {_ANSWER_WORDS[game][0]}', TOP_WIN_LABELS[game]),
        TopType.TOP_ROUNDS: ('по участиям', 'Участия'),
    }[top_type]
    rows = [
        (rank, _profile_name(result), _average(result.score) if top_type == TopType.TOP_FRAME else result.score)
        for rank, result in enumerate(results, 1)
    ]
    html = f'<p>Топ {_GAME_TITLES[game]} {title}</p>'
    html += _table(('#', 'Имя', column), rows, alignments=('center', 'left', 'center'))
    if not results:
        html += '<p>Пока нет результатов.</p>'
    html += f'<p>{_top_explanation(game, top_type)}</p>'
    return {'html': html, 'skip_entity_detection': True}


def _rounds(value: float) -> str:
    """Weighted round counts get one decimal only when they are fractional."""
    rounded = round(value, 1)
    return str(int(rounded)) if rounded.is_integer() else f'{rounded:.1f}'


def _percent(value: float) -> str:
    return f'{value:.0%}'


def _strength(value: float) -> str:
    """A signed strength with a real minus sign. A value that rounds to zero has no sign."""
    text = f'{value:+.2f}'
    if text in ('+0.00', '-0.00'):
        return '0.00'
    return text.replace('-', '\u2212')


_GAME_EMOJI: dict[Game, str] = {'framed': '🎥', 'episode': '📺'}
_GAME_SITES: dict[Game, str] = {'framed': 'https://framed.wtf/', 'episode': 'https://episode.wtf/'}
_ROLE_LABELS = {'director': 'режиссёр', 'creator': 'создатель', 'cast': 'актёр'}
_BOARD_LABELS = {'score': 'очки', 'frame': 'кадры', 'rounds': 'участия'}
_LEAGUE_ALIGNMENTS: tuple[Literal['left', 'center'], ...] = ('center', 'left', 'center', 'center', 'center')
_LEAGUE_COLUMNS: tuple[Literal['left', 'center'], ...] = (*_LEAGUE_ALIGNMENTS, 'center')


def _numbers_line(stats: GameStats) -> str:
    share = stats.rounds_won_count / stats.rounds_count if stats.rounds_count else 0.0
    return (
        f'Раунды {stats.rounds_count} · Победы {stats.rounds_won_count} ({_percent(share)}) · Очки {stats.points} · '
        f'Ср. кадр {_average(stats.average_frame)} в победах, {_average(stats.average_frame_all)} во всех раундах'
    )


def _rank_label(game: Game, rank: Rank) -> str:
    match rank.board:
        case 'win':
            return TOP_WIN_LABELS[game].lower()
        case 'genre' | 'decade':
            return rank.label or ''
        case board:
            return _BOARD_LABELS[board]


def _verdict(buckets: Sequence[Bucket]) -> str:
    """Name the 2 strongest and the 2 weakest genres; with 2 or 3 genres only 1 of each."""
    if len(buckets) < 2:
        return ''
    count = 2 if len(buckets) >= 4 else 1
    ordered = sorted(buckets, key=lambda bucket: (-bucket.strength, bucket.label))
    strongest = ', '.join(escape(bucket.label) for bucket in ordered[:count])
    weakest = ', '.join(escape(bucket.label) for bucket in reversed(ordered[-count:]))
    return f'<p>Сильнее всего: {strongest}. Слабее всего: {weakest}.</p>'


def _bucket_cells(bucket: Bucket) -> tuple[str, ...]:
    return (_rounds(bucket.rounds), _percent(bucket.win_rate), _strength(bucket.strength))


def _genres_html(section: Section) -> str:
    if not section.best:
        return ''
    rows = [(bucket.label, *_bucket_cells(bucket)) for bucket in section.best]
    html = '<h4>Жанры</h4>' + _table(('Жанр', 'Раунды', 'Победы', 'Сила'), rows)
    return html + _verdict(section.best)


def _decades_html(section: Section) -> str:
    if not section.best:
        return ''
    rows = [(bucket.label, *_bucket_cells(bucket)) for bucket in section.best]
    return '<h4>Десятилетия</h4>' + _table(('Десятилетие', 'Раунды', 'Победы', 'Сила'), rows)


def _people_html(section: Section) -> str:
    if not section.best:
        return ''

    def row(bucket: Bucket) -> tuple[str, ...]:
        return (bucket.label, _ROLE_LABELS.get(bucket.role or '', '—'), *_bucket_cells(bucket))

    rows = [row(bucket) for bucket in section.best]
    if section.worst:
        rows.append(('…',) * 5)
        rows.extend(row(bucket) for bucket in section.worst)
    return '<h4>Люди</h4>' + _table(('Кто', 'Роль', 'Раунды', 'Победы', 'Сила'), rows)


def _stats_note(game: Game) -> str:
    miss = FRAMES[game] + 1
    return (
        '<details><summary>Как считаются числа</summary>'
        f'<p>Очки — как в /top: победа на кадре N даёт {miss} \u2212 N очков, промах даёт 1. '
        f'«Во всех раундах» средний кадр считает промах как кадр {miss}. Прочерк значит, что побед пока нет.</p>'
        '<p>Сила — насколько ты играешь лучше (+) или хуже (\u2212) своего обычного уровня, в очках за раунд. '
        f'К твоим раундам в жанре бот добавляет {SMOOTHING} раундов с твоими средними очками. '
        'Поэтому 2 удачных раунда не делают жанр лучшим, а много раундов показывают настоящий уровень. '
        'Пример: в среднем у тебя 3.5 очка за раунд, 2 раунда в жанре дали по 6 очков. '
        f'Тогда (12 + {SMOOTHING} × 3.5) / (2 + {SMOOTHING}) = 3.92, и сила равна +0.42.</p>'
        f'<p>Если у {_ANSWER_WORDS[game][1]}а несколько жанров, раунд делится между ними поровну, '
        f'поэтому раунды бывают дробными. В таблицах есть жанры и десятилетия, где у тебя не меньше '
        f'{MIN_STATS_BUCKET} раундов, и люди, где не меньше {MIN_STATS_PERSON}.</p>'
        '</details>'
    )


def _text_length(html: str) -> int:
    return len(unescape(re.sub(r'<[^>]+>', '', html)))


def block_count(html: str) -> int:
    """An upper estimate of the Telegram blocks: every paragraph, heading, table, row and details block."""
    return len(re.findall(r'<(?:p|h[1-6]|table|tr|details|summary|footer)[ >]', html))


def _fits(html: str) -> bool:
    return _text_length(html) <= STATS_TEXT_LIMIT and block_count(html) <= STATS_BLOCK_LIMIT


def render_game_stats(
    game: Game, stats: GameStats, insight: GameInsight | None = None, *, owner: TopResult | None = None
) -> RichMessage:
    """One /stats message for one game: the numbers, the streak, the ranks, then genres, decades and people.

    If the message does not fit the Telegram limits, the people go first, then all the insight sections.
    """
    header = f'{_GAME_EMOJI[game]} {_cell(_Link(_GAME_TITLES[game], _GAME_SITES[game]))}'
    if owner is not None:
        header += f' · {_cell(_profile_name(owner))}'
    head = f'<h3>{header}</h3><p>{_numbers_line(stats)}</p>'
    if insight is not None and insight.best_streak > 0:
        head += f'<p>Серия побед: сейчас {insight.current_streak}, лучшая {insight.best_streak}</p>'
    if insight is not None and insight.ranks:
        places = ' · '.join(f'{escape(_rank_label(game, rank))} {rank.place}/{rank.total}' for rank in insight.ranks)
        head += f'<p>Места в /top: {places}</p>'
    note = _stats_note(game)
    sections = ''
    if insight is not None:
        with_people = _genres_html(insight.genres) + _decades_html(insight.decades) + _people_html(insight.people)
        without_people = _genres_html(insight.genres) + _decades_html(insight.decades)
        sections = next((html for html in (with_people, without_people) if _fits(head + html + note)), '')
    return {'html': head + sections + note, 'skip_entity_detection': True}


_DIMENSION_WORDS: dict[Dimension, tuple[str, str, str, str]] = {
    # dative plural, accusative singular, prepositional singular, genitive singular
    'genre': ('жанрам', 'жанр', 'жанре', 'жанра'),
    'decade': ('десятилетиям', 'десятилетие', 'десятилетии', 'десятилетия'),
}


def _league_rule(game: Game, dimension: Dimension) -> str:
    where = _DIMENSION_WORDS[dimension][2]
    return f'В лигу попадают игроки, у которых не меньше {MIN_LEAGUE_ROUNDS[game]} раундов в этом {where}.'


def render_league_picker(game: Game, dimension: Dimension, choices: Sequence[Choice]) -> RichMessage:
    """The text above the league buttons. The buttons themselves come from the handler."""
    plural, single, _, _ = _DIMENSION_WORDS[dimension]
    html = f'<p>{_GAME_TITLES[game]}: лиги по {plural}</p>'
    if choices:
        html += f'<p>Выбери {single} кнопкой ниже.</p>'
    else:
        html += '<p>Пока нет ни одной лиги.</p>'
    html += f'<p>{_league_rule(game, dimension)} Лига есть, если таких игроков не меньше {MIN_LEAGUE_PLAYERS}.</p>'
    return {'html': html, 'skip_entity_detection': True}


def render_league(game: Game, league: League, profiles: Sequence[TopResult]) -> RichMessage:
    """Numbers come from league.rows, linked names from profiles, which follow the same order."""
    rows = [
        (
            rank,
            _profile_name(profile),
            _rounds(row.rounds),
            _percent(row.win_rate),
            _strength(row.strength),
            _strength(row.personal),
        )
        for rank, (row, profile) in enumerate(zip(league.rows, profiles, strict=True), 1)
    ]
    html = f'<p>{_GAME_TITLES[game]}: лига {_DIMENSION_WORDS[league.dimension][3]} {escape(league.label)}</p>'
    html += _table(('#', 'Имя', 'Раунды', 'Победы', 'Общая', 'Личная'), rows, alignments=_LEAGUE_COLUMNS)
    html += f'<p>{_league_rule(game, league.dimension)}</p>'
    note = (
        '<p>Общая сила — насколько игрок играет лучше (+) или хуже (\u2212) среднего уровня всех игроков, '
        f'в очках за раунд. К раундам игрока в лиге бот добавляет {SMOOTHING} раундов со средними очками '
        'всех игроков, поэтому несколько удачных раундов не поднимают игрока на первое место. '
        'Лига сортирует игроков по общей силе.</p>'
        '<p>Личная сила — насколько игрок играет здесь лучше или хуже своего обычного уровня, как в /stats. '
        f'Бот добавляет {SMOOTHING} раундов со средними очками самого игрока. Высокая личная сила '
        'показывает знатока этой лиги, даже если в целом игрок слабее других.</p>'
    )
    if league.dimension == 'genre':
        note += (
            f'<p>Если у {_ANSWER_WORDS[game][1]}а несколько жанров, раунд делится между ними поровну, '
            'поэтому раунды бывают дробными.</p>'
        )
    html += f'<details><summary>Как считается сила</summary>{note}</details>'
    return {'html': html, 'skip_entity_detection': True}


_HARD_TITLES: dict[Game, tuple[str, str]] = {
    'framed': ('сложные фильмы', 'Фильм'),
    'episode': ('сложные сериалы', 'Сериал'),
}


def render_hard_titles(game: Game, titles: HardTitles) -> RichMessage:
    """Two ranked lists: the hardest rounds first, then the easiest."""
    title, column = _HARD_TITLES[game]
    html = f'<p>{_GAME_TITLES[game]}: {title}</p>'
    if not titles.hardest and not titles.easiest:
        html += f'<p>Пока нет раундов, где играли не меньше {MIN_TITLE_PLAYERS} игроков.</p>'
    for caption, rows in (('Самые сложные', titles.hardest), ('Самые лёгкие', titles.easiest)):
        if not rows:
            continue
        cells = [
            (
                rank,
                f'{row.name} ({row.year})' if row.year is not None else row.name,
                row.players,
                _percent(row.win_rate),
                _average(row.points),
            )
            for rank, row in enumerate(rows, 1)
        ]
        headers = ('#', column, 'Игроки', 'Победы', 'Очки')
        html += f'<p>{caption}</p>' + _table(headers, cells, alignments=_LEAGUE_ALIGNMENTS)
    html += (
        f'<p>Бот берёт раунды, где играли не меньше {MIN_TITLE_PLAYERS} игроков, и считает средние очки всех игроков '
        'раунда. Чем меньше очков, тем сложнее.</p>'
    )
    return {'html': html, 'skip_entity_detection': True}


async def send_rich_message(
    bot: Bot,
    chat_id: int,
    rich_message: RichMessage,
    *,
    reply_to_message_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Keep PTB serialization, response parsing and ExtBot rate limiting intact."""
    return await bot.do_api_request(
        'sendRichMessage',
        api_kwargs={
            'chat_id': chat_id,
            'rich_message': rich_message,
            'reply_parameters': ReplyParameters(reply_to_message_id) if reply_to_message_id is not None else None,
            'reply_markup': reply_markup,
        },
        return_type=Message,
    )
