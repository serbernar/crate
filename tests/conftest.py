import pytest

from crate import paths


@pytest.fixture(autouse=True)
def crate_home(tmp_path, monkeypatch):
    """Keep every test off the real ~/.config/crate."""
    home = tmp_path / "home"
    monkeypatch.setenv(paths.ENV_HOME, str(home))
    return home
