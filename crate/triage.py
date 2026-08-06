"""Interactive triage: one track per screen, one keystroke per decision.

Every decision is committed before the next track is drawn, so quitting - or
losing the terminal - never costs more than the track on screen.

Suggestions never decide anything on their own: a suggested playlist still has
to be accepted with a keystroke.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from . import stats
from .config import Config, PlaylistCfg
from .db import utcnow
from .models import Decision, Playlist, Status, Track

QUIT_KEYS = {"q", "\x03", "\x04"}  # q, Ctrl-C, Ctrl-D
ACCEPT_KEYS = {"\r", "\n"}


class Terminal(Protocol):
    """The parts of a terminal triage needs, so tests can supply their own."""

    def read_key(self) -> str: ...
    def write(self, line: str = "") -> None: ...
    def clear(self) -> None: ...


@dataclass
class Summary:
    sorted: int = 0
    skipped: int = 0
    undone: int = 0
    quit: bool = False


def suggest(cfg: Config, genres: list[str] | None) -> PlaylistCfg | None:
    """First rule whose genre appears in any of the track's genres wins.

    Rules are matched in the order they appear in config.toml; no match means no
    suggestion, and the user picks unaided.
    """
    lowered = [g.lower() for g in genres or []]
    for rule in cfg.rules:
        if any(rule.genre in genre for genre in lowered):
            return cfg.by_id(rule.playlist_id)
    return None


def remember_playlists(session: Session, cfg: Config) -> None:
    """Mirror config playlists into the database so other commands can name them."""
    if not cfg.playlists:
        return
    # hotkeys are unique, so clear them before reassigning
    session.execute(Playlist.__table__.update().values(hotkey=None))
    session.execute(
        insert(Playlist)
        .values(
            [
                {"playlist_id": p.playlist_id, "name": p.name, "hotkey": p.hotkey}
                for p in cfg.playlists
            ]
        )
        .on_conflict_do_update(
            index_elements=[Playlist.playlist_id],
            set_={
                "name": insert(Playlist).excluded.name,
                "hotkey": insert(Playlist).excluded.hotkey,
            },
        )
    )
    session.commit()


def pending_query(oldest_first: bool = False):
    order = Track.added_at.asc() if oldest_first else Track.added_at.desc()
    return (
        select(Track)
        .outerjoin(Decision, Decision.track_id == Track.track_id)
        .where((Decision.track_id.is_(None)) | (Decision.status == Status.pending))
        .order_by(order, Track.track_id)
    )


def next_pending(session: Session, oldest_first: bool = False) -> Track | None:
    return session.scalars(pending_query(oldest_first).limit(1)).first()


def counts(session: Session) -> tuple[int, int, int]:
    report = stats.collect(session)
    return report.pending, report.sorted, report.skipped


def record(session: Session, track_id: str, status: Status, playlist_id: str | None) -> None:
    """Write one decision and commit it immediately."""
    session.execute(
        insert(Decision)
        .values(
            track_id=track_id,
            status=status,
            playlist_id=playlist_id,
            decided_at=utcnow(),
        )
        .on_conflict_do_update(
            index_elements=[Decision.track_id],
            set_={
                "status": status,
                "playlist_id": playlist_id,
                "decided_at": utcnow(),
            },
        )
    )
    session.commit()


def undo(session: Session, track_id: str) -> None:
    record(session, track_id, Status.pending, None)


def render(track: Track, cfg: Config, suggestion: PlaylistCfg | None, counters) -> list[str]:
    pending, sorted_, skipped = counters
    genres = ", ".join(track.genres) if track.genres else "none"
    lines = [
        f"pending {pending}   sorted {sorted_}   skipped {skipped}",
        "",
        track.title,
        track.artist_name or "unknown artist",
        f"genres: {genres}",
        f"added:  {track.added_at:%Y-%m-%d}",
        "",
    ]
    if suggestion:
        lines.append(f"suggested: {suggestion.hotkey} {suggestion.name}")
    else:
        lines.append("suggested: none")
    lines.append("")
    lines.append("   ".join(f"{p.hotkey} {p.name}" for p in cfg.playlists) or "no playlists in config")
    keys = "s skip   u undo   q quit"
    lines.append(f"enter accept   {keys}" if suggestion else keys)
    return lines


def run(
    session: Session,
    cfg: Config,
    term: Terminal,
    oldest_first: bool = False,
) -> Summary:
    state = Summary()
    remember_playlists(session, cfg)
    history: list[str] = []
    pushback: list[str] = []
    message = ""

    while True:
        if pushback:
            track = session.get(Track, pushback.pop())
        else:
            track = next_pending(session, oldest_first)
        if track is None:
            break

        suggestion = suggest(cfg, track.genres)
        term.clear()
        for line in render(track, cfg, suggestion, counts(session)):
            term.write(line)
        if message:
            term.write("")
            term.write(message)
            message = ""

        key = term.read_key()

        if key in QUIT_KEYS:
            state.quit = True
            break
        if key in ACCEPT_KEYS:
            if not suggestion:
                message = "no suggestion to accept"
                continue
            record(session, track.track_id, Status.sorted, suggestion.playlist_id)
            history.append(track.track_id)
            state.sorted += 1
            continue
        if key == "s":
            record(session, track.track_id, Status.skipped, None)
            history.append(track.track_id)
            state.skipped += 1
            continue
        if key == "u":
            if not history:
                message = "nothing to undo"
                continue
            previous = history.pop()
            # a column query, not session.get: decisions are written with Core
            # inserts, which leave any cached entity stale
            was = session.scalar(select(Decision.status).where(Decision.track_id == previous))
            if was is Status.sorted:
                state.sorted -= 1
            else:
                state.skipped -= 1
            undo(session, previous)
            state.undone += 1
            # the track being shown is still pending; come back to it after
            pushback.append(track.track_id)
            pushback.append(previous)
            continue
        if key.isdigit() and key != "0":
            playlist = cfg.by_hotkey(key)
            if playlist is None:
                message = f"no playlist bound to {key}"
                continue
            record(session, track.track_id, Status.sorted, playlist.playlist_id)
            history.append(track.track_id)
            state.sorted += 1
            continue

        message = "keys: 1-9 playlist, enter accept, s skip, u undo, q quit"

    return state
