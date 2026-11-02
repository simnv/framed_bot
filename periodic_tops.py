"""Monthly and yearly tops for each group: periods, post markers in bot_data, and delivery with retries.

Schedule (spec 2026-11-02): 12:00 Europe/Moscow = 09:00 UTC. The monthly top of a month is due on the first day
of the next month. The yearly top counts Jan 1 to Dec 30 and is due on Dec 31; the Dec 31 round is in the
December monthly top. Only the latest due period of each kind is posted.
"""

from __future__ import annotations

import asyncio
import calendar
import dataclasses
import functools
import logging
import re
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal

from telegram import Bot
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes, JobQueue

from answers import GAMES, Game, current_round, round_date
from models.group import Group
from models.top import MIN_ROUNDS_FOR_AVERAGE, top_results
from rich_tables import RichMessage, TopResult, enrich_top_profiles, render_period_top, send_rich_message
from top_type import TopType

type Kind = Literal['monthly', 'yearly']
type Outcome = Literal['sent', 'skipped', 'removed', 'failed']

KINDS: Final[tuple[Kind, ...]] = ('monthly', 'yearly')
# 12:00 Europe/Moscow is UTC+3 all year, so the job runs at 09:00 UTC.
POST_TIME: Final = time(9, 0, tzinfo=UTC)
MARKER_PREFIX: Final = 'periodic_top'
FIRST_YEARLY_YEAR: Final = 2026
GAME_NAMES: Final[dict[Game, str]] = {'framed': 'Framed', 'episode': 'Episode'}
# Genitive month names: 'итоги октября', '1–31 октября'.
MONTHS: Final = (
    'января',
    'февраля',
    'марта',
    'апреля',
    'мая',
    'июня',
    'июля',
    'августа',
    'сентября',
    'октября',
    'ноября',
    'декабря',
)
DEAD_CHAT_ERRORS: Final = ('chat not found', 'group chat was deactivated')
PERIOD_PATTERN: Final = r'(?P<year>[0-9]{4})(?:-(?P<month>0[1-9]|1[0-2]))?'


@dataclasses.dataclass(frozen=True, slots=True)
class Period:
    kind: Kind
    year: int
    month: int | None = None

    @property
    def id(self) -> str:
        return f'{self.year}-{self.month:02}' if self.month else str(self.year)

    @property
    def start(self) -> date:
        return date(self.year, self.month or 1, 1)

    @property
    def end(self) -> date:
        """The last day of the period, inclusive. A year stops at Dec 30: the Dec 31 round is in December."""
        if self.month:
            return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])
        return date(self.year, 12, 30)

    @property
    def due_at(self) -> datetime:
        return datetime.combine(self.end + timedelta(days=1), POST_TIME)

    def previous(self) -> Period:
        if self.month is None:
            return Period(self.kind, self.year - 1)
        if self.month == 1:
            return Period(self.kind, self.year - 1, 12)
        return Period(self.kind, self.year, self.month - 1)

    def rounds(self, game: Game) -> range:
        """The rounds whose UTC date is in the period."""
        first = current_round(game, self.start)
        if round_date(game, first) < self.start:
            first += 1
        return range(first, current_round(game, self.end) + 1)

    def heading(self, game: Game) -> str:
        name = GAME_NAMES[game]
        if self.month:
            return f'🏆 {name} — итоги {MONTHS[self.month - 1]} {self.year}'
        return f'🏆 {name} — итоги {self.year} года'

    def rounds_line(self, game: Game) -> str:
        rounds = self.rounds(game)
        if self.month:
            dates = f'{self.start.day}–{self.end.day} {MONTHS[self.month - 1]}'
        else:
            dates = f'{self.start.day} {MONTHS[self.start.month - 1]} – {self.end.day} {MONTHS[self.end.month - 1]}'
        return f'Раунды #{rounds.start}–#{rounds.stop - 1}, {dates}'


def parse_period(text: str) -> Period | None:
    """'2026-10' is a monthly period, '2026' a yearly one."""
    match = re.fullmatch(PERIOD_PATTERN, text)
    if match is None:
        return None
    year = int(match.group('year'))
    month = match.group('month')
    return Period('monthly', year, int(month)) if month else Period('yearly', year)


def latest_due(kind: Kind, now: datetime) -> Period:
    """The newest period of the kind whose post time has passed. Older missed periods are never posted."""
    # The current month is never due; the current year is due from Dec 31 09:00 UTC.
    period = Period('monthly', now.year, now.month).previous() if kind == 'monthly' else Period('yearly', now.year)
    return period if now >= period.due_at else period.previous()


def frame_gate(round_count: int) -> int:
    """The average-frame gate of a period: min(10, ceil(R / 2))."""
    return min(MIN_ROUNDS_FOR_AVERAGE, (round_count + 1) // 2)


def marker_key(kind: Kind, chat_id: int, game: Game) -> str:
    """One marker for each chat, kind and game, so a partial send never posts a finished game twice."""
    return f'{MARKER_PREFIX}:{kind}:{chat_id}:{game}'


def last_posted(bot_data: MutableMapping[str, str], kind: Kind, chat_id: int, game: Game) -> str | None:
    return bot_data.get(marker_key(kind, chat_id, game))


def is_due(bot_data: MutableMapping[str, str], kind: Kind, chat_id: int, game: Game, period: Period) -> bool:
    if period.kind == 'yearly' and period.year < FIRST_YEARLY_YEAR:
        return False
    last = last_posted(bot_data, kind, chat_id, game)
    return last is None or period.id > last


def mark_posted(bot_data: MutableMapping[str, str], kind: Kind, chat_id: int, game: Game, period: Period) -> None:
    """Mark the game as done for the period. A game skipped for no results is done too.

    Keep the later period: a forced post of an old period must not make a newer one due again.
    """
    last = last_posted(bot_data, kind, chat_id, game)
    bot_data[marker_key(kind, chat_id, game)] = period.id if last is None else max(last, period.id)


def move_markers(bot_data: MutableMapping[str, str], old_id: int, new_id: int) -> None:
    """Rename the markers of a chat that became a supergroup. If the new chat has a marker, the later period wins."""
    for kind in KINDS:
        for game in GAMES:
            value = bot_data.pop(marker_key(kind, old_id, game), None)
            if value is None:
                continue
            new_key = marker_key(kind, new_id, game)
            bot_data[new_key] = max(value, bot_data.get(new_key, value))


async def deliver(chat_id: int, send: Callable[[], Awaitable[object]]) -> Outcome:
    """Call send with the retry rules of /announce. A dead chat loses its group row; its results stay."""
    for attempt in range(3):
        try:
            await send()
            return 'sent'
        except Forbidden:
            await Group.remove(chat_id)
            logging.info('Removed unavailable recipient %s', chat_id)
            return 'removed'
        except BadRequest as error:
            if str(error).lower() in DEAD_CHAT_ERRORS:
                await Group.remove(chat_id)
                logging.info('Removed unavailable recipient %s', chat_id)
                return 'removed'
            logging.warning('Message rejected for recipient %s', chat_id)
            return 'failed'
        except RetryAfter as error:
            delay = error.retry_after
            seconds = delay.total_seconds() if isinstance(delay, timedelta) else delay
            if attempt == 2 or seconds > 5:
                logging.warning('Rate limit exceeded for recipient %s', chat_id)
                return 'failed'
            await asyncio.sleep(seconds)
        except NetworkError:
            if attempt == 2:
                logging.warning('Network retries exhausted for recipient %s', chat_id)
                return 'failed'
            await asyncio.sleep(attempt + 1)
        except TelegramError:
            logging.warning('Message failed for recipient %s', chat_id)
            return 'failed'
    return 'failed'


async def _build_game_message(bot: Bot, chat_id: int, period: Period, game: Game) -> RichMessage | None:
    rounds = period.rounds(game)
    gate = frame_gate(len(rounds))
    boards: dict[TopType, Sequence[TopResult]] = {}
    for top_type in TopType:
        rows = await top_results(game, top_type, chat_id, rounds=rounds, min_rounds=gate)
        boards[top_type] = await enrich_top_profiles(bot, rows, group_chat_id=chat_id)
    if not boards[TopType.TOP_ROUNDS]:
        return None
    return render_period_top(game, period.heading(game), period.rounds_line(game), boards, frame_gate=gate)


async def build_messages(bot: Bot, chat_id: int, period: Period) -> list[RichMessage]:
    """Build one static message per game with results, Framed first, for an admin preview."""
    messages = []
    for game in GAMES:
        message = await _build_game_message(bot, chat_id, period, game)
        if message is not None:
            messages.append(message)
    return messages


async def post_period(
    bot: Bot,
    bot_data: MutableMapping[str, str],
    chat_id: int,
    period: Period,
    *,
    games: Sequence[Game] = GAMES,
) -> Outcome:
    """Force-post the selected games and mark each sent or empty game immediately.

    The job selects only due games; an admin post defaults to both games regardless of markers.
    A failed game keeps its marker and does not stop the other game. A removed chat stops delivery.
    """
    outcome: Outcome = 'skipped'
    for game in GAMES:
        if game not in games:
            continue
        message = await _build_game_message(bot, chat_id, period, game)
        if message is None:
            mark_posted(bot_data, period.kind, chat_id, game, period)
            continue
        result = await deliver(chat_id, functools.partial(send_rich_message, bot, chat_id, message))
        if result == 'removed':
            return result
        if result == 'sent':
            mark_posted(bot_data, period.kind, chat_id, game, period)
            if outcome == 'skipped':
                outcome = 'sent'
        else:
            outcome = 'failed'
    return outcome


# The startup and daily jobs can overlap on a restart at 09:00 UTC.
# A chat migration also takes this lock, so it cannot run between a send and its marker write.
run_lock = asyncio.Lock()


async def post_due_tops(bot: Bot, bot_data: MutableMapping[str, str], now: datetime | None = None) -> None:
    """Post only the latest due period of each kind, retrying each unfinished game separately."""
    async with run_lock:
        now = now or datetime.now(UTC)
        for group in await Group.get_all():
            for kind in KINDS:
                period = latest_due(kind, now)
                games = tuple(game for game in GAMES if is_due(bot_data, kind, group.id, game, period))
                if not games:
                    continue
                if await post_period(bot, bot_data, group.id, period, games=games) == 'removed':
                    break


async def post_due_tops_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await post_due_tops(context.bot, context.bot_data)
    # Persist the markers before the next scheduled persistence update.
    await context.application.update_persistence()


def schedule(job_queue: JobQueue[ContextTypes.DEFAULT_TYPE]) -> None:
    job_queue.run_daily(post_due_tops_job, POST_TIME, name='periodic-tops')
    # The queue starts after post_init; keep the startup run even if it is late.
    job_queue.run_once(post_due_tops_job, 0, name='periodic-tops', job_kwargs={'misfire_grace_time': None})
