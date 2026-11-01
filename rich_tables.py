"""Telegram native rich tables, independent of configuration and database access.

HTML tags and attributes follow https://core.telegram.org/bots/api#rich-messages.
Use the public do_api_request bridge until PTB exposes sendRichMessage directly.
"""

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Literal, Protocol, TypedDict, TypeGuard

from telegram import Bot, InlineKeyboardMarkup, Message, ReplyParameters
from telegram.error import TelegramError

from answers import FRAMES, Game
from insight_types import (
    MIN_LEAGUE_PLAYERS,
    MIN_LEAGUE_ROUNDS,
    MIN_TITLE_PLAYERS,
    SMOOTHING,
    STATS_BEST,
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
# Telegram rejects a longer rich message. render_stats trims the insight sections to fit.
STATS_HTML_LIMIT = 4096


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


_STATS_NOTE = (
    'Очки и «Ср. кадр (все)» бот считает так же, как в /top. '
    'Во Framed промах считается как кадр 7, в Episode как кадр 11. '
    '«Ср. кадр (победы)» учитывает только угаданные раунды. '
    'Прочерк значит, что побед или раундов пока нет.'
)


def _rounds(value: float) -> str:
    """Weighted round counts get one decimal only when they are fractional."""
    rounded = round(value, 1)
    return str(int(rounded)) if rounded.is_integer() else f'{rounded:.1f}'


def _percent(value: float) -> str:
    return f'{value:.0%}'


def _bucket_row(bucket: Bucket) -> tuple[str, ...]:
    return (bucket.label, _average(bucket.points), _rounds(bucket.rounds), _percent(bucket.win_rate))


def _trim_section(section: Section, limit: int) -> tuple[tuple[Bucket, ...], tuple[Bucket, ...]]:
    """Keep at most limit best and limit worst buckets. The worst stay in points order, best first.

    A section that holds all its buckets in best stays whole when it has 2 * limit buckets or fewer.
    """
    if section.worst:
        worst = tuple(sorted(section.worst, key=lambda bucket: bucket.points, reverse=True))
        return section.best[:limit], worst[-limit:]
    if len(section.best) > 2 * limit:
        return section.best[:limit], section.best[-limit:]
    return section.best, ()


_RANKED_SECTIONS: dict[str, tuple[str, str, str, str]] = {
    # section title, table column, verdict for one bucket, verdict for two buckets
    'genres': ('жанры', 'Жанр', 'Лучший жанр', 'Лучшие жанры'),
    'decades': ('десятилетия', 'Десятилетие', 'Лучшее десятилетие', 'Лучшие десятилетия'),
}
_LEAGUE_ALIGNMENTS: tuple[Literal['left', 'center'], ...] = ('center', 'left', 'center', 'center', 'center')
_ROLE_LABELS = {'director': 'режиссёр', 'creator': 'создатель', 'cast': 'актёр'}
_BOARD_LABELS = {'score': 'Очки', 'frame': 'Средний кадр', 'rounds': 'Участия'}


def _ranked_section_html(title: str, section: Section, kind: str, limit: int) -> str:
    best, worst = _trim_section(section, limit)
    if not best:
        return ''
    name, column, one, many = _RANKED_SECTIONS[kind]
    rows: list[Sequence[str]] = [_bucket_row(bucket) for bucket in best]
    if worst:
        rows.append(('…',) * 4)
        rows.extend(_bucket_row(bucket) for bucket in worst)
    html = f'<p>{title}: {name}</p>' + _table((column, 'Очки', 'Раунды', 'Победы'), rows)
    leaders = best[:2]
    verdict = one if len(leaders) == 1 else many
    return html + f'<p>{verdict}: {", ".join(escape(bucket.label) for bucket in leaders)}.</p>'


def _people_html(title: str, section: Section) -> str:
    if not section.best:
        return ''
    rows = [
        (bucket.label, _ROLE_LABELS.get(bucket.role or '', '—'), *_bucket_row(bucket)[1:]) for bucket in section.best
    ]
    headers = ('Кто', 'Роль', 'Очки', 'Раунды', 'Победы')
    return f'<p>{title}: люди</p>' + _table(headers, rows)


def _rank_label(game: Game, rank: Rank) -> str:
    match rank.board:
        case 'win':
            return TOP_WIN_LABELS[game]
        case 'genre':
            return f'Жанр {rank.label}'
        case 'decade':
            return f'Десятилетие {rank.label}'
        case board:
            return _BOARD_LABELS[board]


def _game_insight_html(game: Game, insight: GameInsight, *, limit: int, people: bool) -> str:
    title = _GAME_TITLES[game]
    html = _ranked_section_html(title, insight.genres, 'genres', limit)
    html += _ranked_section_html(title, insight.decades, 'decades', limit)
    if people:
        html += _people_html(title, insight.people)
    if insight.best_streak > 0:
        html += f'<p>{title}: серия побед сейчас {insight.current_streak}, лучшая {insight.best_streak}.</p>'
    if insight.ranks:
        rows = [(_rank_label(game, rank), f'{rank.place} из {rank.total}') for rank in insight.ranks]
        html += f'<p>{title}: места в /top</p>' + _table(('Таблица', 'Место'), rows)
    return html


def _trimmed_insights_html(insights: Mapping[Game, GameInsight], head_length: int) -> str:
    """Build the insight sections and trim them step by step until the whole message fits the limit.

    The steps: best and worst 3 become 2, then the people sections go, then the Episode sections go.
    If the last step is still too long, no insight is shown, because Telegram rejects a longer message.
    """
    steps: tuple[tuple[int, bool, tuple[Game, ...]], ...] = (
        (STATS_BEST, True, ('framed', 'episode')),
        (2, True, ('framed', 'episode')),
        (2, False, ('framed', 'episode')),
        (2, False, ('framed',)),
    )
    for limit, people, games in steps:
        html = ''.join(
            _game_insight_html(game, insights[game], limit=limit, people=people) for game in games if game in insights
        )
        if head_length + len(html) <= STATS_HTML_LIMIT:
            return html
    return ''


def render_stats(
    framed_stats: GameStats,
    episode_stats: GameStats,
    *,
    owner: TopResult | None = None,
    insights: Mapping[Game, GameInsight] | None = None,
) -> RichMessage:
    """Render the result table, then the insight sections per game, trimmed to STATS_HTML_LIMIT."""
    games = (framed_stats, episode_stats)
    rows = [
        ('Раунды', *(stats.rounds_count for stats in games)),
        ('Победы', *(stats.rounds_won_count for stats in games)),
        ('Очки', *(stats.points for stats in games)),
        ('Ср. кадр (победы)', *(_average(stats.average_frame) for stats in games)),
        ('Ср. кадр (все)', *(_average(stats.average_frame_all) for stats in games)),
    ]
    headers = ('Показатель', _Link('Framed', 'https://framed.wtf/'), _Link('Episode', 'https://episode.wtf/'))
    html = f'<p>Статистика для {_cell(_profile_name(owner))}</p>' if owner is not None else '<p>Твоя статистика</p>'
    html += _table(headers, rows)
    html += f'<p>{_STATS_NOTE}</p>'
    if insights:
        html += _trimmed_insights_html(insights, len(html))
    return {'html': html, 'skip_entity_detection': True}


_DIMENSION_WORDS: dict[Dimension, tuple[str, str, str, str]] = {
    # dative plural, accusative singular, prepositional singular, genitive singular
    'genre': ('жанрам', 'жанр', 'жанре', 'жанра'),
    'decade': ('десятилетиям', 'десятилетие', 'десятилетии', 'десятилетия'),
}


def _league_rule(dimension: Dimension) -> str:
    where = _DIMENSION_WORDS[dimension][2]
    return f'В лигу попадают игроки, у которых не меньше {MIN_LEAGUE_ROUNDS} раундов в этом {where}.'


def render_league_picker(game: Game, dimension: Dimension, choices: Sequence[Choice]) -> RichMessage:
    """The text above the league buttons. The buttons themselves come from the handler."""
    plural, single, _, _ = _DIMENSION_WORDS[dimension]
    html = f'<p>{_GAME_TITLES[game]}: лиги по {plural}</p>'
    if choices:
        html += f'<p>Выбери {single} кнопкой ниже.</p>'
    else:
        html += '<p>Пока нет ни одной лиги.</p>'
    html += f'<p>{_league_rule(dimension)} Лига есть, если таких игроков не меньше {MIN_LEAGUE_PLAYERS}.</p>'
    return {'html': html, 'skip_entity_detection': True}


def render_league(game: Game, league: League, profiles: Sequence[TopResult]) -> RichMessage:
    """Numbers come from league.rows, linked names from profiles, which follow the same order."""
    rows = [
        (rank, _profile_name(profile), _average(row.score), _rounds(row.rounds), _percent(row.win_rate))
        for rank, (row, profile) in enumerate(zip(league.rows, profiles, strict=True), 1)
    ]
    html = f'<p>{_GAME_TITLES[game]}: лига {_DIMENSION_WORDS[league.dimension][3]} {escape(league.label)}</p>'
    html += _table(('#', 'Имя', 'Очки', 'Раунды', 'Победы'), rows, alignments=_LEAGUE_ALIGNMENTS)
    note = (
        f'Очки — средние очки за раунд в этой лиге со сглаживанием: бот добавляет {SMOOTHING} раундов '
        'со средними очками всех игроков. '
    )
    if league.dimension == 'genre':
        note += f'Раунды — сумма весов: {_ANSWER_WORDS[game][1]} с несколькими жанрами делит раунд между ними поровну. '
    html += f'<p>{note}{_league_rule(league.dimension)}</p>'
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
