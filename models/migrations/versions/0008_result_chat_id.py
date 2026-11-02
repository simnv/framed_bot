"""Store the chat of each result and allow one result per chat, user and round.

Every result before this revision came from one group, so the backfill sets its id on all rows.
The unique key moves from (user_id, framed_round) to (chat_id, user_id, framed_round). Production
names of the old keys: framed_result_user_id_framed_round_key, episode_result_user_id_framed_round_key.
Production had 8735 Framed rows and 951 Episode rows on 2026-11-02. No foreign key to "group": a group
row is removed when the bot leaves, and its results stay.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: str | Sequence[str] | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Вастрик.ПопКорн: the only group with results before this revision.
BACKFILL_CHAT_ID = -1001390409915
TABLES = ('framed_result', 'episode_result')


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column('chat_id', sa.BigInteger(), nullable=True))
        op.execute(f'UPDATE {table} SET chat_id = {BACKFILL_CHAT_ID}')  # noqa: S608 - fixed table names
        op.alter_column(table, 'chat_id', nullable=False)
        op.create_index(f'ix_{table}_chat_id', table, ['chat_id'])
        op.drop_constraint(f'{table}_user_id_framed_round_key', table, type_='unique')
        op.create_unique_constraint(
            f'{table}_chat_id_user_id_framed_round_key', table, ['chat_id', 'user_id', 'framed_round']
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_constraint(f'{table}_chat_id_user_id_framed_round_key', table, type_='unique')
        # A user who posted one round in two chats has two rows. The old key allows one: keep the first.
        keep = f'SELECT min(id) FROM {table} GROUP BY user_id, framed_round'  # noqa: S608 - fixed table names
        op.execute(f'DELETE FROM {table} WHERE id NOT IN ({keep})')  # noqa: S608
        op.create_unique_constraint(f'{table}_user_id_framed_round_key', table, ['user_id', 'framed_round'])
        op.drop_index(f'ix_{table}_chat_id', table_name=table)
        op.drop_column(table, 'chat_id')
