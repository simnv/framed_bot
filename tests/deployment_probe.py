"""Container-only integration probe: synthetic credentials, no Telegram API calls."""
import asyncio
from pathlib import Path

from sqlalchemy import func, select, text
from telegram import User as TelegramUser
from telegram.ext import PicklePersistence

from models.db import engine
from models.episode_result import EpisodeResult
from models.framed_result import FramedResult
from models.migrate import upgrade_database
from models.user import User


async def result_counts(user_id):
    async with engine.connect() as conn:
        return [(await conn.execute(select(func.count()).where(model.user_id == user_id))).scalar_one()
                for model in (FramedResult, EpisodeResult)]


async def main():
    await upgrade_database()
    user_id = 5_000_000_123
    await User.update_from_tg_user(TelegramUser(user_id, 'Integration', False, username='integration'))
    assert await FramedResult.save_result(user_id, 123, True, 2)
    assert await EpisodeResult.save_result(user_id, 456, True, 3)
    assert not await FramedResult.save_result(user_id, 123, True, 2)
    assert not await EpisodeResult.save_result(user_id, 456, True, 3)
    user = await User.get(user_id)
    assert user is not None and user.id == user_id
    assert await result_counts(user_id) == [1, 1]
    async with engine.connect() as conn:
        types = (await conn.execute(text("SELECT data_type FROM information_schema.columns WHERE (table_name = 'user' AND column_name = 'id') OR (table_name IN ('framed_result', 'episode_result') AND column_name = 'user_id')"))).scalars().all()
        assert types == ['bigint'] * 3
    persistence = PicklePersistence(filepath='/data/bot_data')
    await persistence.update_bot_data({'probe': user_id})
    await persistence.flush()
    Path('/control/ready').touch()
    while not Path('/control/restarted').exists():
        await asyncio.sleep(.1)
    # The same already-used engine/pool must recover on its first query after restart.
    user = await User.get(user_id)
    assert user is not None and await result_counts(user_id) == [1, 1]
    assert await PicklePersistence(filepath='/data/bot_data').get_bot_data() == {'probe': user_id}
    await engine.dispose()
    print('BIGINT/duplicates/schema/pool restart/persistence passed')


if __name__ == '__main__':
    asyncio.run(main())
