"""Baseline: the schema that create_all produced before Alembic.

Revision ID: 0001
Revises:
Create Date: 2026-10-31
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0001'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'user',
        sa.Column('id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('full_name', sa.String(), nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'group',
        # create_all gave group.id a sequence; keep it so new and existing databases match.
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    for table in ('framed_result', 'episode_result'):
        op.create_table(
            table,
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.BigInteger(), nullable=False),
            sa.Column('framed_round', sa.Integer(), nullable=False),
            sa.Column('won', sa.Boolean(), nullable=False),
            sa.Column('win_frame', sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['user.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'framed_round'),
        )
        op.create_index(f'ix_{table}_framed_round', table, ['framed_round'])
        op.create_index(f'ix_{table}_user_id', table, ['user_id'])


def downgrade() -> None:
    # Dropping the baseline deletes all bot data.
    # A pre-Alembic release ignores alembic_version, so rollback needs no downgrade.
    raise NotImplementedError('The baseline revision cannot be downgraded')
