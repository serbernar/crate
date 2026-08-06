# crate

CLI for sorting Spotify Liked Songs into playlists. Local, single user.

## Setup

`pip install -e .` Create an app at https://developer.spotify.com/dashboard,
add `http://127.0.0.1:8765/callback` as a redirect URI (no client secret
needed), then `crate init`, fill in the config, `crate migrate`, `crate login`.

## Commands

    crate sync      pull new Liked Songs (--full also refreshes known ones)
    crate triage    sort pending tracks into playlists
    crate apply     write the decisions to Spotify (--commit to do it)
    crate stats     pending / sorted / skipped counts
    crate whoami / logout   the cached token

In triage: 1-9 toggle playlists (a track can go into several), enter saves
or accepts the suggestion, s skips, u undoes, q quits. Decisions save as
they are made.

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
wins; no match means no suggestion. `apply` writes nothing without
`--commit`; it reads each playlist first, so re-running never duplicates, and
appends every write to `writes.log`. Nothing is ever removed from Liked Songs,
and Spotify's API cannot see or create playlist folders.
