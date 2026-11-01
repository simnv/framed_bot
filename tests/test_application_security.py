"""Opt-in application regressions against disposable PostgreSQL; no Telegram API."""

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, check=True):
    result = subprocess.run(['docker', *map(str, args)], capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        pytest.fail(f'Docker {args[0]} failed (output suppressed)')
    return result


@pytest.fixture(scope='module')
def isolated_postgres():
    if os.environ.get('FRAMED_APP_TEST') != '1':
        pytest.skip('Opt-in disposable Docker application integration')
    suffix = uuid.uuid4().hex[:10]
    network, db = f'framed-fix-{suffix}-net', f'framed-fix-{suffix}-db'
    try:
        docker('network', 'create', '--internal', network)
        docker(
            'run',
            '-d',
            '--name',
            db,
            '--network',
            network,
            '--network-alias',
            'db',
            '--memory',
            '256m',
            '--tmpfs',
            '/var/lib/postgresql',
            '-e',
            'POSTGRES_USER=synthetic',
            '-e',
            'POSTGRES_DB=synthetic',
            '-e',
            'POSTGRES_PASSWORD=synthetic-only',
            'postgres:18.6-trixie',
        )
        deadline = time.monotonic() + 60
        while docker('exec', db, 'pg_isready', '-U', 'synthetic', '-d', 'synthetic', check=False).returncode:
            if time.monotonic() > deadline:
                pytest.fail('Isolated PostgreSQL did not become ready')
            time.sleep(0.2)
        yield network, suffix
    finally:
        docker('rm', '-f', db, check=False)
        docker('network', 'rm', network, check=False)


@pytest.mark.parametrize('scenario', ['profile', 'duplicates', 'stats', 'announce', 'migrations', 'answers'])
def test_application_security(isolated_postgres, scenario):
    network, suffix = isolated_postgres
    image = os.environ.get('FRAMED_TEST_IMAGE', 'framed-bot:local-validation')
    name = f'framed-fix-{suffix}-{scenario}'
    mounts = []
    # Mount only public application source. Never expose the checkout's environment or secrets.
    for filename in (
        'main.py',
        'reaction_delivery.py',
        'rich_tables.py',
        'stats.py',
        'top_type.py',
        'config.py',
        'safe_logging.py',
        'models',
        'answers',
        'tests/application_security_probe.py',
    ):
        mounts.extend(['-v', f'{ROOT / filename}:/app/{filename}:ro'])
    try:
        result = docker(
            'run',
            '--rm',
            '--name',
            name,
            '--network',
            network,
            '--memory',
            '600m',
            '-e',
            'BOT_TOKEN=123:synthetic-only',
            '-e',
            'ADMIN_USER_ID=1',
            '-e',
            'DB_CONNECTION_STRING=postgresql+asyncpg://synthetic:synthetic-only@db:5432/synthetic',
            '-e',
            'PYTHONPATH=/app',
            *mounts,
            image,
            'python',
            '/app/tests/application_security_probe.py',
            scenario,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout.strip())
    finally:
        docker('rm', '-f', name, check=False)
