# crate

CLI for sorting Spotify Liked Songs into playlists. Local, single user.

## Setup

`pip install -e .` Create an app at https://developer.spotify.com/dashboard
with `http://127.0.0.1:8765/callback` as a redirect URI (no secret needed),
then `crate init`, fill in the config, `crate migrate`, `crate login`.

    crate sync      pull new Liked Songs (--full also refreshes known ones)
    crate learn     read your playlists to know what already lives in them
    crate triage    sort pending tracks into playlists
    crate apply     write the decisions to Spotify (--commit to do it)
    crate stats     pending / sorted / skipped counts

In triage: 1-9 toggle playlists (a track can go into several), enter saves or
accepts the suggestion, a applies it to every pending track by the same artist,
p plays the track, s skips, u undoes, q quits. Decisions save as made, and
`crate whoami` / `crate logout` handle the cached token.

## Config

    [spotify]
    client_id = "..."

    [[playlists]]
    hotkey = "1"
    id = "0000000000000000000000"
    name = "the gym"

    [[rules]]              # optional hand-written overrides
    genre = "hardstyle"
    playlist = "1"

Suggestions come from rules first, then from `crate learn`: an artist already
in a playlist, then a genre several of its tracks share. Each says why and none
applies itself. `apply` writes nothing without `--commit` and reads each
playlist first, so re-running never duplicates; writes go to `writes.log`.
Nothing is removed from Liked Songs. The API cannot see playlist folders.
