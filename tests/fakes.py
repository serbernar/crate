"""A stand-in for the Spotify API, shaped like the JSON spotipy returns."""

from __future__ import annotations

import spotipy


def track_item(track_id: str, added_at: str, artists=(("a1", "Artist One"),), title=None):
    return {
        "added_at": added_at,
        "track": {
            "id": track_id,
            "name": title or f"Track {track_id}",
            "is_local": False,
            "artists": [{"id": aid, "name": name} for aid, name in artists],
        },
    }


def local_item(added_at: str = "2024-01-01T00:00:00Z"):
    return {
        "added_at": added_at,
        "track": {"id": None, "name": "Local File", "is_local": True, "artists": []},
    }


class FakeSpotify:
    """Serves saved tracks newest first, exactly as Spotify does."""

    def __init__(self, items: list[dict], artists: dict[str, list[str]] | None = None):
        self.items = items
        self.artist_genres = artists or {}
        self.page_calls: list[tuple[int, int]] = []
        self.artist_calls: list[list[str]] = []
        self.rate_limit_once: dict | None = None

    def current_user_saved_tracks(self, limit=20, offset=0):
        self.page_calls.append((limit, offset))
        if self.rate_limit_once is not None:
            headers, self.rate_limit_once = self.rate_limit_once, None
            raise spotipy.SpotifyException(429, -1, "rate limited", headers=headers)
        window = self.items[offset : offset + limit]
        return {
            "items": window,
            "next": "url" if offset + limit < len(self.items) else None,
            "total": len(self.items),
        }

    def artists(self, artist_ids):
        self.artist_calls.append(list(artist_ids))
        return {
            "artists": [
                {"id": aid, "name": f"Name {aid}", "genres": self.artist_genres[aid]}
                if aid in self.artist_genres
                else None
                for aid in artist_ids
            ]
        }
