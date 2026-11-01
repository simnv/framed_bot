from datetime import UTC, datetime

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import CommandHandler

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
