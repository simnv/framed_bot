"""Offline Alembic checks: no database and no network."""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEW_TABLES = ('company', 'game_answer', 'genre', 'person', 'title', 'title_company', 'title_genre', 'title_person')


def offline_sql(*args: str) -> str:
    # conftest.py puts placeholder settings in os.environ; offline mode never connects.
    result = subprocess.run(  # noqa: S603 - fixed Alembic arguments
        [sys.executable, '-m', 'alembic', *args, '--sql'],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def test_revision_0002_adds_new_tables_only() -> None:
    sql = offline_sql('upgrade', '0001:0002')

    assert sorted(re.findall(r'^CREATE TABLE (\w+)', sql, re.MULTILINE)) == list(NEW_TABLES)
    assert 'ALTER TABLE' not in sql
    assert 'DROP' not in sql
    assert "CHECK (match_status IN ('auto', 'low_confidence', 'manual', 'unmatched'))" in sql
    assert "CHECK (game IN ('framed', 'episode'))" in sql
    assert 'PRIMARY KEY (game, round)' in sql
    assert 'UNIQUE (kind, tmdb_id)' in sql
    assert "UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001'" in sql


def test_revision_0002_downgrade_drops_only_new_tables() -> None:
    sql = offline_sql('downgrade', '0002:0001')

    assert sorted(re.findall(r'^DROP TABLE (\w+)', sql, re.MULTILINE)) == list(NEW_TABLES)


def test_revision_0003_clears_low_confidence_matches() -> None:
    sql = offline_sql('upgrade', '0002:head')

    assert (
        "UPDATE game_answer SET title_id = NULL, match_status = NULL, updated_at = now() "
        "WHERE match_status = 'low_confidence'"
    ) in sql
    assert "UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002'" in sql
    assert 'DROP' not in sql


def test_models_register_new_tables() -> None:
    from models import Base

    assert set(NEW_TABLES) <= set(Base.metadata.tables)
