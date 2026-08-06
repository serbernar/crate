from datetime import datetime

import pytest

from crate import db, stats
from crate.config import Config, PlaylistCfg
from crate.models import Assignment, Decision, Playlist, Status, Track


@pytest.fixture()
def session(tmp_path):
    engine, factory = db.open_db(tmp_path / "crate.db")
    with factory() as s:
        yield s
    engine.dispose()


def add_track(session, track_id, *, genres=None, synced_at=datetime(2024, 6, 1), decision=None,
              playlists=()):
    session.add(
        Track(
            track_id=track_id,
            artist_ids=["a1"],
            title=f"Track {track_id}",
            artist_name="Artist",
            added_at=datetime(2024, 1, 1),
            genres=genres,
            synced_at=synced_at,
        )
    )
    session.flush()
    if decision is not None:
        session.add(decision)
    for playlist_id in playlists:
        session.add(
            Assignment(track_id=track_id, playlist_id=playlist_id, decided_at=datetime(2024, 6, 2))
        )
    session.commit()


def sorted_decision(track_id):
    return Decision(track_id=track_id, status=Status.sorted, decided_at=datetime(2024, 6, 2))


def test_empty_database(session):
    report = stats.collect(session)
    assert (report.total, report.pending, report.sorted, report.skipped) == (0, 0, 0, 0)
    assert report.last_sync is None
    assert "nothing synced yet - run `crate sync`" in stats.render(report)


def test_counts_by_status(session):
    add_track(session, "t1", genres=[], decision=Decision(track_id="t1", status=Status.pending))
    add_track(session, "t2", genres=[], decision=Decision(track_id="t2", status=Status.skipped))
    add_track(session, "t3", genres=[], decision=sorted_decision("t3"), playlists=["pl1"])

    report = stats.collect(session)
    assert (report.total, report.pending, report.sorted, report.skipped) == (3, 1, 1, 1)


def test_track_without_a_decision_counts_as_pending(session):
    add_track(session, "t1", genres=[])
    report = stats.collect(session)
    assert report.pending == 1
    assert report.total == 1


def test_playlist_breakdown_uses_config_names_and_sorts_by_count(session):
    for i in range(3):
        add_track(session, f"a{i}", genres=[], decision=sorted_decision(f"a{i}"), playlists=["pl_techno"])
    add_track(session, "b0", genres=[], decision=sorted_decision("b0"), playlists=["pl_jazz"])

    cfg = Config(
        client_id="x",
        playlists=(
            PlaylistCfg(hotkey="1", playlist_id="pl_techno", name="Techno"),
            PlaylistCfg(hotkey="2", playlist_id="pl_jazz", name="Jazz"),
        ),
    )
    report = stats.collect(session, cfg)
    assert report.by_playlist == [("pl_techno", "Techno", 3), ("pl_jazz", "Jazz", 1)]


def test_playlist_name_falls_back_to_database_then_to_id(session):
    add_track(session, "t1", genres=[], decision=sorted_decision("t1"), playlists=["pl_known"])
    add_track(session, "t2", genres=[], decision=sorted_decision("t2"), playlists=["pl_unknown"])
    session.add(Playlist(playlist_id="pl_known", name="From DB", hotkey="1"))
    session.commit()

    report = stats.collect(session)
    assert dict((pid, name) for pid, name, _c in report.by_playlist) == {
        "pl_known": "From DB",
        "pl_unknown": "pl_unknown",
    }


def test_reports_tracks_still_missing_genres(session):
    add_track(session, "t1", genres=None)
    add_track(session, "t2", genres=[])
    report = stats.collect(session)
    assert report.genres_missing == 1
    assert any("no genres yet" in line for line in stats.render(report))


def test_last_sync_is_the_most_recent(session):
    add_track(session, "t1", genres=[], synced_at=datetime(2024, 6, 1))
    add_track(session, "t2", genres=[], synced_at=datetime(2024, 7, 9, 12, 30))
    assert stats.collect(session).last_sync == datetime(2024, 7, 9, 12, 30)


def test_render_is_plain_text(session):
    add_track(session, "t1", genres=[], decision=sorted_decision("t1"), playlists=["pl1"])
    for line in stats.render(stats.collect(session)):
        assert line.isascii()
