from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class GameAnswer(Base):
    """The answer of one game round. match_status is NULL until the first TMDB lookup ends."""

    __tablename__ = 'game_answer'
    __table_args__ = (
        CheckConstraint("game IN ('framed', 'episode')", name='ck_game_answer_game'),
        CheckConstraint(
            "match_status IN ('site', 'auto', 'visual', 'low_confidence', 'unmatched', 'manual')",
            name='ck_game_answer_match_status',
        ),
    )

    game: Mapped[str] = mapped_column(Text, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    source_title: Mapped[str] = mapped_column(Text)
    title_id: Mapped[int | None] = mapped_column(ForeignKey('title.id'), index=True)
    match_status: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
