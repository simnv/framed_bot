from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import CommandHandler, ContextTypes

import main


@pytest.mark.parametrize(
    ('chat_type', 'user_id', 'allowed'),
    [
        ('private', 1, True),
        ('private', 2, False),
        ('group', 1, False),
        ('group', 2, False),
    ],
)
def test_announcement_route_requires_private_administrator(chat_type, user_id, allowed):
    application = main.build_application()
    handler = next(
        handler
        for handlers in application.handlers.values()
        for handler in handlers
        if isinstance(handler, CommandHandler) and 'announce' in handler.commands
    )
    message = Message(
        1, datetime.now(UTC), Chat(10, chat_type), from_user=User(user_id, 'Synthetic', False), text='/announce notice'
    )
    assert bool(handler.filters.check_update(Update(1, message=message))) is allowed


@pytest.mark.asyncio
async def test_announce_uses_shared_delivery_for_every_group(monkeypatch):
    monkeypatch.setattr(main.Group, 'get_all', AsyncMock(return_value=[SimpleNamespace(id=-1), SimpleNamespace(id=-2)]))
    send = AsyncMock()

    async def deliver(chat_id, callback):
        await callback()
        return 'failed' if chat_id == -1 else 'sent'

    monkeypatch.setattr(main, 'deliver', deliver)
    message = Message(1, datetime.now(UTC), Chat(1, 'private'), text='/announce Две строки\nТекст')
    context = ContextTypes.DEFAULT_TYPE(main.build_application())
    monkeypatch.setattr(type(context.bot), 'send_message', send)

    await main.announce(Update(1, message=message), context)

    assert [call.kwargs for call in send.await_args_list] == [
        {'chat_id': -1, 'text': 'Две строки\nТекст'},
        {'chat_id': -2, 'text': 'Две строки\nТекст'},
    ]
