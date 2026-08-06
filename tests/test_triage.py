from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from crate import db, triage
from crate.config import Config, PlaylistCfg, Rule
from crate.models import Decision, Playlist, Status, Track


@pytest.fixture()
def session(tmp_path):
    engine, factory = db.open_db(tmp_path / "crate.db")
    with factory() as s:
        yield s
    engine.dispose()


TECHNO = PlaylistCfg(hotkey="1", playlist_id="pl_techno", name="Techno")
JAZZ = PlaylistCfg(hotkey="2", playlist_id="pl_jazz", name="Jazz")


def make_config(rules=()):
    return Config(client_id="x", playlists=(TECHNO, JAZZ), rules=tuple(rules))


def add_tracks(session, specs):
    """specs: [(track_id, genres)] - added_at descends with the index."""
    newest = datetime(2024, 6, 1)
    for i, (track_id, genres) in enumerate(specs):
        session.add(
            Track(
                track_id=track_id,
                artist_ids=["a1"],
                title=f"Title {track_id}",
                artist_name="Artist",
                added_at=newest - timedelta(days=i),
                genres=genres,
                synced_at=newest,
            )
        )
        session.flush()
        session.add(Decision(track_id=track_id, status=Status.pending))
    session.commit()


class FakeTerminal:
    def __init__(self, keys):
        self.keys = list(keys)
        self.screens: list[list[str]] = []
        self._current: list[str] = []

    def clear(self):
        if self._current:
            self.screens.append(self._current)
        self._current = []

    def write(self, line=""):
        self._current.append(line)

    def read_key(self):
        if not self.keys:
            self.clear()
            return "q"
        key = self.keys.pop(0)
        self.clear()
        return key

    def text(self):
        return "\n".join("\n".join(s) for s in self.screens)


# --- suggestions -------------------------------------------------------------


def test_first_matching_rule_wins():
    cfg = make_config([Rule("techno", "pl_techno"), Rule("acid", "pl_jazz")])
    assert triage.suggest(cfg, ["acid techno"]).playlist_id == "pl_techno"


def test_rule_order_decides_between_overlapping_rules():
    cfg = make_config([Rule("deep house", "pl_jazz"), Rule("house", "pl_techno")])
    assert triage.suggest(cfg, ["deep house"]).playlist_id == "pl_jazz"
    reversed_cfg = make_config([Rule("house", "pl_techno"), Rule("deep house", "pl_jazz")])
    assert triage.suggest(reversed_cfg, ["deep house"]).playlist_id == "pl_techno"


def test_matching_is_a_case_insensitive_substring():
    cfg = make_config([Rule("techno", "pl_techno")])
    assert triage.suggest(cfg, ["Minimal Techno"]).playlist_id == "pl_techno"


def test_no_match_means_no_suggestion():
    cfg = make_config([Rule("techno", "pl_techno")])
    assert triage.suggest(cfg, ["ambient"]) is None
    assert triage.suggest(cfg, []) is None
    assert triage.suggest(cfg, None) is None
    assert triage.suggest(make_config(), ["techno"]) is None


# --- the loop ----------------------------------------------------------------


def test_hotkey_sorts_into_that_playlist(session):
    add_tracks(session, [("t1", ["techno"])])
    summary = triage.run(session, make_config(), FakeTerminal(["1"]))

    decision = session.get(Decision, "t1")
    assert (decision.status, decision.playlist_id) == (Status.sorted, "pl_techno")
    assert decision.decided_at is not None
    assert summary.sorted == 1


def test_skip_records_skipped_without_a_playlist(session):
    add_tracks(session, [("t1", ["techno"])])
    triage.run(session, make_config(), FakeTerminal(["s"]))
    decision = session.get(Decision, "t1")
    assert (decision.status, decision.playlist_id) == (Status.skipped, None)


def test_enter_accepts_the_suggestion(session):
    add_tracks(session, [("t1", ["minimal techno"])])
    cfg = make_config([Rule("techno", "pl_techno")])
    triage.run(session, cfg, FakeTerminal(["\r"]))
    assert session.get(Decision, "t1").playlist_id == "pl_techno"


def test_enter_without_a_suggestion_decides_nothing(session):
    add_tracks(session, [("t1", ["ambient"])])
    term = FakeTerminal(["\r", "q"])
    triage.run(session, make_config([Rule("techno", "pl_techno")]), term)
    assert session.get(Decision, "t1").status is Status.pending
    assert "no suggestion to accept" in term.text()


def test_tracks_are_offered_newest_first(session):
    add_tracks(session, [("newest", []), ("middle", []), ("oldest", [])])
    triage.run(session, make_config(), FakeTerminal(["s", "s", "s"]))
    order = [
        d.track_id
        for d in session.scalars(select(Decision).order_by(Decision.decided_at, Decision.track_id))
    ]
    assert order[0] == "newest"


def test_oldest_first_reverses_the_order(session):
    add_tracks(session, [("newest", []), ("oldest", [])])
    term = FakeTerminal(["s", "q"])
    triage.run(session, make_config(), term, oldest_first=True)
    assert session.get(Decision, "oldest").status is Status.skipped
    assert session.get(Decision, "newest").status is Status.pending


def test_each_decision_is_committed_before_the_next_track(session):
    """Quitting mid-session keeps every decision already made."""
    add_tracks(session, [("t1", []), ("t2", []), ("t3", [])])
    triage.run(session, make_config(), FakeTerminal(["1", "s", "q"]))

    session.expire_all()
    assert session.get(Decision, "t1").status is Status.sorted
    assert session.get(Decision, "t2").status is Status.skipped
    assert session.get(Decision, "t3").status is Status.pending


def test_undo_reverts_the_previous_decision_and_shows_it_again(session):
    add_tracks(session, [("t1", []), ("t2", [])])
    term = FakeTerminal(["1", "u", "2"])
    summary = triage.run(session, make_config(), term)

    # t1 was sorted, undone, then sorted again into the other playlist
    assert session.get(Decision, "t1").playlist_id == "pl_jazz"
    assert summary.undone == 1
    assert summary.sorted == 1


def test_undo_returns_to_the_track_that_was_on_screen(session):
    add_tracks(session, [("t1", []), ("t2", [])])
    term = FakeTerminal(["s", "u", "1", "2"])
    triage.run(session, make_config(), term)

    assert session.get(Decision, "t1").playlist_id == "pl_techno"  # the undone one, redecided
    assert session.get(Decision, "t2").playlist_id == "pl_jazz"    # the one interrupted by undo


def test_undo_with_nothing_to_undo_says_so(session):
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["u", "q"])
    summary = triage.run(session, make_config(), term)
    assert "nothing to undo" in term.text()
    assert summary.undone == 0
    assert session.get(Decision, "t1").status is Status.pending


def test_undo_after_skip_keeps_counters_straight(session):
    add_tracks(session, [("t1", []), ("t2", [])])
    term = FakeTerminal(["s", "u", "q"])
    summary = triage.run(session, make_config(), term)
    assert (summary.sorted, summary.skipped, summary.undone) == (0, 0, 1)


def test_unbound_hotkey_is_refused(session):
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["7", "q"])
    triage.run(session, make_config(), term)
    assert "no playlist bound to 7" in term.text()
    assert session.get(Decision, "t1").status is Status.pending


def test_unknown_key_shows_help_and_keeps_the_track(session):
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["x", "q"])
    triage.run(session, make_config(), term)
    assert "keys: 1-9 playlist" in term.text()
    assert session.get(Decision, "t1").status is Status.pending


def test_quit_stops_immediately(session):
    add_tracks(session, [("t1", []), ("t2", [])])
    summary = triage.run(session, make_config(), FakeTerminal(["q"]))
    assert summary.quit is True
    assert session.get(Decision, "t2").status is Status.pending


def test_finishing_the_queue_ends_without_quit(session):
    add_tracks(session, [("t1", [])])
    summary = triage.run(session, make_config(), FakeTerminal(["s"]))
    assert summary.quit is False


def test_already_decided_tracks_are_not_offered_again(session):
    add_tracks(session, [("t1", []), ("t2", [])])
    triage.run(session, make_config(), FakeTerminal(["s", "s"]))
    summary = triage.run(session, make_config(), FakeTerminal(["s"]))
    assert (summary.sorted, summary.skipped) == (0, 0)


def test_screen_shows_track_genres_and_suggestion(session):
    add_tracks(session, [("t1", ["minimal techno", "acid"])])
    cfg = make_config([Rule("techno", "pl_techno")])
    term = FakeTerminal(["1"])
    triage.run(session, cfg, term)

    screen = term.screens[0]
    assert "Title t1" in screen
    assert "Artist" in screen
    assert "genres: minimal techno, acid" in screen
    assert "suggested: 1 Techno" in screen
    assert any("1 Techno   2 Jazz" in line for line in screen)


def test_screen_is_plain_text(session):
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["s"])
    triage.run(session, make_config(), term)
    for line in term.screens[0]:
        assert line.isascii()


def test_playlists_from_config_are_mirrored_into_the_database(session):
    add_tracks(session, [("t1", [])])
    triage.run(session, make_config(), FakeTerminal(["q"]))
    rows = {p.playlist_id: (p.name, p.hotkey) for p in session.scalars(select(Playlist))}
    assert rows == {"pl_techno": ("Techno", "1"), "pl_jazz": ("Jazz", "2")}


def test_hotkeys_can_be_reassigned_between_runs(session):
    add_tracks(session, [("t1", [])])
    triage.run(session, make_config(), FakeTerminal(["q"]))
    swapped = Config(
        client_id="x",
        playlists=(
            PlaylistCfg(hotkey="2", playlist_id="pl_techno", name="Techno"),
            PlaylistCfg(hotkey="1", playlist_id="pl_jazz", name="Jazz"),
        ),
    )
    triage.run(session, swapped, FakeTerminal(["q"]))
    rows = {p.playlist_id: p.hotkey for p in session.scalars(select(Playlist))}
    assert rows == {"pl_techno": "2", "pl_jazz": "1"}


def test_track_without_a_decision_row_is_triaged(session):
    session.add(
        Track(
            track_id="orphan",
            artist_ids=[],
            title="Orphan",
            artist_name="A",
            added_at=datetime(2024, 6, 1),
            genres=[],
            synced_at=datetime(2024, 6, 1),
        )
    )
    session.commit()
    triage.run(session, make_config(), FakeTerminal(["1"]))
    assert session.get(Decision, "orphan").playlist_id == "pl_techno"
