"""Filesystem layout. Everything lives under one directory, overridable for tests."""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "SPOTIFY_TRIAGE_HOME"
DEFAULT_HOME = Path.home() / ".config" / "spotify-triage"


def home() -> Path:
    raw = os.environ.get(ENV_HOME)
    return Path(raw).expanduser() if raw else DEFAULT_HOME


def ensure_home() -> Path:
    d = home()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def db_path() -> Path:
    return home() / "triage.db"


def config_path() -> Path:
    return home() / "config.toml"


def token_path() -> Path:
    return home() / "token.json"


def write_log_path() -> Path:
    return home() / "writes.log"
