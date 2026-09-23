"""A stand-in for the Spotify API, shaped like the JSON spotipy returns."""

from __future__ import annotations

import spotipy


def track_item(track_id: str, added_at: str, artists=(("a1", "Artist One"),), title=None,
               release_date="2020-01-01", popularity=50):
    return {
        "added_at": added_at,
        "track": {
            "id": track_id,
            "name": title or f"Track {track_id}",
            "is_local": False,
            "popularity": popularity,
            "album": {"release_date": release_date},
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


class FakePlaylists(FakeSpotify):
    """Adds the playlist endpoints apply needs."""

    def __init__(self, items=(), artists=None, contents=None):
        super().__init__(list(items), artists)
        self.contents: dict[str, list[str]] = {k: list(v) for k, v in (contents or {}).items()}
        self.add_calls: list[tuple[str, list[str]]] = []
        self.read_calls: list[tuple[str, int]] = []
        self.read_errors: dict[str, spotipy.SpotifyException] = {}
        self.add_errors: dict[str, spotipy.SpotifyException] = {}
        self.rate_limit_adds: dict | None = None

    def playlist_items(self, playlist_id, fields=None, limit=100, offset=0, additional_types=None):
        self.read_calls.append((playlist_id, offset))
        if playlist_id in self.read_errors:
            raise self.read_errors[playlist_id]
        ids = self.contents.get(playlist_id, [])
        # playlist_detail carries the artists behind each track when a test
        # needs them; otherwise a bare id is enough.
        detail = {t["id"]: t for t in getattr(self, "playlist_detail", {}).get(playlist_id, [])}
        window = ids[offset : offset + limit]
        return {
            "items": [
                {"track": detail.get(track_id, {"id": track_id})} for track_id in window
            ],
            "next": "url" if offset + limit < len(ids) else None,
        }

    def playlist_add_items(self, playlist_id, uris):
        if self.rate_limit_adds is not None:
            headers, self.rate_limit_adds = self.rate_limit_adds, None
            raise spotipy.SpotifyException(429, -1, "rate limited", headers=headers)
        if playlist_id in self.add_errors:
            raise self.add_errors[playlist_id]
        track_ids = [uri.split(":")[-1] for uri in uris]
        self.add_calls.append((playlist_id, track_ids))
        self.contents.setdefault(playlist_id, []).extend(track_ids)
        return {"snapshot_id": "s"}
