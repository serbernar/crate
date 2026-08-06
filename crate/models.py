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
    Integer,
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
    # As Spotify sends it: "1998", "1998-05" or "1998-05-21". Precision varies
    # per release, so the raw string is kept and the year read off the front.
    release_date: Mapped[str | None] = mapped_column(Text)
    popularity: Mapped[int | None] = mapped_column(Integer)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_tracks_added_at", "added_at"),)

    @property
    def release_year(self) -> int | None:
        try:
            return int((self.release_date or "")[:4])
        except ValueError:
            return None


class Playlist(Base):
    __tablename__ = "playlists"

    playlist_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    hotkey: Mapped[str | None] = mapped_column(Text, unique=True)


class Decision(Base):
    """Where a track stands: still to look at, sorted, or deliberately skipped.

    Which playlists a sorted track goes to lives in Assignment - a track can
    belong to several at once (a song can be both gym and party).
    """

    __tablename__ = "decisions"

    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.track_id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[Status] = mapped_column(StatusType, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint(f"status IN ({STATUS_VALUES})", name="status_values"),
        Index("ix_decisions_status", "status"),
    )


class Assignment(Base):
    """One track going into one playlist. Rows exist only for sorted tracks."""

    __tablename__ = "assignments"

    track_id: Mapped[str] = mapped_column(
        ForeignKey("tracks.track_id", ondelete="CASCADE"), primary_key=True
    )
    playlist_id: Mapped[str] = mapped_column(Text, primary_key=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Set by apply once the track is actually in the playlist on Spotify.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("ix_assignments_playlist_id", "playlist_id"),)


class Artist(Base):
    __tablename__ = "artists"

    artist_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    genres: Mapped[list[str]] = mapped_column("genres_json", JSON, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
