"""Engine, session factory and the Alembic entry point.

Migrations run through Alembic's Python API rather than its CLI so that
``crate migrate`` works from any directory and needs no alembic.ini next to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from . import paths

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> datetime:
    """Naive UTC. SQLite has no timezone type; everything stored is UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def url_for(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def make_engine(path: Path | None = None) -> Engine:
    target = path or paths.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url_for(target))

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA busy_timeout = 5000")
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = engine
    return cfg


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision() -> str | None:
    return ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()


def upgrade(engine: Engine, revision: str = "head") -> tuple[str | None, str | None]:
    """Run migrations. Returns (revision before, revision after)."""
    before = current_revision(engine)
    command.upgrade(alembic_config(engine), revision)
    return before, current_revision(engine)


def open_db(path: Path | None = None) -> tuple[Engine, sessionmaker[Session]]:
    """Connect and migrate. Every command starts here."""
    engine = make_engine(path)
    upgrade(engine)
    return engine, make_session_factory(engine)
