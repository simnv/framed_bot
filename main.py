# ruff: noqa: E402
# Configure redaction before importing configuration, models, or Telegram.
import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

import httpx

from safe_logging import configure_logging

configure_logging()

from config import ADMIN_USER_ID, BOT_DATA_PATH, BOT_TOKEN, CLIP_MODEL_PATH, DB_CONNECTION_STRING, TMDB_API_KEY

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
from answers.assets import AssetFetcher
from answers.embedder import ProcessEmbedder
from answers.images import ImageFetcher
from answers.pipeline import Resolver, TmdbVisualSource
from answers.ratelimit import API_RATE, CDN_RATE, SITE_RATE, RateLimiter
from answers.scraper import BundleEntry, fetch_entries
from answers.store import DbAnswerStore
from answers.sync import AnswerSync
from answers.tmdb import TmdbClient, TmdbError
from insight import Choice, Dimension, game_insight, hard_titles, league, league_choices
from models import FramedResult, User
from models.episode_result import EpisodeResult
from models.group import Group
from models.migrate import upgrade_database
from models.top import top_results
from reaction_delivery import ReactionDelivery, ReactionIntent
from rich_tables import (
    TOP_WIN_LABELS,
    RichMessage,
    enrich_top_profiles,
    render_hard_titles,
    render_league,
    render_league_picker,
    render_stats,
    render_top,
    send_rich_message,
)
from stats import get_user_stats
from top_type import TopType

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
# CPU limit of the bot service is 1.0, so inference uses one thread.
CLIP_THREADS = 1
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
    game = request.game
    if request.up_to_round is not None:
        await application.answer_sync.sync(game, request.up_to_round)
        return

    async def newest_round() -> int | None:
        return await application.answer_store.max_result_round(game)

    await application.answer_sync.sync_newest(game, newest_round)


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


@dataclass(frozen=True, slots=True)
class Limiters:
    """One limiter for each host group; every request of the answer services waits for one of them."""

    site: RateLimiter  # framed.wtf, episode.wtf and core.framed.wtf
    api: RateLimiter  # api.themoviedb.org
    cdn: RateLimiter  # image.tmdb.org


def default_limiters() -> Limiters:
    return Limiters(RateLimiter(SITE_RATE), RateLimiter(API_RATE), RateLimiter(CDN_RATE))


def build_resolver(client: httpx.AsyncClient, tmdb: TmdbClient, limiters: Limiters) -> Resolver:
    """Text matching always; the visual tie-break only when the CLIP model file exists. The child starts lazily."""
    model = Path(CLIP_MODEL_PATH)
    if not model.is_file():
        logging.warning('The CLIP model file is missing; tied matches keep the text choice')
        return Resolver(tmdb.search)
    fetcher = ImageFetcher(client, limiters.site, limiters.cdn)
    return Resolver(tmdb.search, TmdbVisualSource(fetcher, tmdb), ProcessEmbedder(model, threads=CLIP_THREADS))


def start_answers(
    application: ReactionApplication, client: httpx.AsyncClient | None = None, limiters: Limiters | None = None
) -> None:
    """Create the answer services and schedule the startup backfill. Startup does not wait for it."""
    client = client or httpx.AsyncClient()
    limiters = limiters or default_limiters()
    application.http_client = client
    resolver = None
    if TMDB_API_KEY:
        application.tmdb = TmdbClient(TMDB_API_KEY, client, limiters.api)
        resolver = build_resolver(client, application.tmdb, limiters)
    else:
        logging.warning('TMDB_API_KEY is not set; the bot stores game answers without title data')

    async def scrape(game: Game) -> list[BundleEntry]:
        return await fetch_entries(game, client, limiters.site)

    async def alert(text: str) -> None:
        await alert_admin(application.bot, text)

    application.answer_sync = AnswerSync(
        application.answer_store,
        scrape,
        application.tmdb,
        alert,
        resolver=resolver,
        assets=AssetFetcher(client, limiters.site),
    )
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


@dataclass(frozen=True, slots=True)
class StatsOwner:
    """The user who asked for /stats, in the shape that enrich_top_profiles reads."""

    user_id: int
    name: str
    score: None = None


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
    today = utc_today()
    insights = {game: await game_insight(effective_user.id, game, today) for game in GAMES}
    (owner,) = await enrich_top_profiles(
        context.bot,
        [StatsOwner(effective_user.id, effective_user.full_name)],
        group_chat_id=effective_chat.id if effective_chat.type in ('group', 'supergroup') else None,
    )

    # The message is long, so the bot keeps it. The first line names the owner of the stats.
    await send_rich_message(
        context.bot,
        effective_chat.id,
        render_stats(framed_stats, episode_stats, owner=owner, insights=insights),
        reply_to_message_id=message.id,
    )


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
    if game is None or re.fullmatch(r'[0-9]{1,10}', args[1]) is None or re.fullmatch(r'[0-9]{1,9}', args[2]) is None:
        await context.bot.send_message(chat_id=chat.id, text=set_title_usage)
        return
    round_number, tmdb_id = int(args[1]), int(args[2])
    if not 1 <= round_number <= 2_147_483_647 or tmdb_id < 1:
        await context.bot.send_message(chat_id=chat.id, text=set_title_usage)
        return
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


TOP_GAME_NAMES: dict[Game, str] = {'framed': 'Framed', 'episode': 'Episode'}
# The same emoji as in the shared results. The active metric button starts with the emoji of the shown game.
TOP_GAME_EMOJI: dict[Game, str] = {'framed': '🎥', 'episode': '📺'}
TOP_CALLBACK_PATTERN = r'^top:(framed|episode):([1-4])$'
# Buttons of the JSON format, for example {"top": 2}, stay in old messages. The bot ignores them.
LEGACY_TOP_CALLBACK_PATTERN = r'^{"top": [\d]+}$'
# The metadata screens of /top: two pickers, one league of each picker, and the hard titles.
TOP_SCREEN_LABELS: dict[str, str] = {'genres': 'Жанры', 'decades': 'Десятилетия', 'hard': 'Сложные'}
TOP_SCREEN_PATTERN = r'^top:(framed|episode):(genres|decades|hard|genre:[0-9]{1,9}|decade:[0-9]{4})$'
PICKER_COLUMNS = 3
BACK_LABEL = '← Назад'


def top_callback_data(game: Game, top_type: TopType) -> str:
    return f'top:{game}:{top_type.value}'


def parse_top_callback(data: str) -> tuple[Game, TopType] | None:
    match = re.fullmatch(TOP_CALLBACK_PATTERN, data)
    if match is None:
        return None
    game = parse_game(match.group(1))
    return (game, TopType(int(match.group(2)))) if game else None


def top_reply_markup(game: Game, top_type: TopType) -> InlineKeyboardMarkup:
    """Metrics on the first row, one full-width game switch below.

    Telegram gives the buttons of a row equal widths. Two game buttons over four metric
    buttons looked like two groups, so the game is one button that names the other game.
    """
    metric_labels = {
        TopType.TOP_SCORE: 'Очки',
        TopType.TOP_FRAME: 'Кадры',
        TopType.TOP_WIN: TOP_WIN_LABELS[game],
        TopType.TOP_ROUNDS: 'Участия',
    }
    other: Game = 'episode' if game == 'framed' else 'framed'
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f'{TOP_GAME_EMOJI[game]} {label}' if _type == top_type else label,
                    callback_data=top_callback_data(game, _type),
                )
                for _type, label in metric_labels.items()
            ],
            [
                InlineKeyboardButton(label, callback_data=f'top:{game}:{screen}')
                for screen, label in TOP_SCREEN_LABELS.items()
            ],
            [
                InlineKeyboardButton(
                    f'{TOP_GAME_EMOJI[other]} Переключить на {TOP_GAME_NAMES[other]}',
                    callback_data=top_callback_data(other, top_type),
                )
            ],
        ]
    )


def shown_top(message: TelegramMessage) -> tuple[Game, TopType] | None:
    """Find the leaderboard that a message shows from its one metric button marked with the game emoji."""
    markup = message.reply_markup
    if markup is None:
        return None
    shown = set()
    for row in markup.inline_keyboard:
        for button in row:
            parsed = parse_top_callback(button.callback_data) if isinstance(button.callback_data, str) else None
            marked = any(button.text.startswith(f'{emoji} ') for emoji in TOP_GAME_EMOJI.values())
            if not marked or 'Переключить' in button.text:
                continue
            if parsed is None or not button.text.startswith(f'{TOP_GAME_EMOJI[parsed[0]]} '):
                return None
            shown.add(parsed)
    return shown.pop() if len(shown) == 1 else None


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    effective_chat = update.effective_chat
    message = update.message
    if effective_chat is None or message is None:
        return
    game: Game = 'framed'
    top_type = TopType.TOP_SCORE
    results = await top_results(game, top_type)
    profiles = await enrich_top_profiles(
        context.bot,
        results,
        group_chat_id=effective_chat.id if effective_chat.type in ('group', 'supergroup') else None,
    )
    await send_rich_message(
        context.bot,
        effective_chat.id,
        render_top(game, top_type, profiles),
        reply_to_message_id=message.id,
        reply_markup=top_reply_markup(game, top_type),
    )


async def inline_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.data is None:
        return
    selected = parse_top_callback(query.data)
    query_message = query.message
    if selected is None or not isinstance(query_message, TelegramMessage) or shown_top(query_message) == selected:
        await query.answer()
        return
    game, top_type = selected
    results = await top_results(game, top_type)
    profiles = await enrich_top_profiles(
        context.bot,
        results,
        group_chat_id=query_message.chat_id if query_message.chat.type in ('group', 'supergroup') else None,
    )
    try:
        await edit_rich_message(
            context, query_message, render_top(game, top_type, profiles), top_reply_markup(game, top_type)
        )
    finally:
        await query.answer()


async def ignore_legacy_top(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is not None:
        await query.answer()


def back_markup(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(BACK_LABEL, callback_data=callback_data)]])


def picker_markup(game: Game, dimension: Dimension, choices: Sequence[Choice]) -> InlineKeyboardMarkup:
    """Only leagues that have a board get a button. Back opens the main score board of the game."""
    buttons = [
        InlineKeyboardButton(choice.label, callback_data=f'top:{game}:{dimension}:{choice.key}') for choice in choices
    ]
    rows = [buttons[start : start + PICKER_COLUMNS] for start in range(0, len(buttons), PICKER_COLUMNS)]
    rows.append([InlineKeyboardButton(BACK_LABEL, callback_data=top_callback_data(game, TopType.TOP_SCORE))])
    return InlineKeyboardMarkup(rows)


async def top_screen(
    context: ContextTypes.DEFAULT_TYPE, message: TelegramMessage, game: Game, screen: str
) -> tuple[RichMessage, InlineKeyboardMarkup]:
    today = utc_today()
    match screen.split(':'):
        case ['genres' | 'decades' as picker]:
            dimension: Dimension = 'genre' if picker == 'genres' else 'decade'
            choices = await league_choices(game, dimension, today)
            return render_league_picker(game, dimension, choices), picker_markup(game, dimension, choices)
        case ['genre' | 'decade' as dimension, key]:
            board = await league(game, dimension, int(key), today)
            back = back_markup(f'top:{game}:{dimension}s')
            if board is None:
                # The board can stop to exist between two taps. The picker then shows the boards that exist.
                choices = await league_choices(game, dimension, today)
                return render_league_picker(game, dimension, choices), picker_markup(game, dimension, choices)
            profiles = await enrich_top_profiles(
                context.bot,
                board.rows,
                group_chat_id=message.chat_id if message.chat.type in ('group', 'supergroup') else None,
            )
            return render_league(game, board, profiles), back
        case _:
            titles = await hard_titles(game, today)
            return render_hard_titles(game, titles), back_markup(top_callback_data(game, TopType.TOP_SCORE))


async def edit_rich_message(
    context: ContextTypes.DEFAULT_TYPE, message: TelegramMessage, rich: RichMessage, markup: InlineKeyboardMarkup
) -> None:
    try:
        # PTB's typed edit_message_text still requires text; the public API bridge
        # allows the new rich_message parameter without sending a plain text field.
        await context.bot.do_api_request(
            'editMessageText',
            api_kwargs={
                'chat_id': message.chat_id,
                'message_id': message.message_id,
                'rich_message': rich,
                'reply_markup': markup,
            },
        )
    except BadRequest as error:
        # Two fast taps can ask for the same screen. Telegram then rejects the second edit.
        if 'message is not modified' not in str(error).lower():
            raise


async def inline_top_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.data is None:
        return
    match = re.fullmatch(TOP_SCREEN_PATTERN, query.data)
    game = parse_game(match.group(1)) if match else None
    query_message = query.message
    if match is None or game is None or not isinstance(query_message, TelegramMessage):
        await query.answer()
        return
    try:
        rich, markup = await top_screen(context, query_message, game, match.group(2))
        await edit_rich_message(context, query_message, rich, markup)
    finally:
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
    try:
        await reaction_delivery_for(application).stop()
    finally:
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

    inline_top_handler = CallbackQueryHandler(inline_top, pattern=TOP_CALLBACK_PATTERN, block=False)
    application.add_handler(inline_top_handler)

    inline_top_screen_handler = CallbackQueryHandler(inline_top_screen, pattern=TOP_SCREEN_PATTERN, block=False)
    application.add_handler(inline_top_screen_handler)

    legacy_top_handler = CallbackQueryHandler(ignore_legacy_top, pattern=LEGACY_TOP_CALLBACK_PATTERN, block=False)
    application.add_handler(legacy_top_handler)

    return application


if __name__ == '__main__':
    build_application().run_polling()
