"""Append-only record of everything written to Spotify.

One line per track added, so the log answers "when did this track end up in
this playlist" on its own. Tab separated: timestamp, endpoint, track, playlist.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from . import paths

ADD_TRACKS = "POST /v1/playlists/{playlist_id}/tracks"


class WriteLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.write_log_path()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a")

    def record(self, endpoint: str, track_ids: list[str], playlist_id: str) -> None:
        """Log one write call, a line per track it carried."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._open() as fh:
            for track_id in track_ids:
                fh.write(f"{stamp}\t{endpoint}\t{track_id}\t{playlist_id}\n")

    def lines(self) -> list[str]:
        try:
            return self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
