from datetime import datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, StatementError

from crate import db
from crate.models import Assignment, Base, Decision, Playlist, Status, Track


@pytest.fixture()
def session(tmp_path):
    engine, factory = db.open_db(tmp_path / "crate.db")
    with factory() as s:
        yield s
    engine.dispose()


@pytest.fixture()
def track_session(session):
    """A session with one track already committed, so that decision inserts
    fail on their own constraints rather than on the foreign key."""
    session.add(
        Track(
            track_id="t1",
            artist_ids=["a1"],
            title="Title",
            artist_name="Artist",
            added_at=datetime(2024, 1, 1),
            genres=None,
            synced_at=datetime(2024, 1, 2),
        )
    )
    session.commit()
    return session


def test_upgrade_is_idempotent(tmp_path):
    engine = db.make_engine(tmp_path / "crate.db")
    assert db.upgrade(engine) == (None, db.head_revision())
    assert db.upgrade(engine) == (db.head_revision(), db.head_revision())


def test_migrations_match_models(tmp_path):
    """Guard against models and migrations drifting apart."""
    engine, _ = db.open_db(tmp_path / "crate.db")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_status_values_constrained(track_session):
    track_session.add(Decision(track_id="t1", status="bogus"))
    with pytest.raises(StatementError):
        track_session.commit()


def test_a_track_can_go_into_several_playlists(track_session):
    track_session.add(Decision(track_id="t1", status=Status.sorted, decided_at=datetime(2024, 1, 3)))
    track_session.add_all([
        Assignment(track_id="t1", playlist_id="p1", decided_at=datetime(2024, 1, 3)),
        Assignment(track_id="t1", playlist_id="p2", decided_at=datetime(2024, 1, 3)),
    ])
    track_session.commit()
    assert {a.playlist_id for a in track_session.scalars(select(Assignment))} == {"p1", "p2"}


def test_the_same_playlist_cannot_be_assigned_twice(track_session):
    track_session.add_all([
        Assignment(track_id="t1", playlist_id="p1", decided_at=datetime(2024, 1, 3)),
        Assignment(track_id="t1", playlist_id="p1", decided_at=datetime(2024, 1, 4)),
    ])
    with pytest.raises(IntegrityError):
        track_session.commit()


def test_assignment_requires_known_track(session):
    session.add(Assignment(track_id="ghost", playlist_id="p1", decided_at=datetime(2024, 1, 3)))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.commit()


def test_deleting_a_track_removes_its_assignments(track_session):
    track_session.add(Assignment(track_id="t1", playlist_id="p1", decided_at=datetime(2024, 1, 3)))
    track_session.commit()
    track_session.execute(delete(Track).where(Track.track_id == "t1"))
    track_session.commit()
    assert track_session.scalars(select(Assignment)).all() == []


def test_decision_requires_known_track(session):
    session.add(Decision(track_id="ghost", status=Status.pending))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.commit()


def test_hotkey_is_unique(session):
    session.add_all([Playlist(playlist_id="p1", name="Rock", hotkey="1"),
                     Playlist(playlist_id="p2", name="Jazz", hotkey="1")])
    with pytest.raises(IntegrityError, match="hotkey"):
        session.commit()


def test_json_columns_round_trip(track_session):
    track = track_session.get(Track, "t1")
    track.genres = ["techno", "minimal techno"]
    track_session.commit()
    track_session.expire_all()
    assert track_session.get(Track, "t1").genres == ["techno", "minimal techno"]
    assert track_session.get(Track, "t1").artist_ids == ["a1"]


def test_null_genres_are_sql_null_not_json_null(session):
    """`IS NULL` must find a track whose genres were cleared through the ORM."""
    session.add(
        Track(
            track_id="t1",
            artist_ids=[],
            title="T",
            artist_name="A",
            added_at=datetime(2024, 1, 1),
            genres=None,
            synced_at=datetime(2024, 1, 2),
        )
    )
    session.commit()
    assert session.scalars(select(Track.track_id).where(Track.genres.is_(None))).all() == ["t1"]


def test_migration_round_trip_keeps_data(tmp_path):
    """Batch migrations rebuild tables; with foreign keys left on, dropping the
    original cascades and empties the referencing tables."""
    from alembic import command

    path = tmp_path / "crate.db"
    engine, factory = db.open_db(path)
    with factory() as s:
        s.add(
            Track(
                track_id="t1",
                artist_ids=["a1"],
                title="T",
                artist_name="A",
                added_at=datetime(2024, 1, 1),
                genres=["techno"],
                synced_at=datetime(2024, 1, 2),
            )
        )
        s.flush()
        s.add(Decision(track_id="t1", status=Status.sorted, decided_at=datetime(2024, 1, 3)))
        s.add(Assignment(track_id="t1", playlist_id="p1", decided_at=datetime(2024, 1, 3)))
        s.commit()

    cfg = db.alembic_config(engine)
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")

    with factory() as s:
        assert s.scalars(select(Track.track_id)).all() == ["t1"]
        assert s.scalars(select(Assignment.playlist_id)).all() == ["p1"]
        assert s.get(Decision, "t1").status is Status.sorted
    engine.dispose()
