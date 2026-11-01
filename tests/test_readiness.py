from __future__ import annotations

import logging
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import main
from models.episode_result import EpisodeResult
from models.framed_result import FramedResult
from models.user import User

ROOT = Path(__file__).resolve().parents[1]


def load_config(monkeypatch: pytest.MonkeyPatch):
    for name in (
        'BOT_TOKEN',
        'BOT_TOKEN_FILE',
        'DB_CONNECTION_STRING',
        'DB_CONNECTION_STRING_FILE',
        'BOT_DATA_PATH',
        'TMDB_API_KEY',
        'TMDB_API_KEY_FILE',
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('ADMIN_USER_ID', '1')
    return lambda: runpy.run_path(str(ROOT / 'config.py'))


def test_secret_files_and_persistent_data_path(monkeypatch, tmp_path):
    load = load_config(monkeypatch)
    token_file = tmp_path / 'bot_secret'
    db_file = tmp_path / 'db_secret'
    token_file.write_text('placeholder-bot-secret\n')
    db_file.write_text('postgresql+asyncpg://placeholder@localhost/test\n')
    monkeypatch.setenv('BOT_TOKEN_FILE', str(token_file))
    monkeypatch.setenv('DB_CONNECTION_STRING_FILE', str(db_file))
    monkeypatch.setenv('BOT_DATA_PATH', '/data/bot_data')
    config = load()
    assert config['BOT_TOKEN'] == 'placeholder-bot-secret'
    assert config['DB_CONNECTION_STRING'] == 'postgresql+asyncpg://placeholder@localhost/test'
    assert config['BOT_DATA_PATH'] == '/data/bot_data'


def test_existing_environment_and_default_data_path(monkeypatch, tmp_path):
    load = load_config(monkeypatch)
    monkeypatch.setenv('BOT_TOKEN', 'placeholder-env-secret')
    monkeypatch.setenv('DB_CONNECTION_STRING', 'postgresql+asyncpg://placeholder@localhost/test')
    # An explicit environment value wins over an unused secret-file path.
    monkeypatch.setenv('BOT_TOKEN_FILE', str(tmp_path / 'missing'))
    config = load()
    assert config['BOT_TOKEN'] == 'placeholder-env-secret'
    assert config['BOT_DATA_PATH'] == 'bot_data'


def test_tmdb_key_is_optional(monkeypatch):
    load = load_config(monkeypatch)
    monkeypatch.setenv('BOT_TOKEN', 'placeholder-bot-secret')
    monkeypatch.setenv('DB_CONNECTION_STRING', 'postgresql+asyncpg://placeholder@localhost/test')
    assert load()['TMDB_API_KEY'] is None
    monkeypatch.setenv('TMDB_API_KEY', 'placeholder-tmdb-secret')
    assert load()['TMDB_API_KEY'] == 'placeholder-tmdb-secret'


def test_tmdb_key_from_secret_file(monkeypatch, tmp_path):
    load = load_config(monkeypatch)
    monkeypatch.setenv('BOT_TOKEN', 'placeholder-bot-secret')
    monkeypatch.setenv('DB_CONNECTION_STRING', 'postgresql+asyncpg://placeholder@localhost/test')
    secret = tmp_path / 'tmdb_token'
    secret.write_text('placeholder-tmdb-file-secret\n')
    monkeypatch.setenv('TMDB_API_KEY_FILE', str(secret))
    assert load()['TMDB_API_KEY'] == 'placeholder-tmdb-file-secret'


def test_unreadable_tmdb_secret_file_has_safe_error(monkeypatch, tmp_path):
    load = load_config(monkeypatch)
    monkeypatch.setenv('BOT_TOKEN', 'placeholder-bot-secret')
    monkeypatch.setenv('DB_CONNECTION_STRING', 'postgresql+asyncpg://placeholder@localhost/test')
    monkeypatch.setenv('TMDB_API_KEY_FILE', str(tmp_path / 'placeholder-private-filename'))
    with pytest.raises(RuntimeError) as error:
        load()
    assert 'TMDB_API_KEY_FILE' in str(error.value)
    assert 'placeholder-private-filename' not in str(error.value)


@pytest.mark.parametrize('content', ['', '\n'])
def test_empty_secret_file_has_safe_error(monkeypatch, tmp_path, content):
    load = load_config(monkeypatch)
    secret = tmp_path / 'placeholder-private-filename'
    secret.write_text(content)
    monkeypatch.setenv('BOT_TOKEN_FILE', str(secret))
    with pytest.raises(RuntimeError) as error:
        load()
    assert 'BOT_TOKEN' in str(error.value)
    leaked_path = str(secret) in str(error.value)
    assert not leaked_path


def test_missing_secret_file_has_safe_error(monkeypatch, tmp_path):
    load = load_config(monkeypatch)
    monkeypatch.setenv('BOT_TOKEN_FILE', str(tmp_path / 'placeholder-private-filename'))
    with pytest.raises(RuntimeError) as error:
        load()
    assert 'BOT_TOKEN_FILE' in str(error.value)
    assert error.value.__suppress_context__


def test_invalid_admin_id_does_not_reveal_value_in_traceback(monkeypatch):
    load = load_config(monkeypatch)
    monkeypatch.setenv('BOT_TOKEN', 'placeholder-bot-secret')
    monkeypatch.setenv('DB_CONNECTION_STRING', 'postgresql+asyncpg://placeholder@localhost/test')
    marker = 'placeholder-private-admin-value'
    monkeypatch.setenv('ADMIN_USER_ID', marker)
    import traceback

    try:
        load()
    except RuntimeError as error:
        output = ''.join(traceback.format_exception(error))
    else:
        pytest.fail('invalid administrator ID was accepted')
    leaked_value = marker in output
    assert not leaked_value


@pytest.mark.parametrize('model, column', [(User, 'id'), (FramedResult, 'user_id'), (EpisodeResult, 'user_id')])
def test_postgresql_telegram_id_columns_support_large_ids(model, column):
    # SQLite accepts large integers even for INTEGER, so verify PostgreSQL DDL.
    ddl = str(CreateTable(model.__table__).compile(dialect=postgresql.dialect()))
    assert f'{column} BIGINT' in ddl


def test_configured_logging_redacts_http_urls_and_tracebacks():
    formatter = logging.getLogger().handlers[0].formatter
    assert formatter is not None
    marker = '123456789:placeholderTelegramCredential_0123456789'
    db_url = 'postgresql+asyncpg://placeholder:placeholder-db-password@localhost/test'
    try:
        raise RuntimeError(f'failed https://api.telegram.org/bot{marker}/getUpdates {db_url}')
    except RuntimeError:
        record = logging.LogRecord('httpx', logging.ERROR, __file__, 1, 'request %s', (marker,), sys.exc_info())
    output = formatter.format(record)
    leaked = any(
        part in output
        for part in (
            marker,
            db_url,
            'placeholder-db-password',
            'TelegramCredential_0123456789',
        )
    )
    assert not leaked
    assert 'RuntimeError' in output


def test_uncaught_exception_is_redacted():
    marker = 'placeholder-uncaught-credential'
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            'BOT_TOKEN',
            'BOT_TOKEN_FILE',
            'DB_CONNECTION_STRING',
            'DB_CONNECTION_STRING_FILE',
            'ADMIN_USER_ID',
        )
    }
    env.update(
        BOT_TOKEN=marker,
        ADMIN_USER_ID='1',
        DB_CONNECTION_STRING='postgresql+asyncpg://placeholder@localhost/test',
    )
    result = subprocess.run(
        [sys.executable, '-c', 'import main; raise RuntimeError(main.BOT_TOKEN)'],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    leaked = marker in result.stderr
    assert not leaked
    assert 'Error' in result.stderr


def test_tmdb_key_is_redacted_in_logs():
    marker = 'placeholder-tmdb-credential'
    env = {key: value for key, value in os.environ.items() if key not in ('TMDB_API_KEY', 'TMDB_API_KEY_FILE')}
    env.update(
        TMDB_API_KEY=marker,
        BOT_TOKEN=os.environ['BOT_TOKEN'],
        ADMIN_USER_ID='1',
        DB_CONNECTION_STRING='postgresql+asyncpg://placeholder@localhost/test',
    )
    result = subprocess.run(
        [sys.executable, '-c', 'import logging, main; logging.warning("key %s", main.TMDB_API_KEY)'],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    leaked = marker in result.stderr
    assert not leaked
    assert 'key [REDACTED]' in result.stderr


@pytest.mark.asyncio
async def test_application_persists_data_at_configured_path(monkeypatch, tmp_path):
    path = tmp_path / 'persistent-bot-data'
    monkeypatch.setattr(main, 'BOT_DATA_PATH', str(path), raising=False)
    application = main.build_application()
    persistence = application.persistence
    assert persistence is not None
    await persistence.update_bot_data({'stored': 'after-restart'})
    await persistence.flush()
    assert path.is_file()
    restarted = main.build_application()
    assert restarted.persistence is not None
    assert await restarted.persistence.get_bot_data() == {'stored': 'after-restart'}


def test_logging_redacts_decoded_password_and_traceback_filename():
    from safe_logging import RedactingFormatter

    formatter = RedactingFormatter(
        (
            'postgresql+asyncpg://synthetic_user:private%2Fpassword@localhost/test',
            'filename-private-marker',
        )
    )
    try:
        exec(compile("raise RuntimeError('private/password')", '/tmp/filename-private-marker.py', 'exec'))
    except RuntimeError:
        record = logging.LogRecord('database', logging.ERROR, __file__, 1, 'database failed', (), sys.exc_info())
    output = formatter.format(record)
    leaked = any(
        part in output
        for part in (
            'private/password',
            'private%2Fpassword',
            'filename-private-marker',
        )
    )
    assert not leaked
    assert 'RuntimeError' in output
    assert 'database failed' in output


@pytest.mark.parametrize('separator', ["'", '"', ' '], ids=['apostrophe', 'quote', 'space'])
@pytest.mark.parametrize('with_exception', [False, True], ids=['message', 'traceback'])
def test_logging_redacts_whole_registered_password_with_url_delimiters(separator, with_exception):
    from safe_logging import RedactingFormatter

    password = f'private-head{separator}private-tail'
    dsn = f'postgresql+asyncpg://synthetic_user:{password}@localhost/test'
    formatter = RedactingFormatter((dsn,))
    exc_info = None
    if with_exception:
        try:
            raise RuntimeError(f'connection failed {dsn}; password {password}')
        except RuntimeError:
            exc_info = sys.exc_info()
    record = logging.LogRecord('database', logging.ERROR, __file__, 1, 'connection %s', (dsn,), exc_info)
    output = formatter.format(record)
    leaked = any(part in output for part in ('private-head', 'private-tail'))
    assert not leaked
    assert 'connection' in output
    if with_exception:
        assert 'RuntimeError' in output
