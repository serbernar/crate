"""Read the playlists that already exist and remember what is in them.

Genre strings cannot express "for the gym" or "for a long drive". What can is
the user's own past sorting: if three tracks by an artist already sit in a
playlist, a fourth probably belongs there too. This reads each target playlist
and records, per playlist, how many tracks each artist and each genre accounts
for. Nothing here decides anything - it only produces evidence for a suggestion
the user still has to accept.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import spotipy
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .api import Client
from .config import Config
from .db import utcnow
from .models import Artist, PlaylistArtist, PlaylistGenre
from .sync import store_artists

Progress = Callable[[str], None]


@dataclass
class PlaylistProfile:
    playlist_id: str
    name: str
    tracks: int = 0
    artists: int = 0
    top_genres: list[tuple[str, int]] = field(default_factory=list)
    error: str | None = None


@dataclass
class LearnResult:
    profiles: list[PlaylistProfile] = field(default_factory=list)
    artists_fetched: int = 0


def read_playlist(client: Client, playlist_id: str) -> tuple[Counter, int]:
    """Artist ids and how many of the playlist's tracks each accounts for."""
    per_artist: Counter = Counter()
    tracks = 0
    for track in client.playlist_tracks(playlist_id):
        tracks += 1
        for artist in track.get("artists") or []:
            if artist.get("id"):
                per_artist[artist["id"]] += 1
    return per_artist, tracks


def genre_counts(session: Session, per_artist: Counter) -> Counter:
    """Spread each artist's track count over the genres that artist carries."""
    known = {
        a.artist_id: (a.genres or [])
        for a in session.scalars(
            select(Artist).where(Artist.artist_id.in_(list(per_artist)))
        )
    }
    per_genre: Counter = Counter()
    for artist_id, count in per_artist.items():
        for genre in known.get(artist_id, []):
            per_genre[genre] += count
    return per_genre


def store_profile(
    session: Session,
    playlist_id: str,
    per_artist: Counter,
    per_genre: Counter,
    now: datetime,
) -> None:
    """Replace what was known about this playlist, in one transaction."""
    session.execute(delete(PlaylistArtist).where(PlaylistArtist.playlist_id == playlist_id))
    session.execute(delete(PlaylistGenre).where(PlaylistGenre.playlist_id == playlist_id))
    if per_artist:
        session.execute(
            insert(PlaylistArtist).values(
                [
                    {
                        "playlist_id": playlist_id,
                        "artist_id": artist_id,
                        "tracks": count,
                        "learned_at": now,
                    }
                    for artist_id, count in per_artist.items()
                ]
            )
        )
    if per_genre:
        session.execute(
            insert(PlaylistGenre).values(
                [
                    {
                        "playlist_id": playlist_id,
                        "genre": genre,
                        "tracks": count,
                        "learned_at": now,
                    }
                    for genre, count in per_genre.items()
                ]
            )
        )
    session.commit()


def missing_artist_ids(session: Session, artist_ids: set[str]) -> list[str]:
    if not artist_ids:
        return []
    have = set(
        session.scalars(select(Artist.artist_id).where(Artist.artist_id.in_(list(artist_ids))))
    )
    return sorted(artist_ids - have)


def learn(
    session: Session,
    client: Client,
    cfg: Config,
    progress: Progress | None = None,
) -> LearnResult:
    result = LearnResult()

    def report(line: str) -> None:
        if progress:
            progress(line)

    collected: dict[str, tuple[Counter, int]] = {}
    for playlist in cfg.playlists:
        try:
            per_artist, tracks = read_playlist(client, playlist.playlist_id)
        except spotipy.SpotifyException as exc:
            result.profiles.append(
                PlaylistProfile(
                    playlist_id=playlist.playlist_id,
                    name=playlist.name,
                    error=f"cannot read playlist: {exc.msg or exc.http_status}",
                )
            )
            report(f"{playlist.name}: cannot read")
            continue
        collected[playlist.playlist_id] = (per_artist, tracks)
        report(f"{playlist.name}: {tracks} tracks, {len(per_artist)} artists")

    # Genres come from the artists table, so fill in whoever is missing first.
    wanted: set[str] = set()
    for per_artist, _tracks in collected.values():
        wanted.update(per_artist)
    missing = missing_artist_ids(session, wanted)
    if missing:
        report(f"fetching {len(missing)} artists")
        result.artists_fetched = store_artists(session, client.artists(missing), utcnow())

    now = utcnow()
    for playlist in cfg.playlists:
        if playlist.playlist_id not in collected:
            continue
        per_artist, tracks = collected[playlist.playlist_id]
        per_genre = genre_counts(session, per_artist)
        store_profile(session, playlist.playlist_id, per_artist, per_genre, now)
        result.profiles.append(
            PlaylistProfile(
                playlist_id=playlist.playlist_id,
                name=playlist.name,
                tracks=tracks,
                artists=len(per_artist),
                top_genres=per_genre.most_common(5),
            )
        )
    return result


def render(result: LearnResult) -> list[str]:
    if not result.profiles:
        return ["no playlists in config - add [[playlists]] entries first"]
    lines: list[str] = []
    for profile in result.profiles:
        lines.append(f"{profile.name} ({profile.playlist_id})")
        if profile.error:
            lines.append(f"  {profile.error}")
            continue
        lines.append(f"  {profile.tracks} tracks, {profile.artists} artists")
        if profile.top_genres:
            top = ", ".join(f"{genre} {count}" for genre, count in profile.top_genres)
            lines.append(f"  top genres: {top}")
        else:
            lines.append("  no genres known for these artists")
    return lines
