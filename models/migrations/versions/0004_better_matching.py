"""Store Framed mode assets, allow the site and visual statuses, and match every non-manual round again.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | Sequence[str] | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "match_status IN ('auto', 'low_confidence', 'manual', 'unmatched')"
NEW_STATUSES = "match_status IN ('site', 'auto', 'visual', 'low_confidence', 'unmatched', 'manual')"


def upgrade() -> None:
    op.create_table(
        'framed_asset',
        sa.Column('mode', sa.Text(), nullable=False),
        sa.Column('day', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('classic_day', sa.Integer(), nullable=True),
        sa.Column('tmdb_id', sa.Integer(), nullable=True),
        sa.Column('imdb_id', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("mode IN ('titleshot', 'poster')", name='ck_framed_asset_mode'),
        sa.PrimaryKeyConstraint('mode', 'day'),
    )
    op.create_index('ix_framed_asset_classic_day', 'framed_asset', ['classic_day'])
    op.drop_constraint('ck_game_answer_match_status', 'game_answer', type_='check')
    op.create_check_constraint('ck_game_answer_match_status', 'game_answer', NEW_STATUSES)
    op.execute(
        'UPDATE game_answer SET title_id = NULL, match_status = NULL, updated_at = now() '
        "WHERE match_status IS DISTINCT FROM 'manual'"
    )


def downgrade() -> None:
    # The old CHECK has no site or visual status; those rows are matched again by the old code.
    op.execute(
        'UPDATE game_answer SET title_id = NULL, match_status = NULL, updated_at = now() '
        "WHERE match_status IN ('site', 'visual')"
    )
    op.drop_constraint('ck_game_answer_match_status', 'game_answer', type_='check')
    op.create_check_constraint('ck_game_answer_match_status', 'game_answer', OLD_STATUSES)
    op.drop_index('ix_framed_asset_classic_day', table_name='framed_asset')
    op.drop_table('framed_asset')
