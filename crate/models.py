"""SQLAlchemy models. These are the source of truth for the schema; Alembic
migrations are generated from them.

Column names follow the agreed data model (``genres_json``, ``artist_ids``);
the Python attributes drop the suffix where the type already says it.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Status(enum.Enum):
    pending = "pending"
    sorted = "sorted"
    skipped = "skipped"


# create_constraint stays off: the CHECK is declared explicitly on the table
# instead, because Alembic autogenerate cannot compare an Enum-generated one and
# would emit a spurious drop on every revision.
StatusType = Enum(
    Status,
    name="status",
    native_enum=False,
    length=16,
    validate_strings=True,
    values_callable=lambda e: [m.value for m in e],
)

STATUS_VALUES = ", ".join(f"'{s.value}'" for s in Status)


class Track(Base):
    __tablename__ = "tracks"

    track_id: Mapped[str] = mapped_column(Text, primary_key=True)
    artist_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    artist_name: Mapped[str] = mapped_column(Text, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # NULL until the artists behind the track have been fetched; [] means the
    # artists are known and carry no genres. none_as_null keeps a Python None
    # written through the ORM as a real SQL NULL - by default JSON stores it as
    # the JSON value 'null', which no `IS NULL` query would ever find.
    genres: Mapped[list[str] | None] = mapped_column(
        "genres_json", JSON(none_as_null=True)
    )
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_tracks_added_at", "added_at"),)


class Playlist(Base):
    __tablename__ = "playlists"

    playlist_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    hotkey: Mapped[str | None] = mapped_column(Text, unique=True)


class Decision(Base):
    __tablename__ = "decisions"

    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.track_id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[Status] = mapped_column(StatusType, nullable=False)
    playlist_id: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint(f"status IN ({STATUS_VALUES})", name="status_values"),
        CheckConstraint(
            "(status = 'sorted') = (playlist_id IS NOT NULL)",
            name="sorted_has_playlist",
        ),
        Index("ix_decisions_status", "status"),
    )


class Artist(Base):
    __tablename__ = "artists"

    artist_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    genres: Mapped[list[str]] = mapped_column("genres_json", JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
