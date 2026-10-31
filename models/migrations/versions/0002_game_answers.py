"""Game answers and TMDB title metadata. Adds new tables only.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'title',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('original_name', sa.Text(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('runtime', sa.Integer(), nullable=True),
        sa.Column('countries', postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column('imdb_id', sa.Text(), nullable=True),
        sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('movie', 'tv')", name='ck_title_kind'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kind', 'tmdb_id'),
    )
    for table in ('genre', 'person', 'company'):
        op.create_table(
            table,
            sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
            sa.Column('name', sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
    for table, column, target in (('title_genre', 'genre_id', 'genre'), ('title_company', 'company_id', 'company')):
        op.create_table(
            table,
            sa.Column('title_id', sa.Integer(), nullable=False),
            sa.Column(column, sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['title_id'], ['title.id']),
            sa.ForeignKeyConstraint([column], [f'{target}.id']),
            sa.PrimaryKeyConstraint('title_id', column),
        )
    op.create_table(
        'title_person',
        sa.Column('title_id', sa.Integer(), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('order', sa.Integer(), nullable=False),
        sa.CheckConstraint("role IN ('director', 'creator', 'cast')", name='ck_title_person_role'),
        sa.ForeignKeyConstraint(['person_id'], ['person.id']),
        sa.ForeignKeyConstraint(['title_id'], ['title.id']),
        sa.PrimaryKeyConstraint('title_id', 'person_id', 'role'),
    )
    op.create_table(
        'game_answer',
        sa.Column('game', sa.Text(), nullable=False),
        sa.Column('round', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('source_title', sa.Text(), nullable=False),
        sa.Column('title_id', sa.Integer(), nullable=True),
        sa.Column('match_status', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("game IN ('framed', 'episode')", name='ck_game_answer_game'),
        sa.CheckConstraint(
            "match_status IN ('auto', 'low_confidence', 'manual', 'unmatched')", name='ck_game_answer_match_status'
        ),
        sa.ForeignKeyConstraint(['title_id'], ['title.id']),
        sa.PrimaryKeyConstraint('game', 'round'),
    )
    op.create_index('ix_game_answer_title_id', 'game_answer', ['title_id'])


def downgrade() -> None:
    # The new tables hold only scraped and TMDB data; result tables stay unchanged.
    op.drop_index('ix_game_answer_title_id', table_name='game_answer')
    for table in ('game_answer', 'title_person', 'title_company', 'title_genre', 'company', 'person', 'genre', 'title'):
        op.drop_table(table)
