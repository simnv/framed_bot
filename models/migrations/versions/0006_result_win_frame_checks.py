"""Reject result rows whose win_frame does not match won.

A won round needs a frame within the game's range. A lost round has no frame.
A CHECK passes on NULL, so the won branch tests win_frame IS NOT NULL explicitly.
Production had no violating rows on 2026-11-01, so the checks validate at once.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0006'
down_revision: str | Sequence[str] | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of answers.FRAMES at this revision. Later changes need a new revision.
CHECKS = (
    ('framed_result', 'ck_framed_result_win_frame', 6),
    ('episode_result', 'ck_episode_result_win_frame', 10),
)


def upgrade() -> None:
    for table, name, frames in CHECKS:
        won_rule = f'won AND win_frame IS NOT NULL AND win_frame BETWEEN 1 AND {frames}'
        rule = f'({won_rule}) OR (NOT won AND win_frame IS NULL)'
        op.create_check_constraint(name, table, rule)


def downgrade() -> None:
    for table, name, _frames in reversed(CHECKS):
        op.drop_constraint(name, table, type_='check')
