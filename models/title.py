from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Title(Base):
    """A TMDB movie or TV show. Several game rounds can share one title."""

    __tablename__ = 'title'
    __table_args__ = (
        UniqueConstraint('kind', 'tmdb_id'),
        CheckConstraint("kind IN ('movie', 'tv')", name='ck_title_kind'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer)
    # Minutes; for TV the first episode_run_time value.
    runtime: Mapped[int | None] = mapped_column(Integer)
    countries: Mapped[list[str]] = mapped_column(ARRAY(Text))
    imdb_id: Mapped[str | None] = mapped_column(Text)
    # The full TMDB response keeps the fields that have no column.
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Genre(Base):
    __tablename__ = 'genre'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)


class Person(Base):
    __tablename__ = 'person'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)


class Company(Base):
    __tablename__ = 'company'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(Text)


class TitleGenre(Base):
    __tablename__ = 'title_genre'

    title_id: Mapped[int] = mapped_column(ForeignKey('title.id'), primary_key=True)
    genre_id: Mapped[int] = mapped_column(ForeignKey('genre.id'), primary_key=True)


class TitleCompany(Base):
    __tablename__ = 'title_company'

    title_id: Mapped[int] = mapped_column(ForeignKey('title.id'), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey('company.id'), primary_key=True)


class TitlePerson(Base):
    __tablename__ = 'title_person'
    __table_args__ = (CheckConstraint("role IN ('director', 'creator', 'cast')", name='ck_title_person_role'),)

    title_id: Mapped[int] = mapped_column(ForeignKey('title.id'), primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey('person.id'), primary_key=True)
    role: Mapped[str] = mapped_column(Text, primary_key=True)
    order: Mapped[int] = mapped_column(Integer)
