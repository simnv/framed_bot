"""Authenticated database/schema and persistence readiness; never calls Telegram."""
import asyncio
import os
from pathlib import Path


async def check():
    from sqlalchemy import text
    from models.db import engine
    try:
        async with asyncio.timeout(8):
            async with engine.connect() as conn:
                await conn.execute(text('SELECT 1 FROM "user" LIMIT 1'))
                await conn.execute(text('SELECT 1 FROM framed_result LIMIT 1'))
                await conn.execute(text('SELECT 1 FROM episode_result LIMIT 1'))
        return os.access(Path(os.environ.get('BOT_DATA_PATH', 'bot_data')).parent, os.W_OK)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    try:
        ready = asyncio.run(check())
    except Exception:
        ready = False
    raise SystemExit(0 if ready else 1)
