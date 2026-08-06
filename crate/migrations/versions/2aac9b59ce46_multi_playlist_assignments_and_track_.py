"""multi playlist assignments and track metadata

A track can belong to several playlists at once, so the single playlist_id on
decisions becomes a row per (track, playlist) in assignments. decisions keeps
only where a track stands: pending, sorted or skipped.

Also adds release_date and popularity to tracks, both shown during triage.

Revision ID: 2aac9b59ce46
Revises: e6e6895d6ff4
Create Date: 2026-08-06 10:22:52.443710
"""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '2aac9b59ce46'
down_revision: str | None = 'e6e6895d6ff4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('assignments',
    sa.Column('track_id', sa.Text(), nullable=False),
    sa.Column('playlist_id', sa.Text(), nullable=False),
    sa.Column('decided_at', sa.DateTime(), nullable=False),
    sa.Column('applied_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['track_id'], ['tracks.track_id'], name=op.f('fk_assignments_track_id_tracks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('track_id', 'playlist_id', name=op.f('pk_assignments'))
    )
    with op.batch_alter_table('assignments', schema=None) as batch_op:
        batch_op.create_index('ix_assignments_playlist_id', ['playlist_id'], unique=False)

    # Carry existing decisions over before the column holding them is dropped.
    op.execute(
        """
        INSERT INTO assignments (track_id, playlist_id, decided_at, applied_at)
        SELECT track_id, playlist_id, COALESCE(decided_at, CURRENT_TIMESTAMP), NULL
        FROM decisions
        WHERE status = 'sorted' AND playlist_id IS NOT NULL
        """
    )

    with op.batch_alter_table('decisions', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_decisions_sorted_has_playlist'), type_='check')
        batch_op.drop_column('playlist_id')

    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('release_date', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('popularity', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tracks', schema=None) as batch_op:
        batch_op.drop_column('popularity')
        batch_op.drop_column('release_date')

    with op.batch_alter_table('decisions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('playlist_id', sa.TEXT(), nullable=True))

    # Lossy on purpose: the old schema holds one playlist per track, so a track
    # assigned to several keeps the first by id and the rest are dropped.
    op.execute(
        """
        UPDATE decisions
        SET playlist_id = (
            SELECT MIN(playlist_id) FROM assignments WHERE assignments.track_id = decisions.track_id
        )
        WHERE status = 'sorted'
        """
    )
    # The restored CHECK forbids a sorted track without a playlist; a row in
    # that state cannot be expressed and goes back to pending.
    op.execute("UPDATE decisions SET status = 'pending', decided_at = NULL "
               "WHERE status = 'sorted' AND playlist_id IS NULL")

    with op.batch_alter_table('decisions', schema=None) as batch_op:
        batch_op.create_check_constraint(batch_op.f('ck_decisions_sorted_has_playlist'), "(status = 'sorted') = (playlist_id IS NOT NULL)")

    with op.batch_alter_table('assignments', schema=None) as batch_op:
        batch_op.drop_index('ix_assignments_playlist_id')

    op.drop_table('assignments')
