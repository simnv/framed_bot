"""Telegram native rich tables, independent of configuration and database access.

HTML tags and attributes follow https://core.telegram.org/bots/api#rich-messages.
Use the public do_api_request bridge until PTB exposes sendRichMessage directly.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Literal, Protocol, TypedDict, TypeGuard

from telegram import Bot, InlineKeyboardMarkup, Message, ReplyParameters
from telegram.error import TelegramError

from answers import FRAMES, Game
from top_type import TopType

_PROFILE_LOOKUP_DEADLINE = 8


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
        (
            rank,
            _ProfileName(
                result.name,
                getattr(result, 'user_id', None),
                result.public_username if isinstance(result, _ResolvedTopResult) else None,
            ),
            _average(result.score) if top_type == TopType.TOP_FRAME else result.score,
        )
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


def render_stats(framed_stats: GameStats, episode_stats: GameStats) -> RichMessage:
    games = (framed_stats, episode_stats)
    rows = [
        ('Раунды', *(stats.rounds_count for stats in games)),
        ('Победы', *(stats.rounds_won_count for stats in games)),
        ('Очки', *(stats.points for stats in games)),
        ('Ср. кадр (победы)', *(_average(stats.average_frame) for stats in games)),
        ('Ср. кадр (все)', *(_average(stats.average_frame_all) for stats in games)),
    ]
    headers = ('Показатель', _Link('Framed', 'https://framed.wtf/'), _Link('Episode', 'https://episode.wtf/'))
    html = '<p>Твоя статистика</p>' + _table(headers, rows)
    html += f'<p>{_STATS_NOTE}</p>'
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
