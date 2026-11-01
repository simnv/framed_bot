"""Clear low-confidence TMDB matches so the startup backfill settles old ties by votes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0005'
down_revision: str | Sequence[str] | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        'UPDATE game_answer SET title_id = NULL, match_status = NULL, updated_at = now() '
        "WHERE match_status = 'low_confidence'"
    )


def downgrade() -> None:
    """The old matches cannot be restored; the rows are matched again instead."""
