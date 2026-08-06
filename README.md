# crate

CLI for sorting Spotify Liked Songs into playlists. Local, single user.

## Setup

`pip install -e .` Create an app at https://developer.spotify.com/dashboard
and add `http://127.0.0.1:8765/callback` as a redirect URI (no client secret
needed). Then `crate init`, fill in `~/.config/crate/config.toml` as below,
and run `crate migrate` and `crate login`.

## Commands

    crate sync      pull new Liked Songs (--full also refreshes known ones)
    crate triage    sort pending tracks into playlists
    crate stats     pending / sorted / skipped counts
    crate init / migrate / login    setup, see above
    crate whoami / logout          inspect or drop the cached token

In triage: 1-9 toggle playlists (a track can go into several), enter saves
the selection or accepts the suggestion, s skips, u undoes, q quits. Each
decision is saved as it is made.

## Config

    [spotify]
    client_id = "..."

    [[playlists]]
    hotkey = "1"
    id = "0000000000000000000000"
    name = "Techno"

    [[rules]]
    genre = "techno"
    playlist = "1"

Rules match genre substrings case-insensitively, in file order, first match
wins; no match means no suggestion. Scopes: `user-library-read`,
`playlist-read-private`, `playlist-modify-private`. Nothing is ever removed
from Liked Songs. Spotify's API cannot see or create playlist folders.
