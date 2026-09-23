from datetime import datetime, timedelta

import pytest
import spotipy
from sqlalchemy import select

from crate import api, db, sync
from crate.models import Artist, Assignment, Decision, Status, Track
from tests.fakes import FakeSpotify, local_item, track_item


@pytest.fixture()
def session(tmp_path):
    engine, factory = db.open_db(tmp_path / "crate.db")
    with factory() as s:
        yield s
    engine.dispose()


def client_for(sp, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    return api.Client(sp, **kwargs)


def library(count: int, *, artists=(("a1", "Artist One"),)):
    """Newest first, as Spotify serves them."""
    newest = datetime(2024, 6, 1)
    return [
        track_item(
            f"t{i}",
            (newest - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            artists=artists,
        )
        for i in range(count)
    ]


def test_first_sync_stores_tracks_and_pending_decisions(session):
    sp = FakeSpotify(library(3), artists={"a1": ["techno", "minimal techno"]})
    result = sync.sync(session, client_for(sp))

    assert result.new_tracks == 3
    assert result.stopped_early is False
    tracks = session.scalars(select(Track).order_by(Track.track_id)).all()
    assert [t.track_id for t in tracks] == ["t0", "t1", "t2"]
    assert tracks[0].artist_name == "Artist One"
    assert tracks[0].added_at == datetime(2024, 6, 1)
    assert all(d.status is Status.pending for d in session.scalars(select(Decision)))


def test_paginates_by_fifty(session):
    sp = FakeSpotify(library(120), artists={"a1": []})
    result = sync.sync(session, client_for(sp))
    assert [offset for _limit, offset in sp.page_calls] == [0, 50, 100]
    assert {limit for limit, _ in sp.page_calls} == {50}
    assert result.new_tracks == 120


def test_incremental_stops_at_first_known_track(session):
    items = library(60)
    sync.sync(session, client_for(FakeSpotify(items, artists={"a1": []})))

    # two new likes land on top of the library
    newer = [track_item("new1", "2024-03-02T00:00:00Z"), track_item("new2", "2024-03-01T00:00:00Z")]
    sp = FakeSpotify(newer + items, artists={"a1": []})
    result = sync.sync(session, client_for(sp))

    assert result.new_tracks == 2
    assert result.stopped_early is True
    assert sp.page_calls == [(50, 0)]  # one page, no walking the whole library
    assert session.scalar(select(Track).where(Track.track_id == "new1")) is not None


def test_full_walks_the_whole_library(session):
    items = library(60)
    sync.sync(session, client_for(FakeSpotify(items, artists={"a1": []})))
    sp = FakeSpotify(items, artists={"a1": []})
    result = sync.sync(session, client_for(sp), full=True)

    assert result.new_tracks == 0
    assert result.stopped_early is False
    assert [offset for _l, offset in sp.page_calls] == [0, 50]


def test_resync_does_not_reset_decisions(session):
    items = library(2)
    sync.sync(session, client_for(FakeSpotify(items, artists={"a1": []})))
    session.get(Decision, "t0").status = Status.skipped
    decision = session.get(Decision, "t1")
    decision.status = Status.sorted
    decision.decided_at = datetime(2024, 6, 1)
    session.add(Assignment(track_id="t1", playlist_id="pl_techno", decided_at=datetime(2024, 6, 1)))
    session.commit()

    sync.sync(session, client_for(FakeSpotify(items, artists={"a1": []})), full=True)

    session.expire_all()
    assert session.get(Decision, "t0").status is Status.skipped
    assert session.get(Decision, "t1").status is Status.sorted
    assert session.get(Assignment, ("t1", "pl_techno")) is not None


def test_local_and_unavailable_tracks_are_skipped(session):
    sp = FakeSpotify([track_item("t0", "2024-01-02T00:00:00Z"), local_item()], artists={"a1": []})
    result = sync.sync(session, client_for(sp))
    assert result.new_tracks == 1
    assert result.skipped_local == 1
    assert session.scalars(select(Track.track_id)).all() == ["t0"]


def test_artists_are_fetched_in_batches_of_fifty(session):
    many = [
        track_item(f"t{i}", f"2024-01-01T00:00:{i:02d}Z", artists=((f"a{i}", f"Artist {i}"),))
        for i in range(60)
    ]
    sp = FakeSpotify(many, artists={f"a{i}": [f"genre{i}"] for i in range(60)})
    result = sync.sync(session, client_for(sp))

    assert [len(batch) for batch in sp.artist_calls] == [50, 10]
    assert result.artists_fetched == 60
    assert session.get(Track, "t7").genres == ["genre7"]


def test_genres_are_merged_across_a_tracks_artists(session):
    items = [track_item("t0", "2024-01-01T00:00:00Z", artists=(("a1", "One"), ("a2", "Two")))]
    sp = FakeSpotify(items, artists={"a1": ["techno", "acid"], "a2": ["acid", "ebm"]})
    sync.sync(session, client_for(sp))
    assert session.get(Track, "t0").genres == ["acid", "ebm", "techno"]
    assert session.get(Track, "t0").artist_name == "One, Two"


def test_artists_without_genres_store_an_empty_list(session):
    """Empty means answered; the track must not be re-examined forever."""
    sp = FakeSpotify(library(1), artists={"a1": []})
    sync.sync(session, client_for(sp))
    assert session.get(Track, "t0").genres == []
    assert session.get(Artist, "a1").genres == []

    sp2 = FakeSpotify(library(1), artists={"a1": []})
    sync.sync(session, client_for(sp2), full=True)
    assert sp2.artist_calls == []


def test_unfetchable_artist_leaves_genres_null_for_a_later_run(session):
    sp = FakeSpotify(library(1), artists={})  # /artists returns null for a1
    result = sync.sync(session, client_for(sp))
    assert result.genres_filled == 0
    assert session.get(Track, "t0").genres is None


def test_only_missing_artists_are_refetched(session):
    sp = FakeSpotify(library(2), artists={"a1": ["techno"]})
    sync.sync(session, client_for(sp))
    assert sp.artist_calls == [["a1"]]

    sp2 = FakeSpotify(
        [track_item("t9", "2024-03-01T00:00:00Z", artists=(("a2", "Two"),))] + library(2),
        artists={"a1": ["techno"], "a2": ["jazz"]},
    )
    sync.sync(session, client_for(sp2))
    assert sp2.artist_calls == [["a2"]]  # a1 is already known


def test_interrupted_sync_keeps_completed_pages(session):
    class Failing(FakeSpotify):
        def current_user_saved_tracks(self, limit=20, offset=0):
            if offset == 50:
                raise RuntimeError("connection dropped")
            return super().current_user_saved_tracks(limit=limit, offset=offset)

    sp = Failing(library(120), artists={"a1": []})
    with pytest.raises(RuntimeError):
        sync.sync(session, client_for(sp))
    assert len(session.scalars(select(Track.track_id)).all()) == 50


def test_rate_limit_waits_for_retry_after_then_continues(session):
    slept: list[float] = []
    sp = FakeSpotify(library(2), artists={"a1": []})
    sp.rate_limit_once = {"Retry-After": "3"}
    waits: list[int] = []

    result = sync.sync(
        session, api.Client(sp, sleep=slept.append, on_wait=waits.append)
    )

    assert slept == [4]  # Retry-After plus a second of slack
    assert waits == [3]
    assert result.new_tracks == 2


def test_rate_limit_with_an_absurd_retry_after_gives_up(session):
    sp = FakeSpotify(library(2), artists={"a1": []})
    sp.rate_limit_once = {"Retry-After": "86400"}
    with pytest.raises(api.RateLimited) as exc:
        sync.sync(session, api.Client(sp, sleep=lambda _s: None))
    assert exc.value.retry_after == 86400


def test_non_rate_limit_errors_are_not_retried(session):
    class Broken(FakeSpotify):
        def current_user_saved_tracks(self, limit=20, offset=0):
            self.page_calls.append((limit, offset))
            raise spotipy.SpotifyException(403, -1, "forbidden")

    sp = Broken(library(1))
    with pytest.raises(spotipy.SpotifyException):
        sync.sync(session, client_for(sp))
    assert len(sp.page_calls) == 1


def test_release_date_and_popularity_are_stored(session):
    sp = FakeSpotify(
        [track_item("t0", "2024-06-01T00:00:00Z", release_date="1998-05-21", popularity=73)],
        artists={"a1": []},
    )
    sync.sync(session, client_for(sp))
    track = session.get(Track, "t0")
    assert (track.release_date, track.popularity, track.release_year) == ("1998-05-21", 73, 1998)


def test_partial_release_dates_still_yield_a_year(session):
    sp = FakeSpotify(
        [track_item("t0", "2024-06-01T00:00:00Z", release_date="1975")], artists={"a1": []}
    )
    sync.sync(session, client_for(sp))
    assert session.get(Track, "t0").release_year == 1975


def test_missing_release_date_is_tolerated(session):
    sp = FakeSpotify(
        [track_item("t0", "2024-06-01T00:00:00Z", release_date=None, popularity=None)],
        artists={"a1": []},
    )
    sync.sync(session, client_for(sp))
    track = session.get(Track, "t0")
    assert (track.release_date, track.popularity, track.release_year) == (None, None, None)


def test_full_refreshes_metadata_on_known_tracks(session):
    """The only way to backfill fields added after a track was first synced."""
    sp = FakeSpotify(
        [track_item("t0", "2024-06-01T00:00:00Z", release_date="1998", popularity=10)],
        artists={"a1": []},
    )
    sync.sync(session, client_for(sp))

    sp2 = FakeSpotify(
        [track_item("t0", "2024-06-01T00:00:00Z", release_date="1999-02-01", popularity=88)],
        artists={"a1": []},
    )
    result = sync.sync(session, client_for(sp2), full=True)

    session.expire_all()
    track = session.get(Track, "t0")
    assert (track.release_date, track.popularity) == ("1999-02-01", 88)
    assert (result.new_tracks, result.refreshed) == (0, 1)


def test_refreshing_does_not_touch_genres_or_decisions(session):
    sp = FakeSpotify(library(1), artists={"a1": ["techno"]})
    sync.sync(session, client_for(sp))
    session.get(Decision, "t0").status = Status.skipped
    session.commit()

    sync.sync(session, client_for(FakeSpotify(library(1), artists={"a1": ["techno"]})), full=True)

    session.expire_all()
    assert session.get(Track, "t0").genres == ["techno"]
    assert session.get(Decision, "t0").status is Status.skipped
