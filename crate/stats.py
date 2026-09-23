"""Counts for the local database. Reads only, never contacts Spotify."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Config
from .models import Assignment, Decision, Playlist, Status, Track


@dataclass
class Stats:
    total: int = 0
    pending: int = 0
    sorted: int = 0
    skipped: int = 0
    by_playlist: list[tuple[str, str, int]] = field(default_factory=list)
    assignments: int = 0
    genres_missing: int = 0
    last_sync: datetime | None = None


def playlist_names(session: Session, cfg: Config | None) -> dict[str, str]:
    """config.toml first, then whatever the database remembers."""
    names = {p.playlist_id: p.name for p in session.scalars(select(Playlist))}
    if cfg:
        names.update({p.playlist_id: p.name for p in cfg.playlists})
    return names


def collect(session: Session, cfg: Config | None = None) -> Stats:
    counts = dict(
        session.execute(
            select(Decision.status, func.count()).group_by(Decision.status)
        ).all()
    )
    total = session.scalar(select(func.count()).select_from(Track)) or 0
    # A track with no decision row at all is still waiting to be triaged.
    undecided = session.scalar(
        select(func.count())
        .select_from(Track)
        .outerjoin(Decision, Decision.track_id == Track.track_id)
        .where(Decision.track_id.is_(None))
    ) or 0

    names = playlist_names(session, cfg)
    # A track can sit in several playlists, so these counts add up to more than
    # the number of sorted tracks.
    rows = session.execute(
        select(Assignment.playlist_id, func.count()).group_by(Assignment.playlist_id)
    ).all()
    by_playlist = sorted(
        ((pid, names.get(pid, pid), count) for pid, count in rows),
        key=lambda r: (-r[2], r[1]),
    )

    return Stats(
        total=total,
        pending=counts.get(Status.pending, 0) + undecided,
        sorted=counts.get(Status.sorted, 0),
        skipped=counts.get(Status.skipped, 0),
        by_playlist=by_playlist,
        assignments=sum(count for _pid, _name, count in by_playlist),
        genres_missing=session.scalar(
            select(func.count()).select_from(Track).where(Track.genres.is_(None))
        ) or 0,
        last_sync=session.scalar(select(func.max(Track.synced_at))),
    )


def render(stats: Stats) -> list[str]:
    lines = [
        f"pending  {stats.pending:>6}",
        f"sorted   {stats.sorted:>6}",
        f"skipped  {stats.skipped:>6}",
        f"total    {stats.total:>6}",
    ]
    if stats.by_playlist:
        lines.append("")
        extra = stats.assignments - stats.sorted
        suffix = f" ({extra} in more than one)" if extra > 0 else ""
        lines.append(f"sorted by playlist:{suffix}")
        width = max(len(name) for _pid, name, _c in stats.by_playlist)
        lines.extend(
            f"  {name:<{width}}  {count:>5}" for _pid, name, count in stats.by_playlist
        )
    if stats.genres_missing:
        lines.append("")
        lines.append(f"tracks with no genres yet: {stats.genres_missing} (run sync again)")
    if stats.last_sync:
        lines.append("")
        lines.append(f"last sync: {stats.last_sync:%Y-%m-%d %H:%M:%S} UTC")
    elif not stats.total:
        lines.append("")
        lines.append("nothing synced yet - run `crate sync`")
    return lines
