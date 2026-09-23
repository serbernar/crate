"""Write triage decisions to Spotify.

Nothing is written without --commit. The plan is built the same way either way,
so a dry run shows exactly what a commit would do.

Idempotence comes from the playlist itself: every target playlist is read back
before anything is added, and tracks already in it are marked done without an
API call. A repeat run therefore adds nothing, even if applied_at was lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import spotipy
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api import Client
from .config import Config
from .db import utcnow
from .models import Assignment, Playlist, Track
from .writelog import ADD_TRACKS, WriteLog

Progress = Callable[[str], None]


@dataclass
class PlaylistPlan:
    playlist_id: str
    name: str
    to_add: list[Track] = field(default_factory=list)
    already_there: list[Track] = field(default_factory=list)
    error: str | None = None


@dataclass
class Plan:
    playlists: list[PlaylistPlan] = field(default_factory=list)

    @property
    def to_add(self) -> int:
        return sum(len(p.to_add) for p in self.playlists)

    @property
    def already_there(self) -> int:
        return sum(len(p.already_there) for p in self.playlists)

    @property
    def failed(self) -> list[PlaylistPlan]:
        return [p for p in self.playlists if p.error]


@dataclass
class Result:
    added: int = 0
    confirmed: int = 0  # already in the playlist, recorded without a write
    calls: int = 0
    failed: list[str] = field(default_factory=list)


def pending_assignments(session: Session) -> dict[str, list[Track]]:
    """Tracks waiting to be written, grouped by playlist, oldest decision first."""
    rows = session.execute(
        select(Assignment.playlist_id, Track)
        .join(Track, Track.track_id == Assignment.track_id)
        .where(Assignment.applied_at.is_(None))
        .order_by(Assignment.playlist_id, Assignment.decided_at, Track.track_id)
    ).all()
    grouped: dict[str, list[Track]] = {}
    for playlist_id, track in rows:
        grouped.setdefault(playlist_id, []).append(track)
    return grouped


def playlist_names(session: Session, cfg: Config | None) -> dict[str, str]:
    names = {p.playlist_id: p.name for p in session.scalars(select(Playlist))}
    if cfg:
        names.update({p.playlist_id: p.name for p in cfg.playlists})
    return names


def build_plan(session: Session, client: Client, cfg: Config | None = None) -> Plan:
    names = playlist_names(session, cfg)
    plan = Plan()
    for playlist_id, tracks in sorted(pending_assignments(session).items()):
        entry = PlaylistPlan(playlist_id=playlist_id, name=names.get(playlist_id, playlist_id))
        try:
            present = client.playlist_track_ids(playlist_id)
        except spotipy.SpotifyException as exc:
            entry.error = f"cannot read playlist: {exc.msg or exc.http_status}"
            plan.playlists.append(entry)
            continue
        for track in tracks:
            if track.track_id in present:
                entry.already_there.append(track)
            else:
                entry.to_add.append(track)
        plan.playlists.append(entry)
    return plan


def render(plan: Plan, committing: bool = False) -> list[str]:
    if not plan.playlists:
        return ["nothing to apply - every decision is already on Spotify"]

    lines: list[str] = []
    for entry in plan.playlists:
        lines.append(f"{entry.name} ({entry.playlist_id})")
        if entry.error:
            lines.append(f"  error: {entry.error}")
            lines.append("")
            continue
        for track in entry.to_add:
            lines.append(f"  add   {track.title} - {track.artist_name}")
        for track in entry.already_there:
            lines.append(f"  have  {track.title} - {track.artist_name}")
        lines.append("")

    verb = "adding" if committing else "would add"
    lines.append(f"{verb} {plan.to_add} tracks, {plan.already_there} already in place")
    if plan.failed:
        lines.append(f"{len(plan.failed)} playlists could not be read")
    if not committing:
        lines.append("dry run - nothing was written; pass --commit to apply")
    return lines


def mark_applied(session: Session, playlist_id: str, track_ids: list[str]) -> None:
    now = utcnow()
    session.execute(
        Assignment.__table__.update()
        .where(
            Assignment.playlist_id == playlist_id,
            Assignment.track_id.in_(track_ids),
        )
        .values(applied_at=now)
    )
    session.commit()


def execute(
    session: Session,
    client: Client,
    plan: Plan,
    log: WriteLog | None = None,
    progress: Progress | None = None,
) -> Result:
    log = log or WriteLog()
    result = Result()

    def report(line: str) -> None:
        if progress:
            progress(line)

    for entry in plan.playlists:
        if entry.error:
            result.failed.append(entry.playlist_id)
            continue

        if entry.already_there:
            # Already on Spotify; record that without touching the API.
            mark_applied(session, entry.playlist_id, [t.track_id for t in entry.already_there])
            result.confirmed += len(entry.already_there)

        if not entry.to_add:
            continue

        track_ids = [t.track_id for t in entry.to_add]
        endpoint = ADD_TRACKS.format(playlist_id=entry.playlist_id)
        try:
            for batch in client.add_tracks(entry.playlist_id, track_ids):
                log.record(endpoint, batch, entry.playlist_id)
                mark_applied(session, entry.playlist_id, batch)
                result.added += len(batch)
                result.calls += 1
                report(f"{entry.name}: added {len(batch)}")
        except spotipy.SpotifyException as exc:
            # Batches already written stay recorded; the rest is left for the
            # next run, which will see them missing from the playlist again.
            entry.error = f"cannot add to playlist: {exc.msg or exc.http_status}"
            result.failed.append(entry.playlist_id)
            report(f"{entry.name}: {entry.error}")

    return result
