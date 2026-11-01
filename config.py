import os
from pathlib import Path
from typing import Final


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == '':
        raise RuntimeError(f'Environment variable {name} is required')
    return value


def _required_secret(name: str) -> str:
    value = _optional_secret(name)
    if value is None:
        raise RuntimeError(f'Environment variable {name} or {name}_FILE is required')
    return value


def _optional_secret(name: str) -> str | None:
    """Read NAME, or the file that NAME_FILE names. Return None when neither is set."""
    value = os.environ.get(name)
    if value:
        return value
    file_name = f'{name}_FILE'
    secret_path = os.environ.get(file_name)
    if not secret_path:
        return None
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
# Optional: without it the bot stores game answers but no TMDB title data.
TMDB_API_KEY: Final = _optional_secret('TMDB_API_KEY')
BOT_DATA_PATH: Final = os.environ.get('BOT_DATA_PATH') or 'bot_data'
# The CLIP model for the visual tie-break; without the file the bot matches by text only.
CLIP_MODEL_PATH: Final = os.environ.get('CLIP_MODEL_PATH') or '/opt/clip/vision_model.onnx'
