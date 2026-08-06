from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from crate import db, triage
from crate.config import Config, PlaylistCfg, Rule
from crate.models import Assignment, Decision, Playlist, Status, Track


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
                release_date="1998-05-21",
                popularity=42,
                synced_at=newest,
            )
        )
        session.flush()
        session.add(Decision(track_id=track_id, status=Status.pending))
    session.commit()


def playlists_of(session, track_id):
    session.expire_all()
    return set(
        session.scalars(select(Assignment.playlist_id).where(Assignment.track_id == track_id))
    )


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
    assert triage.suggest(cfg, ["acid techno"]).playlist.playlist_id == "pl_techno"


def test_rule_order_decides_between_overlapping_rules():
    cfg = make_config([Rule("deep house", "pl_jazz"), Rule("house", "pl_techno")])
    assert triage.suggest(cfg, ["deep house"]).playlist.playlist_id == "pl_jazz"
    reversed_cfg = make_config([Rule("house", "pl_techno"), Rule("deep house", "pl_jazz")])
    assert triage.suggest(reversed_cfg, ["deep house"]).playlist.playlist_id == "pl_techno"


def test_matching_is_a_case_insensitive_substring():
    cfg = make_config([Rule("techno", "pl_techno")])
    assert triage.suggest(cfg, ["Minimal Techno"]).playlist.playlist_id == "pl_techno"


def test_no_match_means_no_suggestion():
    cfg = make_config([Rule("techno", "pl_techno")])
    assert triage.suggest(cfg, ["ambient"]) is None
    assert triage.suggest(cfg, []) is None
    assert triage.suggest(cfg, None) is None
    assert triage.suggest(make_config(), ["techno"]) is None


# --- the loop ----------------------------------------------------------------


def test_hotkey_sorts_into_that_playlist(session):
    add_tracks(session, [("t1", ["techno"])])
    summary = triage.run(session, make_config(), FakeTerminal(["1", "\r"]))

    decision = session.get(Decision, "t1")
    assert decision.status is Status.sorted
    assert decision.decided_at is not None
    assert playlists_of(session, "t1") == {"pl_techno"}
    assert summary.sorted == 1


def test_skip_records_skipped_without_a_playlist(session):
    add_tracks(session, [("t1", ["techno"])])
    triage.run(session, make_config(), FakeTerminal(["s"]))
    decision = session.get(Decision, "t1")
    assert decision.status is Status.skipped
    assert playlists_of(session, "t1") == set()


def test_enter_accepts_the_suggestion(session):
    add_tracks(session, [("t1", ["minimal techno"])])
    cfg = make_config([Rule("techno", "pl_techno")])
    triage.run(session, cfg, FakeTerminal(["\r"]))
    assert playlists_of(session, "t1") == {"pl_techno"}


def test_enter_without_a_suggestion_decides_nothing(session):
    add_tracks(session, [("t1", ["ambient"])])
    term = FakeTerminal(["\r", "q"])
    triage.run(session, make_config([Rule("techno", "pl_techno")]), term)
    assert session.get(Decision, "t1").status is Status.pending
    assert "nothing selected and nothing suggested" in term.text()


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
    triage.run(session, make_config(), FakeTerminal(["1", "\r", "s", "q"]))

    session.expire_all()
    assert session.get(Decision, "t1").status is Status.sorted
    assert session.get(Decision, "t2").status is Status.skipped
    assert session.get(Decision, "t3").status is Status.pending


def test_undo_reverts_the_previous_decision_and_shows_it_again(session):
    add_tracks(session, [("t1", []), ("t2", [])])
    term = FakeTerminal(["1", "\r", "u", "2", "\r"])
    summary = triage.run(session, make_config(), term)

    # t1 was sorted, undone, then sorted again into the other playlist
    assert playlists_of(session, "t1") == {"pl_jazz"}
    assert summary.undone == 1
    assert summary.sorted == 1


def test_undo_returns_to_the_track_that_was_on_screen(session):
    add_tracks(session, [("t1", []), ("t2", [])])
    term = FakeTerminal(["s", "u", "1", "\r", "2", "\r"])
    triage.run(session, make_config(), term)

    assert playlists_of(session, "t1") == {"pl_techno"}  # the undone one, redecided
    assert playlists_of(session, "t2") == {"pl_jazz"}    # the one interrupted by undo


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
    assert "keys: 1-9 toggle playlist" in term.text()
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
    term = FakeTerminal(["1", "\r"])
    triage.run(session, cfg, term)

    screen = term.screens[0]
    assert "Title t1" in screen
    assert "Artist" in screen
    assert "genres: minimal techno, acid" in screen
    assert any(line.startswith("suggested: 1 Techno") for line in screen)
    assert any("1 Techno" in line and "2 Jazz" in line for line in screen)


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
    triage.run(session, make_config(), FakeTerminal(["1", "\r"]))
    assert playlists_of(session, "orphan") == {"pl_techno"}


# --- several playlists per track ---------------------------------------------


def test_a_track_can_go_into_several_playlists(session):
    add_tracks(session, [("t1", [])])
    summary = triage.run(session, make_config(), FakeTerminal(["1", "2", "\r"]))

    assert playlists_of(session, "t1") == {"pl_techno", "pl_jazz"}
    assert session.get(Decision, "t1").status is Status.sorted
    assert (summary.sorted, summary.assignments) == (1, 2)


def test_pressing_a_hotkey_twice_deselects_it(session):
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["1", "2", "1", "\r"])
    triage.run(session, make_config(), term)
    assert playlists_of(session, "t1") == {"pl_jazz"}


def test_selection_is_shown_and_marked(session):
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["1", "\r"])
    triage.run(session, make_config(), term)

    after_toggle = term.screens[1]
    assert "selected: Techno" in after_toggle
    assert any(line.startswith("*1 Techno") for line in after_toggle)


def test_selection_overrides_the_suggestion(session):
    add_tracks(session, [("t1", ["techno"])])
    cfg = make_config([Rule("techno", "pl_techno")])
    triage.run(session, cfg, FakeTerminal(["2", "\r"]))
    assert playlists_of(session, "t1") == {"pl_jazz"}


def test_redeciding_replaces_the_previous_playlists(session):
    add_tracks(session, [("t1", [])])
    triage.run(session, make_config(), FakeTerminal(["1", "2", "\r", "u", "2", "\r"]))
    assert playlists_of(session, "t1") == {"pl_jazz"}


def test_undoing_the_only_track_does_not_show_it_twice(session):
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["1", "\r", "u", "2", "\r"])
    summary = triage.run(session, make_config(), term)
    assert playlists_of(session, "t1") == {"pl_jazz"}
    assert summary.quit is False  # the queue really is empty afterwards


def test_undo_clears_the_assignments(session):
    add_tracks(session, [("t1", []), ("t2", [])])
    summary = triage.run(session, make_config(), FakeTerminal(["1", "2", "\r", "u", "q"]))
    assert playlists_of(session, "t1") == set()
    assert session.get(Decision, "t1").status is Status.pending
    assert (summary.sorted, summary.assignments) == (0, 0)


def test_a_sorted_track_is_not_offered_again(session):
    add_tracks(session, [("t1", [])])
    triage.run(session, make_config(), FakeTerminal(["1", "\r"]))
    summary = triage.run(session, make_config(), FakeTerminal(["2", "\r"]))
    assert (summary.sorted, summary.assignments) == (0, 0)
    assert playlists_of(session, "t1") == {"pl_techno"}


def test_screen_shows_release_year_and_popularity(session):
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["s"])
    triage.run(session, make_config(), term)
    line = next(line for line in term.screens[0] if "added" in line)
    assert "released 1998" in line
    assert "popularity 42" in line


def test_the_last_decision_can_still_be_undone(session):
    """The queue empties after the final track; undo must still be reachable."""
    add_tracks(session, [("t1", [])])
    term = FakeTerminal(["s", "u", "1", "\r", "q"])
    summary = triage.run(session, make_config(), term)

    assert "nothing left to triage" in term.text()
    assert playlists_of(session, "t1") == {"pl_techno"}
    assert (summary.sorted, summary.skipped, summary.undone) == (1, 0, 1)


def test_quitting_from_the_empty_screen_is_not_an_early_exit(session):
    add_tracks(session, [("t1", [])])
    summary = triage.run(session, make_config(), FakeTerminal(["s", "q"]))
    assert summary.quit is False


# --- whole artist at once, and previewing -------------------------------------


def add_track_by(session, track_id, artist_ids, *, artist_name="Artist", days=0):
    session.add(
        Track(
            track_id=track_id,
            artist_ids=artist_ids,
            title=f"Title {track_id}",
            artist_name=artist_name,
            added_at=datetime(2024, 6, 1) - timedelta(days=days),
            genres=[],
            release_date="2001",
            popularity=1,
            synced_at=datetime(2024, 6, 1),
        )
    )
    session.flush()
    session.add(Decision(track_id=track_id, status=Status.pending))
    session.commit()


def test_a_applies_the_decision_to_the_whole_artist(session):
    for i in range(3):
        add_track_by(session, f"t{i}", ["a1"], days=i)
    add_track_by(session, "other", ["a2"], days=9)

    summary = triage.run(session, make_config(), FakeTerminal(["1", "a", "s"]))

    for i in range(3):
        assert playlists_of(session, f"t{i}") == {"pl_techno"}
    assert session.get(Decision, "other").status is Status.skipped
    assert (summary.sorted, summary.assignments) == (3, 3)


def test_a_offers_the_count_of_siblings(session):
    for i in range(3):
        add_track_by(session, f"t{i}", ["a1"], days=i)
    term = FakeTerminal(["q"])
    triage.run(session, make_config(), term)
    assert any("a apply to 2 more tracks by this artist" in line for line in term.screens[0])


def test_a_is_not_offered_without_siblings(session):
    add_track_by(session, "t1", ["a1"])
    term = FakeTerminal(["q"])
    triage.run(session, make_config(), term)
    assert not any("by this artist" in line for line in term.screens[0])


def test_a_without_siblings_says_so(session):
    add_track_by(session, "t1", ["a1"])
    term = FakeTerminal(["1", "a", "q"])
    triage.run(session, make_config(), term)
    assert "no other pending tracks by this artist" in term.text()
    assert session.get(Decision, "t1").status is Status.pending


def test_a_matches_any_shared_artist(session):
    add_track_by(session, "solo", ["a1"], days=0)
    add_track_by(session, "feat", ["a2", "a1"], days=1)
    add_track_by(session, "unrelated", ["a3"], days=2)

    triage.run(session, make_config(), FakeTerminal(["1", "a", "s"]))

    assert playlists_of(session, "feat") == {"pl_techno"}
    assert session.get(Decision, "unrelated").status is Status.skipped


def test_a_can_be_undone_one_track_at_a_time(session):
    for i in range(2):
        add_track_by(session, f"t{i}", ["a1"], days=i)
    summary = triage.run(session, make_config(), FakeTerminal(["1", "a", "u", "q"]))

    assert playlists_of(session, "t1") == set()  # the sibling was undone
    assert playlists_of(session, "t0") == {"pl_techno"}
    assert (summary.sorted, summary.undone) == (1, 1)


def test_a_only_takes_pending_tracks(session):
    for i in range(3):
        add_track_by(session, f"t{i}", ["a1"], days=i)
    triage.run(session, make_config(), FakeTerminal(["s"]))  # t0 skipped, rest pending

    triage.run(session, make_config(), FakeTerminal(["2", "a"]))

    assert playlists_of(session, "t1") == {"pl_jazz"}
    assert playlists_of(session, "t2") == {"pl_jazz"}
    assert session.get(Decision, "t0").status is Status.skipped  # untouched
    assert playlists_of(session, "t0") == set()


def test_p_opens_the_track_without_deciding(session):
    add_track_by(session, "t1", ["a1"])
    opened: list[str] = []
    term = FakeTerminal(["p", "q"])

    triage.run(session, make_config(), term, player=opened.append)

    assert opened == ["t1"]
    assert session.get(Decision, "t1").status is Status.pending
    assert "opened Title t1 in Spotify" in term.text()


def test_p_is_offered_on_screen(session):
    add_track_by(session, "t1", ["a1"])
    term = FakeTerminal(["q"])
    triage.run(session, make_config(), term)
    assert any("p play" in line for line in term.screens[0])


def test_open_in_spotify_builds_a_track_uri():
    seen: list[str] = []
    triage.open_in_spotify("abc123", opener=seen.append)
    assert seen == ["spotify:track:abc123"]
