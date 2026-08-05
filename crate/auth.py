"""Authorization Code + PKCE against Spotify.

No client secret is stored anywhere - PKCE exists precisely so a locally
installed app does not need one. The token file holds the access token and the
refresh token and is written 0600.

Scopes are deliberately minimal. user-library-modify is never requested: this
tool only reads Liked Songs and never removes anything from them.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import spotipy
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyPKCE

from . import paths
from .config import Config

SCOPES = (
    "user-library-read",
    "playlist-read-private",
    "playlist-modify-private",
)
SCOPE_STRING = " ".join(SCOPES)

FORBIDDEN_SCOPES = frozenset({"user-library-modify"})


class NotAuthenticated(Exception):
    pass


class SecureFileCache(CacheHandler):
    """Token cache backed by a single 0600 file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.token_path()

    def get_cached_token(self) -> dict | None:
        try:
            with self.path.open("r") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None

    def save_token_to_cache(self, token_info: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        # Create with the right mode from the start - never widen, even briefly.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(token_info, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def mode(self) -> str | None:
        try:
            return stat.filemode(self.path.stat().st_mode)
        except FileNotFoundError:
            return None


def auth_manager(cfg: Config, cache: CacheHandler | None = None, open_browser: bool = True) -> SpotifyPKCE:
    assert FORBIDDEN_SCOPES.isdisjoint(SCOPES), "refusing to request a write scope on the library"
    return SpotifyPKCE(
        client_id=cfg.client_id,
        redirect_uri=cfg.redirect_uri,
        scope=SCOPE_STRING,
        cache_handler=cache or SecureFileCache(),
        open_browser=open_browser,
    )


def login(cfg: Config, open_browser: bool = True, cache: CacheHandler | None = None) -> dict:
    """Run the interactive flow (or reuse a valid token) and return the profile."""
    paths.ensure_home()
    manager = auth_manager(cfg, cache=cache, open_browser=open_browser)
    manager.get_access_token()
    return spotipy.Spotify(auth_manager=manager).current_user()


def cached_token(manager: SpotifyPKCE) -> dict | None:
    """Cached token, or None if absent or issued for a different scope set."""
    return manager.validate_token(manager.cache_handler.get_cached_token())


def client(cfg: Config, cache: CacheHandler | None = None) -> spotipy.Spotify:
    """Authenticated client for a token obtained earlier. Refreshes silently."""
    manager = auth_manager(cfg, cache=cache, open_browser=False)
    if cached_token(manager) is None:
        raise NotAuthenticated("not authenticated - run `crate login`")
    return spotipy.Spotify(auth_manager=manager)


def token_status(cache: CacheHandler | None = None) -> dict | None:
    """Describe the cached token without contacting Spotify."""
    cache = cache or SecureFileCache()
    token = cache.get_cached_token()
    if token is None:
        return None
    expires_at = token.get("expires_at")
    return {
        "scopes": sorted((token.get("scope") or "").split()),
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc) if expires_at else None,
        "has_refresh_token": bool(token.get("refresh_token")),
    }
