# ruff: noqa: E402
# Configure redaction before importing configuration, models, or Telegram.
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import httpx

from safe_logging import configure_logging

configure_logging()

from config import ADMIN_USER_ID, BOT_DATA_PATH, BOT_TOKEN, DB_CONNECTION_STRING, TMDB_API_KEY

configure_logging((BOT_TOKEN, DB_CONNECTION_STRING, TMDB_API_KEY or ''))

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    Update,
)
from telegram import (
    Message as TelegramMessage,
)
from telegram.constants import ReactionEmoji
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    JobQueue,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.ext.filters import Message, MessageFilter

from answers import GAMES, KIND_BY_GAME, Game, parse_game, round_limit, utc_today
from answers.scraper import fetch_titles
from answers.store import DbAnswerStore
from answers.sync import AnswerSync
from answers.tmdb import TmdbClient, TmdbError
from models import FramedResult, User
from models.episode_result import EpisodeResult
from models.group import Group
from models.migrate import upgrade_database
from reaction_delivery import ReactionDelivery, ReactionIntent
from rich_tables import TopType, enrich_top_profiles, render_stats, render_top, send_rich_message
from stats import get_user_stats

framed_pattern = r'Framed #(?P<round>[\d]+)\n🎥(?P<result>(?: 🟥| 🟩| ⬛| ⬛️){6})\n\nhttps:\/\/framed\.wtf'
episode_pattern = r'Episode #(?P<round>[\d]+)\n📺(?P<result>(?: 🟥| 🟩| ⬛| ⬛️){10})\n\nhttps:\/\/episode\.wtf'
saved_result_reaction = ReactionTypeEmoji(ReactionEmoji.WRITING_HAND)
first_frame_saved_result_reaction = ReactionTypeEmoji(ReactionEmoji.TROPHY)
duplicate_result_reaction = ReactionTypeEmoji(ReactionEmoji.GHOST)
game_by_pattern: dict[str, Game] = {framed_pattern: 'framed', episode_pattern: 'episode'}
tmdb_notice = 'This product uses the TMDB API but is not endorsed or certified by TMDB.'
help_text = (
    'Я сохраняю результаты Framed и Episode, которые вы отправляете в группу.\n'
    '/stats — твоя статистика\n'
    '/top — рейтинг игроков\n\n'
    f'{tmdb_notice}'
)
set_title_usage = 'Использование: /set_title <framed|episode> <раунд> <tmdb_id>'


class ReactionApplication(
    Application[
        ExtBot[None],
        ContextTypes.DEFAULT_TYPE,
        dict[str, str],
        dict[str, str],
        dict[str, str],
        JobQueue[ContextTypes.DEFAULT_TYPE],
    ]
):
    """Ephemeral delivery state is excluded from PicklePersistence's bot_data."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reaction_delivery = ReactionDelivery(self.bot)
        self.answer_store = DbAnswerStore()
        # post_init creates the answer services; they stay None in tests that skip it.
        self.http_client: httpx.AsyncClient | None = None
        self.tmdb: TmdbClient | None = None
        self.answer_sync: AnswerSync | None = None

    async def stop(self) -> None:
        if not self.running:
            await super().stop()
            return
        # PTB.stop waits for queued handlers before post_stop. Release admission
        # backpressure now and begin the drain deadline before that wait.
        self.reaction_delivery.close_admissions()
        draining = asyncio.create_task(self.reaction_delivery.stop(), name='reaction-drain')
        try:
            # PTB's stop() waits for running jobs, and an answer backfill can run for minutes.
            if self.answer_sync is not None:
                await self.answer_sync.shutdown()
            await super().stop()
        finally:
            await draining


def reaction_delivery_for(application: object) -> ReactionDelivery:
    if not isinstance(application, ReactionApplication):
        raise TypeError('ReactionApplication is required for result delivery')
    return application.reaction_delivery


def answer_application_for(application: object) -> ReactionApplication:
    if not isinstance(application, ReactionApplication):
        raise TypeError('ReactionApplication is required for game answers')
    return application


@dataclass(frozen=True, slots=True)
class AnswerSyncRequest:
    game: Game
    # None: up to the newest round in the result table of the game.
    up_to_round: int | None


async def sync_answers_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None or not isinstance(job.data, AnswerSyncRequest):
        return
    application = answer_application_for(context.application)
    if application.answer_sync is None:
        return
    request = job.data
    up_to_round = request.up_to_round
    if up_to_round is None:
        newest = await application.answer_store.max_result_round(request.game)
        # The stored maximum comes from player messages, so it is untrusted too.
        up_to_round = application.answer_sync.limit(request.game, newest) if newest else None
    if up_to_round:
        await application.answer_sync.sync(request.game, up_to_round)


async def schedule_answer_sync(context: ContextTypes.DEFAULT_TYPE, game: Game, round_number: int) -> None:
    """Schedule a sync for a round without a stored answer. Errors never reach result saving."""
    try:
        application = answer_application_for(context.application)
        if application.answer_sync is None:
            return
        job_queue = context.job_queue
        if job_queue is None:
            return
        if round_number > round_limit(game, utc_today()):
            # A forged or mistyped round must not reach the database or the scraper.
            logging.info('Ignored %s round %d after the current round', game, round_number)
            return
        if await application.answer_store.has_answer(game, round_number):
            return
        job_queue.run_once(sync_answers_job, 0, data=AnswerSyncRequest(game, round_number), name=f'answers-{game}')
    except Exception:
        logging.exception('Cannot schedule the %s answer sync for round %d', game, round_number)


async def alert_admin(bot: ExtBot[None], text: str) -> None:
    try:
        await bot.send_message(chat_id=ADMIN_USER_ID, text=text)
    except TelegramError:
        logging.warning('Cannot send the game answer alert to the admin')


def start_answers(application: ReactionApplication) -> None:
    """Create the answer services and schedule the startup backfill. Startup does not wait for it."""
    client = httpx.AsyncClient()
    application.http_client = client
    if TMDB_API_KEY:
        application.tmdb = TmdbClient(TMDB_API_KEY, client)
    else:
        logging.warning('TMDB_API_KEY is not set; the bot stores game answers without title data')

    async def scrape(game: Game) -> list[str]:
        return await fetch_titles(game, client)

    async def alert(text: str) -> None:
        await alert_admin(application.bot, text)

    application.answer_sync = AnswerSync(application.answer_store, scrape, application.tmdb, alert)
    job_queue = application.job_queue
    if job_queue is None:
        return
    for game in GAMES:
        # The queue starts after post_init and after the Telegram bootstrap; never drop these jobs as missed.
        job_queue.run_once(
            sync_answers_job,
            0,
            data=AnswerSyncRequest(game, None),
            name=f'answers-{game}',
            job_kwargs={'misfire_grace_time': None},
        )


async def stop_answers(application: ReactionApplication) -> None:
    if application.http_client is not None:
        await application.http_client.aclose()
        application.http_client = None


def saved_reaction_for(win_frame: int | None) -> ReactionTypeEmoji:
    return first_frame_saved_result_reaction if win_frame == 1 else saved_result_reaction


class FramedFilter(MessageFilter):
    def filter(self, message: Message):
        return message.text is not None and re.search(framed_pattern, message.text) is not None


FRAMED_FILTER = FramedFilter(name='FramedFilter')


class EpisodeFilter(MessageFilter):
    def filter(self, message: Message):
        return message.text is not None and re.search(episode_pattern, message.text) is not None


EPISODE_FILTER = EpisodeFilter(name='EpisodeFilter')


class ResultSaver(Protocol):
    @staticmethod
    async def save_result(user_id: int, framed_round: int, won: bool, win_frame: int | None) -> bool: ...


async def delete_message_task(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    if job is None:
        return
    chat_id = job.chat_id
    message_id = job.data
    if chat_id is None or not isinstance(message_id, int):
        return
    await context.bot.delete_message(chat_id, message_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return
    await context.bot.send_message(chat_id=chat.id, text='Привет')


async def save_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pattern: str,
    result_class: type[ResultSaver],
):
    effective_user = update.effective_user
    effective_chat = update.effective_chat
    message = update.message
    if effective_user is None or effective_chat is None or message is None or message.text is None:
        return

    delivery = reaction_delivery_for(context.application)
    existing = delivery.known_intent(effective_chat.id, message.id)
    if existing is not None:
        await delivery.submit(existing)
        return

    await User.update_from_tg_user(effective_user)
    result = re.search(pattern, message.text)
    if result is None:
        return

    data_round = int(result.groupdict()['round'])
    data_result = result.groupdict()['result']
    data_won = '🟩' in data_result
    data_win_frame = data_result.count('🟥') + 1 if data_won else None

    saved = await result_class.save_result(effective_user.id, data_round, data_won, data_win_frame)

    reaction = saved_reaction_for(data_win_frame) if saved else duplicate_result_reaction

    await delivery.submit(ReactionIntent(effective_chat.id, message.id, reaction.emoji))

    game = game_by_pattern.get(pattern)
    if saved and game is not None:
        await schedule_answer_sync(context, game, data_round)


async def new_framed_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_results(update, context, framed_pattern, FramedResult)


async def new_episode_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await save_results(update, context, episode_pattern, EpisodeResult)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    effective_user = update.effective_user
    effective_chat = update.effective_chat
    message = update.message
    if effective_user is None or effective_chat is None or message is None:
        return

    user_stats = await get_user_stats(effective_user.id)

    if user_stats is None:
        await context.bot.send_message(chat_id=effective_chat.id, text='Я тебя не знаю', reply_to_message_id=message.id)
        return

    framed_stats, episode_stats = user_stats

    reply_message = await send_rich_message(
        context.bot,
        effective_chat.id,
        render_stats(framed_stats, episode_stats),
        reply_to_message_id=message.id,
    )

    job_queue = context.job_queue
    if job_queue is None:
        return

    job_queue.run_once(delete_message_task, timedelta(seconds=30), message.id, chat_id=effective_chat.id)

    job_queue.run_once(delete_message_task, timedelta(seconds=30), reply_message.id, chat_id=effective_chat.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return
    await context.bot.send_message(chat_id=chat.id, text=help_text)


async def set_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin fix: link a round to a TMDB id. Later syncs never change this row."""
    chat = update.effective_chat
    if chat is None:
        return
    application = answer_application_for(context.application)
    args = context.args or []
    game = parse_game(args[0]) if len(args) == 3 else None
    if game is None or not args[1].isdigit() or not args[2].isdigit():
        await context.bot.send_message(chat_id=chat.id, text=set_title_usage)
        return
    round_number, tmdb_id = int(args[1]), int(args[2])
    if application.tmdb is None:
        await context.bot.send_message(chat_id=chat.id, text='TMDB_API_KEY не задан')
        return
    try:
        data = await application.tmdb.details(KIND_BY_GAME[game], tmdb_id)
    except TmdbError as error:
        logging.warning('TMDB lookup for /set_title failed: %s', error)
        await context.bot.send_message(chat_id=chat.id, text='TMDB не ответил, попробуй позже')
        return
    if not await application.answer_store.set_manual_title(game, round_number, data):
        await context.bot.send_message(chat_id=chat.id, text=f'Ответ {game} #{round_number} ещё не сохранён')
        return
    year = f' ({data.year})' if data.year else ''
    await context.bot.send_message(chat_id=chat.id, text=f'{game} #{round_number}: {data.name}{year}')


async def update_chat_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    effective_chat = update.effective_chat
    if effective_chat is None:
        return
    await Group.update_from_tg_chat(effective_chat)


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    effective_message = update.effective_message
    if effective_message is None or effective_message.text is None:
        return
    _, _, announcement_text = effective_message.text.partition(' ')
    if not announcement_text:
        return
    groups = await Group.get_all()
    for group in groups:
        for attempt in range(3):
            try:
                await context.bot.send_message(chat_id=group.id, text=announcement_text)
                break
            except Forbidden:
                await Group.remove(group.id)
                logging.info('Removed unavailable announcement recipient %s', group.id)
                break
            except BadRequest as error:
                if str(error).lower() in ('chat not found', 'group chat was deactivated'):
                    await Group.remove(group.id)
                logging.warning('Announcement rejected for recipient %s', group.id)
                break
            except RetryAfter as error:
                delay = error.retry_after
                seconds = delay.total_seconds() if isinstance(delay, timedelta) else delay
                if attempt == 2 or seconds > 5:
                    logging.warning('Announcement rate limit exceeded for recipient %s', group.id)
                    break
                await asyncio.sleep(seconds)
            except NetworkError:
                if attempt == 2:
                    logging.warning('Announcement network retries exhausted for recipient %s', group.id)
                    break
                await asyncio.sleep(attempt + 1)
            except TelegramError:
                logging.warning('Announcement failed for recipient %s', group.id)
                break


def top_reply_markup(top_type: TopType):
    type_to_text = {
        TopType.TOP_SCORE: 'По очкам',
        TopType.TOP_ROUNDS: 'По участиям',
        TopType.TOP_FRAME: 'По кадрам',
        TopType.TOP_WIN: 'По фильмам',
    }
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(type_to_text[_type], callback_data=json.dumps({'top': _type}))
                for _type in type_to_text
                if _type != top_type
            ]
        ]
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    effective_chat = update.effective_chat
    message = update.message
    if effective_chat is None or message is None:
        return
    results = await FramedResult.top_score()
    profiles = await enrich_top_profiles(
        context.bot,
        results,
        group_chat_id=effective_chat.id if effective_chat.type in ('group', 'supergroup') else None,
    )
    await send_rich_message(
        context.bot,
        effective_chat.id,
        render_top(TopType.TOP_SCORE, profiles),
        reply_to_message_id=message.id,
        reply_markup=top_reply_markup(TopType.TOP_SCORE),
    )


async def inline_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.data is None:
        return
    data = json.loads(query.data)
    top_type = TopType(data['top'])
    results = []
    match top_type:
        case TopType.TOP_WIN:
            results = await FramedResult.top_won()
        case TopType.TOP_FRAME:
            results = await FramedResult.top_average_frame()
        case TopType.TOP_SCORE:
            results = await FramedResult.top_score()
        case TopType.TOP_ROUNDS:
            results = await FramedResult.top_rounds()

    query_message = query.message
    if not isinstance(query_message, TelegramMessage):
        await query.answer()
        return
    profiles = await enrich_top_profiles(
        context.bot,
        results,
        group_chat_id=query_message.chat_id if query_message.chat.type in ('group', 'supergroup') else None,
    )
    # PTB's typed edit_message_text still requires text; the public API bridge
    # allows the new rich_message parameter without sending a plain text field.
    await context.bot.do_api_request(
        'editMessageText',
        api_kwargs={
            'chat_id': query_message.chat_id,
            'message_id': query_message.message_id,
            'rich_message': render_top(top_type, profiles),
            'reply_markup': top_reply_markup(top_type),
        },
    )

    await query.answer()


async def initialize_database(
    _application: Application[
        ExtBot[None],
        ContextTypes.DEFAULT_TYPE,
        dict[str, str],
        dict[str, str],
        dict[str, str],
        JobQueue[ContextTypes.DEFAULT_TYPE],
    ],
) -> None:
    await upgrade_database()
    reaction_delivery_for(_application).start()
    start_answers(answer_application_for(_application))


async def stop_reaction_delivery(application: object) -> None:
    # The post_stop hook also closes the HTTP client of the answer services.
    await reaction_delivery_for(application).stop()
    await stop_answers(answer_application_for(application))


def build_application() -> ReactionApplication:
    application = (
        ApplicationBuilder()
        .application_class(ReactionApplication)
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .update_queue(asyncio.Queue(maxsize=100))
        .persistence(PicklePersistence(BOT_DATA_PATH))
        .post_init(initialize_database)
        .post_stop(stop_reaction_delivery)
        .build()
    )
    if not isinstance(application, ReactionApplication):
        raise TypeError('ApplicationBuilder did not create ReactionApplication')

    start_handler = CommandHandler('start', start, block=False)
    application.add_handler(start_handler)

    announce_handler = CommandHandler(
        'announce', announce, filters.ChatType.PRIVATE & filters.User(ADMIN_USER_ID), block=False
    )
    application.add_handler(announce_handler)

    set_title_handler = CommandHandler(
        'set_title', set_title, filters.ChatType.PRIVATE & filters.User(ADMIN_USER_ID), block=False
    )
    application.add_handler(set_title_handler)

    help_handler = CommandHandler('help', help_command, block=False)
    application.add_handler(help_handler)

    chat_update_handler = MessageHandler(filters.ChatType.GROUPS, update_chat_data, block=True)
    application.add_handler(chat_update_handler, -1)

    framed_data_handler = MessageHandler(filters.ChatType.GROUPS & FRAMED_FILTER, new_framed_data, block=True)
    application.add_handler(framed_data_handler)

    episode_data_handler = MessageHandler(filters.ChatType.GROUPS & EPISODE_FILTER, new_episode_data, block=True)
    application.add_handler(episode_data_handler)

    stats_handler = CommandHandler('stats', stats, block=False)
    application.add_handler(stats_handler)

    top_handler = CommandHandler('top', top, block=False)
    application.add_handler(top_handler)

    inline_top_handler = CallbackQueryHandler(inline_top, pattern=r'^{"top": [\d]+}$', block=False)
    application.add_handler(inline_top_handler)

    return application


if __name__ == '__main__':
    build_application().run_polling()
