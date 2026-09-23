import stat
from datetime import datetime, timedelta

import pytest
import spotipy
from sqlalchemy import select

from crate import api, apply, db
from crate.config import Config, PlaylistCfg
from crate.models import Assignment, Decision, Status, Track
from crate.writelog import WriteLog
from tests.fakes import FakePlaylists

TECHNO = PlaylistCfg(hotkey="1", playlist_id="pl_techno", name="Techno")
JAZZ = PlaylistCfg(hotkey="2", playlist_id="pl_jazz", name="Jazz")
CFG = Config(client_id="x", playlists=(TECHNO, JAZZ))


@pytest.fixture()
def session(tmp_path):
    engine, factory = db.open_db(tmp_path / "crate.db")
    with factory() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def log(tmp_path):
    return WriteLog(tmp_path / "writes.log")


def client_for(sp):
    return api.Client(sp, sleep=lambda _s: None)


def sort_track(session, track_id, playlists, *, decided=datetime(2024, 6, 1), applied=None):
    session.add(
        Track(
            track_id=track_id,
            artist_ids=["a1"],
            title=f"Title {track_id}",
            artist_name="Artist",
            added_at=datetime(2024, 1, 1),
            genres=[],
            synced_at=datetime(2024, 6, 1),
        )
    )
    session.flush()
    session.add(Decision(track_id=track_id, status=Status.sorted, decided_at=decided))
    for i, playlist_id in enumerate(playlists):
        session.add(
            Assignment(
                track_id=track_id,
                playlist_id=playlist_id,
                decided_at=decided + timedelta(seconds=i),
                applied_at=applied,
            )
        )
    session.commit()


def applied_map(session):
    session.expire_all()
    return {
        (a.track_id, a.playlist_id): a.applied_at is not None
        for a in session.scalars(select(Assignment))
    }


# --- the plan ----------------------------------------------------------------


def test_dry_run_writes_nothing(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": []})

    plan = apply.build_plan(session, client_for(sp), CFG)
    lines = apply.render(plan)

    assert sp.add_calls == []
    assert applied_map(session) == {("t1", "pl_techno"): False}
    assert plan.to_add == 1
    assert "would add 1 tracks, 0 already in place" in lines
    assert "dry run - nothing was written; pass --commit to apply" in lines


def test_plan_splits_new_from_already_present(session):
    sort_track(session, "t1", ["pl_techno"])
    sort_track(session, "t2", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": ["t2"]})

    plan = apply.build_plan(session, client_for(sp), CFG)
    entry = plan.playlists[0]

    assert [t.track_id for t in entry.to_add] == ["t1"]
    assert [t.track_id for t in entry.already_there] == ["t2"]
    assert entry.name == "Techno"


def test_plan_lists_every_track(session):
    """No silent truncation - the plan is the thing being reviewed."""
    for i in range(30):
        sort_track(session, f"t{i}", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": []})

    lines = apply.render(apply.build_plan(session, client_for(sp), CFG))
    assert sum(1 for line in lines if line.startswith("  add ")) == 30


def test_applied_assignments_are_not_replanned(session):
    sort_track(session, "t1", ["pl_techno"], applied=datetime(2024, 6, 2))
    sp = FakePlaylists(contents={"pl_techno": ["t1"]})

    plan = apply.build_plan(session, client_for(sp), CFG)
    assert plan.playlists == []
    assert sp.read_calls == []  # the playlist is not even read
    assert apply.render(plan) == ["nothing to apply - every decision is already on Spotify"]


def test_playlist_name_falls_back_to_id(session):
    sort_track(session, "t1", ["pl_unknown"])
    sp = FakePlaylists(contents={"pl_unknown": []})
    plan = apply.build_plan(session, client_for(sp), CFG)
    assert plan.playlists[0].name == "pl_unknown"


def test_reading_a_long_playlist_paginates(session):
    sort_track(session, "t1", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": [f"x{i}" for i in range(250)]})

    apply.build_plan(session, client_for(sp), CFG)
    assert [offset for _pid, offset in sp.read_calls] == [0, 100, 200]


# --- committing --------------------------------------------------------------


def test_commit_adds_tracks_and_marks_them(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sort_track(session, "t2", ["pl_techno", "pl_jazz"])
    sp = FakePlaylists(contents={"pl_techno": [], "pl_jazz": []})
    client = client_for(sp)

    result = apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    assert sorted(sp.add_calls) == [("pl_jazz", ["t2"]), ("pl_techno", ["t1", "t2"])]
    assert result.added == 3
    assert all(applied_map(session).values())


def test_a_track_can_land_in_several_playlists(session, log):
    sort_track(session, "t1", ["pl_techno", "pl_jazz"])
    sp = FakePlaylists(contents={"pl_techno": [], "pl_jazz": []})
    client = client_for(sp)

    apply.execute(session, client, apply.build_plan(session, client, CFG), log)
    assert sp.contents["pl_techno"] == ["t1"]
    assert sp.contents["pl_jazz"] == ["t1"]


def test_adds_are_batched_by_a_hundred(session, log):
    for i in range(250):
        sort_track(session, f"t{i:03d}", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": []})
    client = client_for(sp)

    result = apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    assert [len(ids) for _pid, ids in sp.add_calls] == [100, 100, 50]
    assert result.calls == 3
    assert result.added == 250


def test_tracks_already_in_the_playlist_are_recorded_without_a_write(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": ["t1"]})
    client = client_for(sp)

    result = apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    assert sp.add_calls == []
    assert result.confirmed == 1
    assert applied_map(session) == {("t1", "pl_techno"): True}
    assert log.lines() == []


def test_running_twice_adds_nothing_the_second_time(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": []})
    client = client_for(sp)

    apply.execute(session, client, apply.build_plan(session, client, CFG), log)
    second = apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    assert len(sp.add_calls) == 1
    assert second.added == 0
    assert sp.contents["pl_techno"] == ["t1"]


def test_a_lost_applied_at_does_not_cause_duplicates(session, log):
    """Idempotence comes from the playlist, not from local bookkeeping."""
    sort_track(session, "t1", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": []})
    client = client_for(sp)
    apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    session.execute(Assignment.__table__.update().values(applied_at=None))
    session.commit()

    result = apply.execute(session, client, apply.build_plan(session, client, CFG), log)
    assert result.added == 0
    assert result.confirmed == 1
    assert sp.contents["pl_techno"] == ["t1"]


# --- failures ----------------------------------------------------------------


def test_unreadable_playlist_is_reported_and_others_continue(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sort_track(session, "t2", ["pl_jazz"])
    sp = FakePlaylists(contents={"pl_jazz": []})
    sp.read_errors["pl_techno"] = spotipy.SpotifyException(404, -1, "Not found")
    client = client_for(sp)

    plan = apply.build_plan(session, client, CFG)
    result = apply.execute(session, client, plan, log)

    assert result.failed == ["pl_techno"]
    assert sp.add_calls == [("pl_jazz", ["t2"])]
    assert applied_map(session)[("t1", "pl_techno")] is False
    assert any("error: cannot read playlist" in line for line in apply.render(plan))


def test_a_failing_add_leaves_the_rest_for_next_time(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sort_track(session, "t2", ["pl_jazz"])
    sp = FakePlaylists(contents={"pl_techno": [], "pl_jazz": []})
    sp.add_errors["pl_techno"] = spotipy.SpotifyException(403, -1, "Forbidden")
    client = client_for(sp)

    result = apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    assert result.failed == ["pl_techno"]
    assert result.added == 1  # pl_jazz still went through
    assert applied_map(session)[("t1", "pl_techno")] is False


def test_rate_limited_add_waits_and_succeeds(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": []})
    sp.rate_limit_adds = {"Retry-After": "2"}
    slept: list[float] = []
    client = api.Client(sp, sleep=slept.append)

    result = apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    assert slept == [3]
    assert result.added == 1


# --- the write log -----------------------------------------------------------


def test_every_write_is_logged_with_four_fields(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sort_track(session, "t2", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": []})
    client = client_for(sp)

    apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    lines = [line.split("\t") for line in log.lines()]
    assert len(lines) == 2
    for stamp, endpoint, track_id, playlist_id in lines:
        assert stamp.endswith("+00:00")
        assert endpoint == "POST /v1/playlists/pl_techno/tracks"
        assert playlist_id == "pl_techno"
    assert [line[2] for line in lines] == ["t1", "t2"]


def test_log_is_appended_not_replaced(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sort_track(session, "t2", ["pl_jazz"])
    sp = FakePlaylists(contents={"pl_techno": [], "pl_jazz": []})
    client = client_for(sp)
    apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    sort_track(session, "t3", ["pl_techno"])
    apply.execute(session, client, apply.build_plan(session, client, CFG), log)

    assert [line.split("\t")[2] for line in log.lines()] == ["t2", "t1", "t3"]


def test_log_file_is_0600(session, tmp_path):
    log = WriteLog(tmp_path / "writes.log")
    log.record("POST /v1/playlists/p/tracks", ["t1"], "p")
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600


def test_nothing_is_logged_on_a_dry_run(session, log):
    sort_track(session, "t1", ["pl_techno"])
    sp = FakePlaylists(contents={"pl_techno": []})
    apply.build_plan(session, client_for(sp), CFG)
    assert log.lines() == []
