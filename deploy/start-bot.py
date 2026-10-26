"""Refuse invalid persisted roles before starting Telegram polling. No Telegram imports here."""
import asyncio
import os
from pathlib import Path

from sqlalchemy import text

from models.db import engine


async def authorized():
    policy = Path(__file__).with_name('role-policy.sql').read_text().split(';', 1)[1].strip()
    try:
        async with asyncio.timeout(8):
            async with engine.connect() as connection:
                await connection.execute(text('SET search_path = pg_catalog'))
                role = await connection.scalar(text('SELECT current_user'))
                return role == 'framed' and await connection.scalar(text(policy)) is True
    finally:
        await engine.dispose()


async def wait_for_roles():
    for _ in range(60):
        try:
            if await authorized():
                return True
        except Exception:
            pass
        await asyncio.sleep(2)
    return False


if __name__ == '__main__':
    if not asyncio.run(wait_for_roles()):
        raise SystemExit('Database authorization readiness failed; details suppressed')
    os.execvp('python', ['python', 'main.py'])
