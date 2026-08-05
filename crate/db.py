"""SQLite access and schema migrations.

Schema version is tracked with PRAGMA user_version. Migrations are applied in
order inside a single transaction each; a failed migration leaves the previous
version intact.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from . import paths

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE tracks (
            track_id    TEXT PRIMARY KEY,
            artist_ids  TEXT NOT NULL,          -- JSON array of artist ids
            title       TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            added_at    TEXT NOT NULL,          -- ISO-8601 UTC, from Spotify
            genres_json TEXT,                   -- JSON array; NULL until artists are fetched
            synced_at   TEXT NOT NULL
        );
        CREATE INDEX idx_tracks_added_at ON tracks(added_at DESC);

        CREATE TABLE playlists (
            playlist_id TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            hotkey      TEXT UNIQUE
        );

        CREATE TABLE decisions (
            track_id    TEXT PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
            status      TEXT NOT NULL CHECK (status IN ('pending', 'sorted', 'skipped')),
            playlist_id TEXT,
            decided_at  TEXT,
            CHECK ((status = 'sorted') = (playlist_id IS NOT NULL))
        );
        CREATE INDEX idx_decisions_status ON decisions(status);

        CREATE TABLE artists (
            artist_id   TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            genres_json TEXT NOT NULL,          -- JSON array
            fetched_at  TEXT NOT NULL
        );
        """,
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or paths.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> tuple[int, int]:
    """Bring the database up to SCHEMA_VERSION. Returns (from, to)."""
    start = current_version(conn)
    for version, script in MIGRATIONS:
        if version <= start:
            continue
        # executescript() commits any open transaction first, so BEGIN/COMMIT
        # have to live inside the script itself.
        try:
            conn.executescript(
                f"BEGIN;\n{script}\nPRAGMA user_version = {version:d};\nCOMMIT;"
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return start, current_version(conn)


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """Connect and migrate. Every command uses this."""
    conn = connect(path)
    migrate(conn)
    return conn


# --- small helpers shared by the commands ------------------------------------


def upsert_playlists(conn: sqlite3.Connection, rows: Iterable[Sequence[str]]) -> None:
    """rows: (playlist_id, name, hotkey|None). Hotkeys are unique, so stale ones
    are cleared first to allow re-assignment between runs."""
    rows = list(rows)
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE playlists SET hotkey = NULL")
        conn.executemany(
            """
            INSERT INTO playlists (playlist_id, name, hotkey) VALUES (?, ?, ?)
            ON CONFLICT(playlist_id) DO UPDATE SET name = excluded.name,
                                                   hotkey = excluded.hotkey
            """,
            rows,
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def loads(value: str | None) -> list:
    return json.loads(value) if value else []


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
