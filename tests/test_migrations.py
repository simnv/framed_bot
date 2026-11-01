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
    sql = offline_sql('upgrade', '0002:0003')

    assert (
        'UPDATE game_answer SET title_id = NULL, match_status = NULL, updated_at = now() '
        "WHERE match_status = 'low_confidence'"
    ) in sql
    assert "UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002'" in sql
    assert 'DROP' not in sql


def test_revision_0004_adds_assets_new_statuses_and_resets_non_manual_rows() -> None:
    sql = offline_sql('upgrade', '0003:0004')

    assert re.findall(r'^CREATE TABLE (\w+)', sql, re.MULTILINE) == ['framed_asset']
    assert 'PRIMARY KEY (mode, day)' in sql
    assert "CONSTRAINT ck_framed_asset_mode CHECK (mode IN ('titleshot', 'poster'))" in sql
    assert 'CREATE INDEX ix_framed_asset_classic_day ON framed_asset (classic_day)' in sql
    drop = sql.index('ALTER TABLE game_answer DROP CONSTRAINT ck_game_answer_match_status')
    add = sql.index(
        'ALTER TABLE game_answer ADD CONSTRAINT ck_game_answer_match_status CHECK '
        "(match_status IN ('site', 'auto', 'visual', 'low_confidence', 'unmatched', 'manual'))"
    )
    reset = sql.index(
        'UPDATE game_answer SET title_id = NULL, match_status = NULL, updated_at = now() '
        "WHERE match_status IS DISTINCT FROM 'manual'"
    )
    assert drop < add < reset
    assert 'DROP TABLE' not in sql
    assert "UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003'" in sql


def test_revision_0004_downgrade_restores_the_old_check() -> None:
    sql = offline_sql('downgrade', '0004:0003')

    reset = sql.index(
        'UPDATE game_answer SET title_id = NULL, match_status = NULL, updated_at = now() '
        "WHERE match_status IN ('site', 'visual')"
    )
    old = sql.index(
        'ADD CONSTRAINT ck_game_answer_match_status CHECK '
        "(match_status IN ('auto', 'low_confidence', 'manual', 'unmatched'))"
    )
    assert reset < old < sql.index('DROP TABLE framed_asset')


def test_models_register_new_tables() -> None:
    from models import Base

    assert set(NEW_TABLES) | {'framed_asset'} <= set(Base.metadata.tables)
    check = next(
        constraint
        for constraint in Base.metadata.tables['game_answer'].constraints
        if constraint.name == 'ck_game_answer_match_status'
    )
    assert "'site'" in str(check.sqltext) and "'visual'" in str(check.sqltext)
