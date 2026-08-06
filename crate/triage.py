"""Interactive triage: one track per screen, keystrokes to sort it.

A track can go into several playlists at once, so hotkeys toggle a selection and
enter commits it. Every commit is written before the next track is drawn, so
quitting - or losing the terminal - never costs more than the track on screen.

Suggestions never decide anything on their own: a suggested playlist still has
to be accepted with a keystroke.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from . import stats
from .config import Config, PlaylistCfg
from .db import utcnow
from .models import Assignment, Decision, Playlist, Status, Track

QUIT_KEYS = {"q", "\x03", "\x04"}  # q, Ctrl-C, Ctrl-D
COMMIT_KEYS = {"\r", "\n"}


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
    assignments: int = 0
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


def assignments_for(session: Session, track_id: str) -> set[str]:
    return set(
        session.scalars(select(Assignment.playlist_id).where(Assignment.track_id == track_id))
    )


def commit_decision(
    session: Session, track_id: str, status: Status, playlist_ids: set[str]
) -> None:
    """Write one track's decision and its playlists, in one transaction."""
    now = utcnow()
    session.execute(
        insert(Decision)
        .values(track_id=track_id, status=status, decided_at=now)
        .on_conflict_do_update(
            index_elements=[Decision.track_id],
            set_={"status": status, "decided_at": now},
        )
    )
    # Assignments are replaced wholesale: undo and re-decide must not leave
    # a playlist behind from the previous answer. applied_at is preserved for
    # rows that survive, so apply does not re-add what it already added.
    stale = delete(Assignment).where(Assignment.track_id == track_id)
    if playlist_ids:
        stale = stale.where(Assignment.playlist_id.notin_(playlist_ids))
    session.execute(stale)
    if playlist_ids:
        session.execute(
            insert(Assignment)
            .values(
                [
                    {"track_id": track_id, "playlist_id": pid, "decided_at": now}
                    for pid in sorted(playlist_ids)
                ]
            )
            .on_conflict_do_nothing(index_elements=[Assignment.track_id, Assignment.playlist_id])
        )
    session.commit()


def undo(session: Session, track_id: str) -> None:
    commit_decision(session, track_id, Status.pending, set())
    session.execute(
        Decision.__table__.update()
        .where(Decision.track_id == track_id)
        .values(decided_at=None)
    )
    session.commit()


def undo_previous(session: Session, track_id: str, state: "Summary") -> bool:
    """Revert one decision and adjust the session counters."""
    # a column query, not session.get: decisions are written with Core inserts,
    # which leave any cached entity stale
    was = session.scalar(select(Decision.status).where(Decision.track_id == track_id))
    if was is Status.sorted:
        state.sorted -= 1
        state.assignments -= len(assignments_for(session, track_id))
    else:
        state.skipped -= 1
    undo(session, track_id)
    state.undone += 1
    return True


def render(
    track: Track,
    cfg: Config,
    suggestion: PlaylistCfg | None,
    selected: set[str],
    counters,
) -> list[str]:
    pending, sorted_, skipped = counters
    genres = ", ".join(track.genres) if track.genres else "none"
    meta = [f"added {track.added_at:%Y-%m-%d}"]
    if track.release_year:
        meta.append(f"released {track.release_year}")
    if track.popularity is not None:
        meta.append(f"popularity {track.popularity}")

    lines = [
        f"pending {pending}   sorted {sorted_}   skipped {skipped}",
        "",
        track.title,
        track.artist_name or "unknown artist",
        f"genres: {genres}",
        "   ".join(meta),
        "",
    ]
    if selected:
        chosen = [p.name for p in cfg.playlists if p.playlist_id in selected]
        lines.append(f"selected: {', '.join(chosen)}")
    elif suggestion:
        lines.append(f"suggested: {suggestion.hotkey} {suggestion.name}")
    else:
        lines.append("suggested: none")
    lines.append("")
    lines.append(
        "   ".join(
            f"{'*' if p.playlist_id in selected else ' '}{p.hotkey} {p.name}"
            for p in cfg.playlists
        )
        or "no playlists in config"
    )
    if selected:
        action = "enter save"
    elif suggestion:
        action = "enter accept"
    else:
        action = "1-9 choose"
    lines.append(f"{action}   s skip   u undo   q quit")
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
        track = None
        while pushback and track is None:
            # A queued track may have been decided in the meantime - undoing the
            # track currently on screen queues it and then re-decides it.
            candidate = pushback.pop()
            status = session.scalar(
                select(Decision.status).where(Decision.track_id == candidate)
            )
            if status in (None, Status.pending):
                track = session.get(Track, candidate)
        if track is None:
            track = next_pending(session, oldest_first)
        if track is None:
            # Nothing left, but the last decision must still be undoable.
            if not history:
                break
            term.clear()
            term.write("nothing left to triage")
            term.write("")
            term.write("u undo   q quit")
            if message:
                term.write("")
                term.write(message)
                message = ""
            key = term.read_key()
            if key == "u":
                previous = history.pop()
                if undo_previous(session, previous, state):
                    pushback.append(previous)
                continue
            if key in QUIT_KEYS:
                break
            message = "keys: u undo, q quit"
            continue

        suggestion = suggest(cfg, track.genres)
        selected = assignments_for(session, track.track_id)

        while True:  # keystrokes for this track
            term.clear()
            for line in render(track, cfg, suggestion, selected, counts(session)):
                term.write(line)
            if message:
                term.write("")
                term.write(message)
                message = ""

            key = term.read_key()

            if key in QUIT_KEYS:
                state.quit = True
                break
            if key in COMMIT_KEYS:
                chosen = selected or ({suggestion.playlist_id} if suggestion else set())
                if not chosen:
                    message = "nothing selected and nothing suggested - press 1-9"
                    continue
                commit_decision(session, track.track_id, Status.sorted, chosen)
                history.append(track.track_id)
                state.sorted += 1
                state.assignments += len(chosen)
                break
            if key == "s":
                commit_decision(session, track.track_id, Status.skipped, set())
                history.append(track.track_id)
                state.skipped += 1
                break
            if key == "u":
                if not history:
                    message = "nothing to undo"
                    continue
                previous = history.pop()
                undo_previous(session, previous, state)
                # come back to the track on screen after the undone one, unless
                # they are the same track
                if previous != track.track_id:
                    pushback.append(track.track_id)
                pushback.append(previous)
                break
            if key.isdigit() and key != "0":
                playlist = cfg.by_hotkey(key)
                if playlist is None:
                    message = f"no playlist bound to {key}"
                    continue
                selected ^= {playlist.playlist_id}  # toggle
                continue

            message = "keys: 1-9 toggle playlist, enter save, s skip, u undo, q quit"

        if state.quit:
            break

    return state
