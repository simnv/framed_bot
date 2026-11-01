"""Replace HTML entities in stored TMDB names with plain text.

TMDB returns some names with HTML entities, for example 'Sci-Fi &amp; Fantasy'. The parser now
unescapes them; this revision fixes the rows that are already stored.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0007'
down_revision: str | Sequence[str] | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# &amp; comes last, so '&amp;lt;' becomes '&lt;' and not '<'.
ENTITIES: tuple[tuple[str, str], ...] = (
    ('&lt;', '<'),
    ('&gt;', '>'),
    ('&quot;', '"'),
    ('&#39;', "''"),
    ('&#039;', "''"),
    ('&apos;', "''"),
    ('&amp;', '&'),
)
COLUMNS: tuple[tuple[str, str], ...] = (
    ('genre', 'name'),
    ('person', 'name'),
    ('company', 'name'),
    ('title', 'name'),
    ('title', 'original_name'),
)


def _unescaped(column: str) -> str:
    expression = column
    for entity, text in ENTITIES:
        expression = f"replace({expression}, '{entity}', '{text}')"
    return expression


def upgrade() -> None:
    for table, column in COLUMNS:
        op.execute(f"UPDATE {table} SET {column} = {_unescaped(column)} WHERE {column} LIKE '%&%;%'")  # noqa: S608


def downgrade() -> None:
    """The escaped names are not restored. Earlier releases show the plain names correctly."""
