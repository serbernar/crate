from datetime import datetime

import pytest
import spotipy
from sqlalchemy import select

from crate import api, db, learn, triage
from crate.config import Config, PlaylistCfg, Rule
from crate.models import Artist, PlaylistArtist, PlaylistGenre, Track
from tests.fakes import FakePlaylists

GYM = PlaylistCfg(hotkey="1", playlist_id="pl_gym", name="the gym")
MORNING = PlaylistCfg(hotkey="2", playlist_id="pl_morning", name="morning coffee")
CFG = Config(client_id="x", playlists=(GYM, MORNING))


@pytest.fixture()
def session(tmp_path):
    engine, factory = db.open_db(tmp_path / "crate.db")
    with factory() as s:
        yield s
    engine.dispose()


def client_for(sp):
    return api.Client(sp, sleep=lambda _s: None)


def with_tracks(contents, artists=None):
    """contents: {playlist_id: [(track_id, [artist_id, ...]), ...]}"""
    sp = FakePlaylists(artists=artists or {})
    sp.playlist_detail = {}
    for playlist_id, tracks in contents.items():
        sp.contents[playlist_id] = [track_id for track_id, _ in tracks]
        sp.playlist_detail[playlist_id] = [
            {"id": track_id, "artists": [{"id": a, "name": f"Name {a}"} for a in artist_ids]}
            for track_id, artist_ids in tracks
        ]
    return sp


def test_counts_tracks_per_artist(session):
    sp = with_tracks(
        {"pl_gym": [("t1", ["a1"]), ("t2", ["a1"]), ("t3", ["a2"])], "pl_morning": []},
        artists={"a1": ["hardstyle"], "a2": ["techno"]},
    )
    result = learn.learn(session, client_for(sp), CFG)

    rows = {
        (r.playlist_id, r.artist_id): r.tracks for r in session.scalars(select(PlaylistArtist))
    }
    assert rows == {("pl_gym", "a1"): 2, ("pl_gym", "a2"): 1}
    gym = next(p for p in result.profiles if p.playlist_id == "pl_gym")
    assert (gym.tracks, gym.artists) == (3, 2)


def test_genres_are_counted_through_the_artists(session):
    sp = with_tracks(
        {"pl_gym": [("t1", ["a1"]), ("t2", ["a1"]), ("t3", ["a2"])]},
        artists={"a1": ["hardstyle", "techno"], "a2": ["techno"]},
    )
    learn.learn(session, client_for(sp), CFG)

    rows = {r.genre: r.tracks for r in session.scalars(select(PlaylistGenre))}
    assert rows == {"hardstyle": 2, "techno": 3}


def test_artists_are_fetched_once_and_reused(session):
    sp = with_tracks({"pl_gym": [("t1", ["a1"])]}, artists={"a1": ["techno"]})
    result = learn.learn(session, client_for(sp), CFG)
    assert result.artists_fetched == 1
    assert session.get(Artist, "a1").genres == ["techno"]

    sp2 = with_tracks({"pl_gym": [("t1", ["a1"])]}, artists={"a1": ["techno"]})
    again = learn.learn(session, client_for(sp2), CFG)
    assert again.artists_fetched == 0
    assert sp2.artist_calls == []


def test_relearning_replaces_the_previous_profile(session):
    sp = with_tracks({"pl_gym": [("t1", ["a1"]), ("t2", ["a2"])]},
                     artists={"a1": ["techno"], "a2": ["jazz"]})
    learn.learn(session, client_for(sp), CFG)

    sp2 = with_tracks({"pl_gym": [("t1", ["a1"])]}, artists={"a1": ["techno"]})
    learn.learn(session, client_for(sp2), CFG)

    rows = {r.artist_id for r in session.scalars(select(PlaylistArtist))}
    assert rows == {"a1"}  # a2 is gone, not merged


def test_only_configured_playlists_are_read(session):
    sp = with_tracks({"pl_gym": [("t1", ["a1"])], "pl_other": [("t9", ["a9"])]},
                     artists={"a1": [], "a9": []})
    learn.learn(session, client_for(sp), CFG)
    assert {pid for pid, _offset in sp.read_calls} == {"pl_gym", "pl_morning"}


def test_unreadable_playlist_is_reported_and_the_rest_continue(session):
    sp = with_tracks({"pl_morning": [("t1", ["a1"])]}, artists={"a1": []})
    sp.read_errors["pl_gym"] = spotipy.SpotifyException(404, -1, "Not found")

    result = learn.learn(session, client_for(sp), CFG)

    gym = next(p for p in result.profiles if p.playlist_id == "pl_gym")
    assert "cannot read playlist" in gym.error
    assert any(r.playlist_id == "pl_morning" for r in session.scalars(select(PlaylistArtist)))


def test_paginates_a_long_playlist(session):
    tracks = [(f"t{i}", ["a1"]) for i in range(250)]
    sp = with_tracks({"pl_gym": tracks}, artists={"a1": []})
    learn.learn(session, client_for(sp), CFG)
    assert [o for pid, o in sp.read_calls if pid == "pl_gym"] == [0, 100, 200]


# --- how the evidence is used ------------------------------------------------


def evidence_from(session):
    return triage.Evidence.load(session, CFG)


def learned(session, contents, artists):
    sp = with_tracks(contents, artists=artists)
    learn.learn(session, client_for(sp), CFG)
    return evidence_from(session)


def test_an_artist_already_in_a_playlist_is_suggested(session):
    ev = learned(session, {"pl_gym": [("t1", ["a1"]), ("t2", ["a1"])]}, {"a1": []})
    suggestion = triage.suggest(CFG, [], ["a1"], ev)
    assert suggestion.playlist.playlist_id == "pl_gym"
    assert suggestion.reason == "2 tracks by this artist already there"


def test_a_single_track_by_the_artist_is_enough(session):
    ev = learned(session, {"pl_gym": [("t1", ["a1"])]}, {"a1": []})
    assert triage.suggest(CFG, [], ["a1"], ev).reason == "1 track by this artist already there"


def test_the_playlist_with_more_of_the_artist_wins(session):
    ev = learned(
        session,
        {"pl_gym": [("t1", ["a1"])], "pl_morning": [("t2", ["a1"]), ("t3", ["a1"])]},
        {"a1": []},
    )
    assert triage.suggest(CFG, [], ["a1"], ev).playlist.playlist_id == "pl_morning"


def test_genre_evidence_needs_several_tracks(session):
    two = {"pl_gym": [("t1", ["a1"]), ("t2", ["a2"])]}
    ev = learned(session, two, {"a1": ["ambient"], "a2": ["ambient"]})
    assert triage.suggest(CFG, ["ambient"], ["new"], ev) is None

    three = {"pl_gym": [("t1", ["a1"]), ("t2", ["a2"]), ("t3", ["a3"])]}
    ev = learned(session, three, {"a1": ["ambient"], "a2": ["ambient"], "a3": ["ambient"]})
    suggestion = triage.suggest(CFG, ["ambient"], ["new"], ev)
    assert suggestion.playlist.playlist_id == "pl_gym"
    assert suggestion.reason == "3 ambient tracks already there"


def test_artist_evidence_beats_genre_evidence(session):
    ev = learned(
        session,
        {
            "pl_gym": [("t1", ["a1"]), ("t2", ["a2"]), ("t3", ["a3"]), ("t4", ["a4"])],
            "pl_morning": [("t5", ["a9"])],
        },
        {"a1": ["ambient"], "a2": ["ambient"], "a3": ["ambient"], "a4": ["ambient"],
         "a9": ["ambient"]},
    )
    # a9 sits in morning once; ambient is all over the gym
    assert triage.suggest(CFG, ["ambient"], ["a9"], ev).playlist.playlist_id == "pl_morning"


def test_a_config_rule_beats_learned_evidence(session):
    ev = learned(session, {"pl_gym": [("t1", ["a1"]), ("t2", ["a1"])]}, {"a1": ["techno"]})
    cfg = Config(client_id="x", playlists=(GYM, MORNING),
                 rules=(Rule("techno", "pl_morning"),))
    suggestion = triage.suggest(cfg, ["techno"], ["a1"], ev)
    assert suggestion.playlist.playlist_id == "pl_morning"
    assert suggestion.reason == "rule: techno"


def test_no_evidence_means_no_suggestion(session):
    ev = learned(session, {"pl_gym": [("t1", ["a1"])]}, {"a1": ["techno"]})
    assert triage.suggest(CFG, ["ambient"], ["unknown"], ev) is None


def test_evidence_ignores_playlists_dropped_from_config(session):
    ev = learned(session, {"pl_gym": [("t1", ["a1"])]}, {"a1": []})
    smaller = Config(client_id="x", playlists=(MORNING,))
    assert triage.suggest(smaller, [], ["a1"], triage.Evidence.load(session, smaller)) is None


def test_suggestion_reaches_the_triage_screen(session):
    learned(session, {"pl_gym": [("t1", ["a1"]), ("t2", ["a1"])]}, {"a1": []})
    session.add(
        Track(
            track_id="new",
            artist_ids=["a1"],
            title="New Track",
            artist_name="Name a1",
            added_at=datetime(2024, 6, 1),
            genres=[],
            synced_at=datetime(2024, 6, 1),
        )
    )
    session.commit()

    from tests.test_triage import FakeTerminal

    term = FakeTerminal(["\r"])
    triage.run(session, CFG, term)

    assert any(
        "suggested: 1 the gym  (2 tracks by this artist already there)" in line
        for line in term.screens[0]
    )
