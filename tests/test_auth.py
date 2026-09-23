import json
import stat
import time

from crate import auth
from crate.config import Config


def cfg() -> Config:
    return Config(client_id="abc123")


def token(**over) -> dict:
    base = {
        "access_token": "at",
        "refresh_token": "rt",
        "scope": auth.SCOPE_STRING,
        "expires_at": int(time.time()) + 3600,
        "token_type": "Bearer",
    }
    base.update(over)
    return base


def test_scopes_are_read_only_on_the_library():
    assert "user-library-modify" not in auth.SCOPES
    assert set(auth.SCOPES) == {
        "user-library-read",
        "playlist-read-private",
        "playlist-modify-private",
    }


def test_token_file_is_written_0600(tmp_path):
    path = tmp_path / "token.json"
    cache = auth.SecureFileCache(path)
    cache.save_token_to_cache(token())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["refresh_token"] == "rt"
    assert cache.get_cached_token()["access_token"] == "at"


def test_token_file_stays_0600_when_overwritten(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("{}")
    path.chmod(0o644)
    cache = auth.SecureFileCache(path)
    cache.save_token_to_cache(token())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_missing_and_corrupt_token_read_as_absent(tmp_path):
    cache = auth.SecureFileCache(tmp_path / "token.json")
    assert cache.get_cached_token() is None
    assert cache.clear() is False
    (tmp_path / "token.json").write_text("not json")
    assert cache.get_cached_token() is None


def test_auth_manager_uses_pkce_without_secret(tmp_path):
    manager = auth.auth_manager(cfg(), cache=auth.SecureFileCache(tmp_path / "t.json"))
    assert manager.redirect_uri == "http://127.0.0.1:8765/callback"
    url = manager.get_authorize_url()
    assert "code_challenge_method=S256" in url
    assert "client_id=abc123" in url
    assert "client_secret" not in url


def test_client_refuses_without_token(tmp_path):
    cache = auth.SecureFileCache(tmp_path / "token.json")
    try:
        auth.client(cfg(), cache=cache)
    except auth.NotAuthenticated as exc:
        assert "crate login" in str(exc)
    else:
        raise AssertionError("expected NotAuthenticated")


def test_token_status_reports_scopes_and_expiry(tmp_path):
    cache = auth.SecureFileCache(tmp_path / "token.json")
    assert auth.token_status(cache) is None
    cache.save_token_to_cache(token())
    status = auth.token_status(cache)
    assert status["scopes"] == sorted(auth.SCOPES)
    assert status["has_refresh_token"] is True
    assert status["expires_at"].tzinfo is not None


def test_token_with_stale_scopes_is_rejected(tmp_path):
    """Adding a scope later must force a re-login rather than silently pass."""
    cache = auth.SecureFileCache(tmp_path / "token.json")
    cache.save_token_to_cache(token(scope="user-library-read"))
    manager = auth.auth_manager(cfg(), cache=cache)
    assert auth.cached_token(manager) is None
