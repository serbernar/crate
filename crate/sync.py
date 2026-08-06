"""Incremental pull of Liked Songs.

Liked Songs come back newest first, so an incremental run walks from the top and
stops at the first track it already knows. Each page is committed before the
next one is fetched, so an interrupted sync keeps what it got.

Nothing is ever deleted here: a track that disappears from Liked Songs keeps its
row and its decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .api import Client
from .db import utcnow
from .models import Artist, Decision, Status, Track

Progress = Callable[[str], None]


@dataclass
class SyncResult:
    pages: int = 0
    seen: int = 0
    new_tracks: int = 0
    skipped_local: int = 0
    stopped_early: bool = False
    artists_fetched: int = 0
    genres_filled: int = 0
    lines: list[str] = field(default_factory=list)


def parse_added_at(value: str) -> datetime:
    """Spotify sends '2024-01-01T00:00:00Z'; store naive UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def track_row(item: dict, now: datetime) -> dict | None:
    """Map one saved-track item, or None if it is not a real catalogue track."""
    track = item.get("track") or {}
    track_id = track.get("id")
    if not track_id or track.get("is_local"):
        return None
    artists = [a for a in (track.get("artists") or []) if a.get("id")]
    return {
        "track_id": track_id,
        "artist_ids": [a["id"] for a in artists],
        "title": track.get("name") or track_id,
        "artist_name": ", ".join(a.get("name") or "" for a in artists),
        "added_at": parse_added_at(item["added_at"]),
        "synced_at": now,
    }


def known_track_ids(session: Session) -> set[str]:
    return set(session.scalars(select(Track.track_id)))


def store_page(session: Session, rows: list[dict]) -> None:
    """Insert tracks and their pending decisions.

    Tracks are updated in place, but genres_json is left alone: it is filled in
    by the artist pass and must survive a re-sync. Decisions are never touched
    once they exist, so re-syncing cannot undo triage.
    """
    if not rows:
        return
    session.execute(
        insert(Track)
        .values(rows)
        .on_conflict_do_update(
            index_elements=[Track.track_id],
            set_={
                "artist_ids": insert(Track).excluded.artist_ids,
                "title": insert(Track).excluded.title,
                "artist_name": insert(Track).excluded.artist_name,
                "added_at": insert(Track).excluded.added_at,
                "synced_at": insert(Track).excluded.synced_at,
            },
        )
    )
    session.execute(
        insert(Decision)
        .values([{"track_id": r["track_id"], "status": Status.pending} for r in rows])
        .on_conflict_do_nothing(index_elements=[Decision.track_id])
    )
    session.commit()


def pull_tracks(
    session: Session, client: Client, full: bool, progress: Progress, result: SyncResult
) -> None:
    known = known_track_ids(session)
    now = utcnow()

    for page in client.saved_track_pages():
        result.pages += 1
        rows: list[dict] = []
        for item in page.get("items") or []:
            result.seen += 1
            row = track_row(item, now)
            if row is None:
                result.skipped_local += 1
                continue
            if row["track_id"] in known:
                if not full:
                    result.stopped_early = True
                    break
                continue
            known.add(row["track_id"])
            rows.append(row)

        store_page(session, rows)
        result.new_tracks += len(rows)
        progress(f"page {result.pages}: {len(page.get('items') or [])} seen, {len(rows)} new")
        if result.stopped_early:
            progress("reached a track that was already known, stopping")
            break


def missing_artist_ids(session: Session) -> list[str]:
    """Artists referenced by tracks that still have no genres, and not yet fetched."""
    needed: set[str] = set()
    for artist_ids in session.scalars(select(Track.artist_ids).where(Track.genres.is_(None))):
        needed.update(artist_ids or [])
    if not needed:
        return []
    have = set(session.scalars(select(Artist.artist_id).where(Artist.artist_id.in_(needed))))
    return sorted(needed - have)


def store_artists(session: Session, artists: Iterable[dict], now: datetime) -> int:
    rows = [
        {
            "artist_id": a["id"],
            "name": a.get("name") or a["id"],
            # set_ and excluded below are keyed by column name, not by the
            # model attribute, so spell it out here too.
            "genres_json": a.get("genres") or [],
            "fetched_at": now,
        }
        for a in artists
        if a.get("id")
    ]
    if not rows:
        return 0
    session.execute(
        insert(Artist)
        .values(rows)
        .on_conflict_do_update(
            index_elements=[Artist.artist_id],
            set_={
                "name": insert(Artist).excluded.name,
                "genres_json": insert(Artist).excluded.genres_json,
                "fetched_at": insert(Artist).excluded.fetched_at,
            },
        )
    )
    session.commit()
    return len(rows)


def fill_genres(session: Session) -> int:
    """Denormalise artist genres onto every track that still lacks them.

    An empty list is a real answer - the artists are known and carry no genres -
    and is stored as such so the track is not re-examined on the next run.
    """
    pending = session.scalars(select(Track).where(Track.genres.is_(None))).all()
    if not pending:
        return 0
    known = {
        a.artist_id: (a.genres or [])
        for a in session.scalars(select(Artist)).all()
    }
    filled = 0
    for track in pending:
        ids = track.artist_ids or []
        if any(aid not in known for aid in ids):
            continue  # an artist could not be fetched; try again next sync
        genres = sorted({g for aid in ids for g in known[aid]})
        track.genres = genres
        filled += 1
    session.commit()
    return filled


def sync(
    session: Session,
    client: Client,
    full: bool = False,
    progress: Progress | None = None,
) -> SyncResult:
    result = SyncResult()

    def report(line: str) -> None:
        result.lines.append(line)
        if progress:
            progress(line)

    pull_tracks(session, client, full, report, result)

    missing = missing_artist_ids(session)
    if missing:
        report(f"fetching {len(missing)} artists")
        result.artists_fetched = store_artists(session, client.artists(missing), utcnow())
    result.genres_filled = fill_genres(session)
    return result
