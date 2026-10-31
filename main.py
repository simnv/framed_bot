# ruff: noqa: E402
# Configure redaction before importing configuration, models, or Telegram.
import asyncio
import json
import logging
import re
from datetime import timedelta
from typing import Protocol

from safe_logging import configure_logging

configure_logging()

from config import ADMIN_USER_ID, BOT_DATA_PATH, BOT_TOKEN, DB_CONNECTION_STRING

configure_logging((BOT_TOKEN, DB_CONNECTION_STRING))

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

from models import FramedResult, User, init_db
from models.episode_result import EpisodeResult
from models.group import Group
from reaction_delivery import ReactionDelivery, ReactionIntent
from rich_tables import TopType, enrich_top_profiles, render_stats, render_top, send_rich_message
from stats import get_user_stats

framed_pattern = r'Framed #(?P<round>[\d]+)\n🎥(?P<result>(?: 🟥| 🟩| ⬛| ⬛️){6})\n\nhttps:\/\/framed\.wtf'
episode_pattern = r'Episode #(?P<round>[\d]+)\n📺(?P<result>(?: 🟥| 🟩| ⬛| ⬛️){10})\n\nhttps:\/\/episode\.wtf'
saved_result_reaction = ReactionTypeEmoji(ReactionEmoji.WRITING_HAND)
first_frame_saved_result_reaction = ReactionTypeEmoji(ReactionEmoji.TROPHY)
duplicate_result_reaction = ReactionTypeEmoji(ReactionEmoji.GHOST)


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

    async def stop(self) -> None:
        if not self.running:
            await super().stop()
            return
        # PTB.stop waits for queued handlers before post_stop. Release admission
        # backpressure now and begin the drain deadline before that wait.
        self.reaction_delivery.close_admissions()
        draining = asyncio.create_task(self.reaction_delivery.stop(), name='reaction-drain')
        try:
            await super().stop()
        finally:
            await draining


def reaction_delivery_for(application: object) -> ReactionDelivery:
    if not isinstance(application, ReactionApplication):
        raise TypeError('ReactionApplication is required for result delivery')
    return application.reaction_delivery


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
    profiles = await enrich_top_profiles(context.bot, results)
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
    profiles = await enrich_top_profiles(context.bot, results)
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
    await init_db()
    reaction_delivery_for(_application).start()


async def stop_reaction_delivery(application: object) -> None:
    await reaction_delivery_for(application).stop()


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
