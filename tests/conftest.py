import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ['BOT_TOKEN'] = '123:placeholder-test-token'
os.environ['DB_CONNECTION_STRING'] = 'postgresql+asyncpg://placeholder:placeholder@localhost:5432/framed_bot_test'
os.environ['ADMIN_USER_ID'] = '1'
os.environ.pop('BOT_TOKEN_FILE', None)
os.environ.pop('DB_CONNECTION_STRING_FILE', None)
