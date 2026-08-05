# crate

CLI for sorting Spotify Liked Songs into playlists. Local, single user.

## Setup

1. `pip install -e .`
2. Create an app at https://developer.spotify.com/dashboard, add
   `http://127.0.0.1:8765/callback` as a redirect URI. No client secret needed.
3. `crate init`, then put the client id into `~/.config/crate/config.toml`.
4. `crate migrate`, then `crate login`.

## Commands

    crate init      write the config template
    crate migrate   create or upgrade the database
    crate login     authorize (Authorization Code + PKCE)
    crate whoami    show the cached token and account
    crate logout    delete the cached token

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
wins. No match means no suggestion.

Scopes: `user-library-read`, `playlist-read-private`, `playlist-modify-private`.
Nothing is ever removed from Liked Songs; `user-library-modify` is not requested.
