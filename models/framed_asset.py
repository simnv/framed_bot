from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class FramedAsset(Base):
    """One day of a Framed mode (titleshot or poster) from core.framed.wtf. NULL ids mean the day returned 404."""

    __tablename__ = 'framed_asset'
    __table_args__ = (CheckConstraint("mode IN ('titleshot', 'poster')", name='ck_framed_asset_mode'),)

    mode: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    classic_day: Mapped[int | None] = mapped_column(Integer, index=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    imdb_id: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
