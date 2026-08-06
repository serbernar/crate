from __future__ import annotations

import click

from . import api, auth, config, db, paths, stats as stats_mod, sync as sync_mod
from .config import ConfigError


def _load_config() -> config.Config:
    try:
        return config.load()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="crate")
def cli() -> None:
    """Sort Spotify Liked Songs into playlists."""


@cli.command()
def init() -> None:
    """Write a config template to ~/.config/crate/config.toml."""
    try:
        path = config.write_template()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"wrote {path}")
    click.echo("fill in spotify.client_id, then run: crate login")


@cli.command()
def migrate() -> None:
    """Create or upgrade the local database."""
    paths.ensure_home()
    engine = db.make_engine()
    before, after = db.upgrade(engine)
    if before == after:
        click.echo(f"schema up to date ({after})")
    else:
        click.echo(f"schema migrated {before or 'empty'} -> {after}")
    click.echo(f"database: {paths.db_path()}")


@cli.command()
@click.option("--no-browser", is_flag=True, help="print the URL instead of opening a browser")
def login(no_browser: bool) -> None:
    """Authorize against Spotify (Authorization Code + PKCE)."""
    cfg = _load_config()
    click.echo(f"redirect URI: {cfg.redirect_uri}")
    click.echo(f"scopes: {auth.SCOPE_STRING}")
    try:
        profile = auth.login(cfg, open_browser=not no_browser)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"authorized as {profile.get('display_name') or profile['id']} ({profile['id']})")
    click.echo(f"token: {paths.token_path()} ({auth.SecureFileCache().mode()})")


@cli.command()
def whoami() -> None:
    """Show the cached token and the account it belongs to."""
    cfg = _load_config()
    status = auth.token_status()
    if status is None:
        raise click.ClickException("not authenticated - run `crate login`")
    click.echo(f"token: {paths.token_path()} ({auth.SecureFileCache().mode()})")
    click.echo(f"scopes: {' '.join(status['scopes'])}")
    click.echo(f"expires_at: {status['expires_at']:%Y-%m-%d %H:%M:%S} UTC")
    click.echo(f"refresh_token: {'yes' if status['has_refresh_token'] else 'no'}")
    try:
        profile = auth.client(cfg).current_user()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"account: {profile.get('display_name') or profile['id']} ({profile['id']})")


@cli.command()
@click.option("--full", is_flag=True, help="walk the whole library instead of stopping at the first known track")
def sync(full: bool) -> None:
    """Pull new Liked Songs and the genres of their artists."""
    cfg = _load_config()
    paths.ensure_home()
    engine, factory = db.open_db()
    try:
        client = api.connect(cfg, on_wait=lambda s: click.echo(f"rate limited, waiting {s}s"))
    except auth.NotAuthenticated as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        with factory() as session:
            result = sync_mod.sync(session, client, full=full, progress=click.echo)
    except api.RateLimited as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        engine.dispose()
    click.echo(f"tracks added: {result.new_tracks}")
    click.echo(f"artists fetched: {result.artists_fetched}")
    click.echo(f"genres filled: {result.genres_filled}")
    if result.skipped_local:
        click.echo(f"skipped (local or unavailable): {result.skipped_local}")


@cli.command()
def stats() -> None:
    """Show how many tracks are pending, sorted and skipped."""
    try:
        cfg = config.load()
    except ConfigError:
        cfg = None  # counts do not need a config, only playlist names do
    engine, factory = db.open_db()
    try:
        with factory() as session:
            report = stats_mod.collect(session, cfg)
    finally:
        engine.dispose()
    for line in stats_mod.render(report):
        click.echo(line)


@cli.command()
def logout() -> None:
    """Delete the cached token."""
    removed = auth.SecureFileCache().clear()
    click.echo("token removed" if removed else "no token to remove")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
