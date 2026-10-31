"""Telegram native rich tables, independent of configuration and database access.

HTML tags and attributes follow https://core.telegram.org/bots/api#rich-messages.
Use the public do_api_request bridge until PTB exposes sendRichMessage directly.
"""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from html import escape
from typing import Literal, Protocol, TypedDict, TypeGuard

from telegram import Bot, InlineKeyboardMarkup, Message, ReplyParameters
from telegram.error import TelegramError

_PROFILE_LOOKUP_DEADLINE = 8


class TopType(IntEnum):
    TOP_SCORE = 1
    TOP_FRAME = 2
    TOP_WIN = 3
    TOP_ROUNDS = 4


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


async def enrich_top_profiles(bot: Bot, results: Sequence[TopResult]) -> tuple[_ResolvedTopResult, ...]:
    """Resolve fresh, identity-checked public usernames for at most ten leaderboard rows.

    No stored username is trusted or cached. An eight-second batch deadline also
    bounds time spent waiting for the SDK rate limiter; unfinished lookups fall
    back to ID mentions. Original row order, names and scores are preserved.
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
                return
            username = getattr(chat, 'username', None)
            if (
                type(getattr(chat, 'id', None)) is int
                and chat.id == user_id
                and getattr(chat, 'type', None) == 'private'
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


def _cell(value: str | int | float | _ProfileName | None) -> str:
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
    headers: Sequence[str],
    rows: Sequence[Sequence[str | int | float | _ProfileName | None]],
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


def render_top(top_type: TopType, results: Sequence[TopResult]) -> RichMessage:
    """Render already ranked query results without changing their order or values."""
    title, column = {
        TopType.TOP_SCORE: ('Топ по очкам', 'Очки'),
        TopType.TOP_FRAME: ('Топ по среднему кадру', 'Средний кадр'),
        TopType.TOP_WIN: ('Топ по количеству отгаданных фильмов', 'Фильмы'),
        TopType.TOP_ROUNDS: ('Топ по количеству участий', 'Участия'),
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
    html = f'<p>{title}</p>' + _table(('#', 'Имя', column), rows, alignments=('center', 'left', 'center'))
    if top_type == TopType.TOP_FRAME:
        html += '<p>Неугаданный фильм = 7 кадров; меньше — лучше.</p>'
    if not results:
        html += '<p>Пока нет результатов.</p>'
    return {'html': html, 'skip_entity_detection': True}


def render_stats(framed_stats: GameStats, episode_stats: GameStats) -> RichMessage:
    rows = [
        (game, stats.rounds_count, stats.rounds_won_count, _average(stats.average_frame))
        for game, stats in (('framed.wtf', framed_stats), ('episode.wtf', episode_stats))
    ]
    html = '<p>Твоя статистика</p>' + _table(('Игра', 'Раунды', 'Победы', 'Средний кадр'), rows)
    html += '<p>Средний кадр — только по победам. «—» означает, что побед пока нет.</p>'
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
