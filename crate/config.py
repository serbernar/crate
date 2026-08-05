"""config.toml loading and validation.

The file is hand-edited. Playlists are declared once with a hotkey; genre rules
reference a playlist by that hotkey and are matched in file order, first match
wins.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
HOTKEYS = "123456789"

TEMPLATE = """\
[spotify]
# From https://developer.spotify.com/dashboard - create an app, add the
# redirect URI below to it. No client secret is needed (PKCE).
client_id = ""
redirect_uri = "{redirect_uri}"

# Target playlists. hotkey is what you press during `crate triage`, 1-9.
# id is the Spotify playlist id (the part after /playlist/ in its URL).
# [[playlists]]
# hotkey = "1"
# id = "0000000000000000000000"
# name = "Techno"

# Genre rules, matched as case-insensitive substrings against the track's
# artist genres. First match wins; no match means no suggestion.
# [[rules]]
# genre = "techno"
# playlist = "1"
"""


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class PlaylistCfg:
    hotkey: str
    playlist_id: str
    name: str


@dataclass(frozen=True)
class Rule:
    genre: str
    playlist_id: str


@dataclass(frozen=True)
class Config:
    client_id: str
    redirect_uri: str = DEFAULT_REDIRECT_URI
    playlists: tuple[PlaylistCfg, ...] = ()
    rules: tuple[Rule, ...] = field(default=())

    def by_hotkey(self, hotkey: str) -> PlaylistCfg | None:
        return next((p for p in self.playlists if p.hotkey == hotkey), None)

    def by_id(self, playlist_id: str) -> PlaylistCfg | None:
        return next((p for p in self.playlists if p.playlist_id == playlist_id), None)


def write_template(path: Path | None = None) -> Path:
    target = path or paths.config_path()
    if target.exists():
        raise ConfigError(f"{target} already exists")
    paths.ensure_home()
    target.write_text(TEMPLATE.format(redirect_uri=DEFAULT_REDIRECT_URI))
    return target


def load(path: Path | None = None) -> Config:
    target = path or paths.config_path()
    if not target.exists():
        raise ConfigError(f"no config at {target} - run `crate init` first")
    try:
        with target.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target}: {exc}") from exc
    return parse(raw, source=str(target))


def parse(raw: dict, source: str = "config") -> Config:
    spotify = raw.get("spotify") or {}
    client_id = str(spotify.get("client_id") or "").strip()
    if not client_id:
        raise ConfigError(f"{source}: spotify.client_id is empty")
    redirect_uri = str(spotify.get("redirect_uri") or DEFAULT_REDIRECT_URI).strip()

    playlists: list[PlaylistCfg] = []
    seen_hotkeys: set[str] = set()
    seen_ids: set[str] = set()
    for i, entry in enumerate(raw.get("playlists") or [], start=1):
        where = f"{source}: playlists[{i}]"
        hotkey = str(entry.get("hotkey") or "").strip()
        playlist_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip() or playlist_id
        if hotkey not in HOTKEYS:
            raise ConfigError(f"{where}: hotkey must be one of {HOTKEYS}, got {hotkey!r}")
        if not playlist_id:
            raise ConfigError(f"{where}: id is required")
        if hotkey in seen_hotkeys:
            raise ConfigError(f"{where}: hotkey {hotkey!r} is used twice")
        if playlist_id in seen_ids:
            raise ConfigError(f"{where}: playlist id {playlist_id!r} is used twice")
        seen_hotkeys.add(hotkey)
        seen_ids.add(playlist_id)
        playlists.append(PlaylistCfg(hotkey=hotkey, playlist_id=playlist_id, name=name))

    known = {p.hotkey: p for p in playlists}
    rules: list[Rule] = []
    for i, entry in enumerate(raw.get("rules") or [], start=1):
        where = f"{source}: rules[{i}]"
        genre = str(entry.get("genre") or "").strip().lower()
        ref = str(entry.get("playlist") or "").strip()
        if not genre:
            raise ConfigError(f"{where}: genre is required")
        target = known.get(ref) or next((p for p in playlists if p.playlist_id == ref), None)
        if target is None:
            raise ConfigError(f"{where}: playlist {ref!r} is not declared in [[playlists]]")
        rules.append(Rule(genre=genre, playlist_id=target.playlist_id))

    return Config(
        client_id=client_id,
        redirect_uri=redirect_uri,
        playlists=tuple(playlists),
        rules=tuple(rules),
    )
