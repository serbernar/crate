import sqlite3

import pytest

from spotify_triage import db


@pytest.fixture()
def conn(tmp_path):
    c = db.open_db(tmp_path / "triage.db")
    yield c
    c.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "triage.db"
    c = db.connect(path)
    assert db.migrate(c) == (0, db.SCHEMA_VERSION)
    assert db.migrate(c) == (db.SCHEMA_VERSION, db.SCHEMA_VERSION)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tracks", "decisions", "playlists", "artists"} <= tables


def test_decision_status_constrained(conn):
    conn.execute(
        "INSERT INTO tracks VALUES ('t1', '[\"a1\"]', 'Title', 'Artist', '2024-01-01T00:00:00Z', NULL, '2024-01-02T00:00:00Z')"
    )
    conn.execute("INSERT INTO decisions VALUES ('t1', 'pending', NULL, NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE decisions SET status = 'bogus' WHERE track_id = 't1'")


def test_sorted_requires_playlist_and_pending_forbids_it(conn):
    conn.execute(
        "INSERT INTO tracks VALUES ('t1', '[]', 'Title', 'Artist', '2024-01-01T00:00:00Z', NULL, '2024-01-02T00:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO decisions VALUES ('t1', 'sorted', NULL, '2024-01-03T00:00:00Z')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO decisions VALUES ('t1', 'pending', 'p1', NULL)")
    conn.execute("INSERT INTO decisions VALUES ('t1', 'sorted', 'p1', '2024-01-03T00:00:00Z')")


def test_decision_requires_known_track(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO decisions VALUES ('ghost', 'pending', NULL, NULL)")


def test_upsert_playlists_reassigns_hotkeys(conn):
    db.upsert_playlists(conn, [("p1", "Rock", "1"), ("p2", "Jazz", "2")])
    db.upsert_playlists(conn, [("p2", "Jazz Renamed", "1"), ("p3", "Ambient", "2")])
    rows = {r["playlist_id"]: (r["name"], r["hotkey"]) for r in conn.execute("SELECT * FROM playlists")}
    assert rows["p1"] == ("Rock", None)
    assert rows["p2"] == ("Jazz Renamed", "1")
    assert rows["p3"] == ("Ambient", "2")
