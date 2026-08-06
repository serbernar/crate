"""Thin wrapper over spotipy: pagination, batching and rate limits in one place.

Only endpoints that still work for apps registered after 2024-11-27 are used.
audio-features, audio-analysis, recommendations and related-artists all return
403 for those apps and are deliberately absent.
"""

from __future__ import annotations

import time
from typing import Callable, Iterator

import spotipy

from .config import Config

SAVED_TRACKS_PAGE = 50
ARTISTS_BATCH = 50          # /v1/artists accepts at most 50 ids
PLAYLIST_ADD_BATCH = 100    # /v1/playlists/{id}/tracks accepts at most 100 uris
PLAYLIST_PAGE = 100         # page size when reading a playlist back

# 429 is handled here rather than by spotipy's urllib3 retry, which would sleep
# for whatever Retry-After says without telling anyone.
RETRY_STATUSES = (500, 502, 503, 504)
MAX_RETRY_AFTER = 300
MAX_ATTEMPTS = 5


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            f"rate limited by Spotify, Retry-After is {retry_after}s - try again later"
        )
        self.retry_after = retry_after


def retry_after_seconds(headers) -> int:
    try:
        return max(1, int((headers or {}).get("Retry-After", 1)))
    except (TypeError, ValueError):
        return 1


class Client:
    def __init__(
        self,
        sp: spotipy.Spotify,
        sleep: Callable[[float], None] = time.sleep,
        on_wait: Callable[[int], None] | None = None,
    ) -> None:
        self.sp = sp
        self._sleep = sleep
        self._on_wait = on_wait

    def call(self, fn: Callable, *args, **kwargs):
        """Run one API call, waiting out 429s as instructed."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return fn(*args, **kwargs)
            except spotipy.SpotifyException as exc:
                if exc.http_status != 429:
                    raise
                wait = retry_after_seconds(exc.headers)
                if wait > MAX_RETRY_AFTER or attempt == MAX_ATTEMPTS:
                    raise RateLimited(wait) from exc
                if self._on_wait:
                    self._on_wait(wait)
                # A second of slack: Retry-After is whole seconds, rounded down.
                self._sleep(wait + 1)
        raise AssertionError("unreachable")

    # --- reads ---------------------------------------------------------------

    def saved_track_pages(self, page_size: int = SAVED_TRACKS_PAGE) -> Iterator[dict]:
        """Liked Songs, newest first, one page at a time."""
        offset = 0
        while True:
            page = self.call(
                self.sp.current_user_saved_tracks, limit=page_size, offset=offset
            )
            items = page.get("items") or []
            if not items:
                return
            yield page
            if not page.get("next"):
                return
            offset += len(items)

    def playlist_track_ids(self, playlist_id: str) -> set[str]:
        """Every track already in the playlist.

        This is what makes apply idempotent: the playlist itself is the truth,
        so a re-run adds nothing even if the local database was lost.
        """
        found: set[str] = set()
        offset = 0
        while True:
            page = self.call(
                self.sp.playlist_items,
                playlist_id,
                fields="items(track(id)),next",
                limit=PLAYLIST_PAGE,
                offset=offset,
                additional_types=("track",),
            )
            items = page.get("items") or []
            for item in items:
                track = item.get("track") or {}
                if track.get("id"):
                    found.add(track["id"])
            if not items or not page.get("next"):
                return found
            offset += len(items)

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> Iterator[list[str]]:
        """Add tracks in batches, yielding each batch after it lands.

        The caller records what has been written between batches, so an
        interruption leaves the database agreeing with Spotify.
        """
        for i in range(0, len(track_ids), PLAYLIST_ADD_BATCH):
            batch = track_ids[i : i + PLAYLIST_ADD_BATCH]
            self.call(
                self.sp.playlist_add_items,
                playlist_id,
                [f"spotify:track:{t}" for t in batch],
            )
            yield batch

    def artists(self, artist_ids: list[str]) -> Iterator[dict]:
        for i in range(0, len(artist_ids), ARTISTS_BATCH):
            batch = artist_ids[i : i + ARTISTS_BATCH]
            response = self.call(self.sp.artists, batch)
            for artist in response.get("artists") or []:
                if artist:  # unavailable artists come back as null
                    yield artist


def connect(cfg: Config, **kwargs) -> Client:
    from . import auth

    # Let spotipy retry transient server errors, but not 429 - see above.
    sp = auth.client(cfg, status_forcelist=RETRY_STATUSES)
    return Client(sp, **kwargs)
