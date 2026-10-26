import os
from pathlib import Path
from typing import Final


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == '':
        raise RuntimeError(f'Environment variable {name} is required')
    return value


def _required_secret(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    file_name = f'{name}_FILE'
    secret_path = os.environ.get(file_name)
    if not secret_path:
        raise RuntimeError(f'Environment variable {name} or {file_name} is required')
    try:
        value = Path(secret_path).read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError(f'Cannot read secret file configured by {file_name}') from None
    if not value:
        raise RuntimeError(f'Secret file configured by {file_name} is empty')
    return value


def _required_int_env(name: str) -> int:
    value = _required_env(name)
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f'Environment variable {name} must be an integer') from None


BOT_TOKEN: Final = _required_secret('BOT_TOKEN')
DB_CONNECTION_STRING: Final = _required_secret('DB_CONNECTION_STRING')
ADMIN_USER_ID: Final = _required_int_env('ADMIN_USER_ID')
BOT_DATA_PATH: Final = os.environ.get('BOT_DATA_PATH') or 'bot_data'
