import pytest

from crate import config
from crate.config import ConfigError

GOOD = """
[spotify]
client_id = "abc123"

[[playlists]]
hotkey = "1"
id = "pl_techno"
name = "Techno"

[[playlists]]
hotkey = "2"
id = "pl_jazz"
name = "Jazz"

[[rules]]
genre = "Minimal Techno"
playlist = "1"

[[rules]]
genre = "jazz"
playlist = "pl_jazz"
"""


def parse(text: str):
    import tomllib

    return config.parse(tomllib.loads(text))


def test_parses_playlists_and_rules():
    cfg = parse(GOOD)
    assert cfg.client_id == "abc123"
    assert cfg.redirect_uri == config.DEFAULT_REDIRECT_URI
    assert [p.hotkey for p in cfg.playlists] == ["1", "2"]
    assert cfg.by_hotkey("2").name == "Jazz"
    # genres are lowercased for substring matching; rules keep file order
    assert [(r.genre, r.playlist_id) for r in cfg.rules] == [
        ("minimal techno", "pl_techno"),
        ("jazz", "pl_jazz"),
    ]


def test_rule_order_is_preserved():
    cfg = parse(
        """
        [spotify]
        client_id = "x"
        [[playlists]]
        hotkey = "1"
        id = "p1"
        [[rules]]
        genre = "deep house"
        playlist = "1"
        [[rules]]
        genre = "house"
        playlist = "1"
        """
    )
    assert [r.genre for r in cfg.rules] == ["deep house", "house"]


@pytest.mark.parametrize(
    "text, message",
    [
        ('[spotify]\nclient_id = ""', "client_id"),
        ('[spotify]\nclient_id = "x"\n[[playlists]]\nhotkey = "0"\nid = "p"', "hotkey"),
        ('[spotify]\nclient_id = "x"\n[[playlists]]\nhotkey = "1"\nid = ""', "id is required"),
        (
            '[spotify]\nclient_id = "x"\n[[playlists]]\nhotkey = "1"\nid = "p1"\n'
            '[[playlists]]\nhotkey = "1"\nid = "p2"',
            "used twice",
        ),
        (
            '[spotify]\nclient_id = "x"\n[[playlists]]\nhotkey = "1"\nid = "p1"\n'
            '[[rules]]\ngenre = "techno"\nplaylist = "9"',
            "not declared",
        ),
    ],
)
def test_rejects_bad_config(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)


def test_template_round_trips(tmp_path):
    path = tmp_path / "config.toml"
    config.write_template(path)
    with pytest.raises(ConfigError, match="already exists"):
        config.write_template(path)
    # the template is intentionally incomplete: client_id must be filled in
    with pytest.raises(ConfigError, match="client_id"):
        config.load(path)


def test_missing_config_points_at_init(tmp_path):
    with pytest.raises(ConfigError, match="crate init"):
        config.load(tmp_path / "nope.toml")
