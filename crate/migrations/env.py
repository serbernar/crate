from __future__ import annotations

from alembic import context
from sqlalchemy import Engine

from crate.models import Base

target_metadata = Base.metadata


def _run(connection) -> None:
    # Batch mode rebuilds a table by copying rows into a replacement and
    # dropping the original. With foreign keys on, that DROP performs an
    # implicit DELETE, and any ON DELETE CASCADE wipes the referencing tables -
    # dropping a column from `tracks` would empty `assignments`. The pragma is
    # a no-op inside a transaction, so it goes to the driver before one opens.
    sqlite = connection.dialect.name == "sqlite"
    if sqlite:
        connection.connection.dbapi_connection.execute("PRAGMA foreign_keys=OFF")

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()

    if sqlite:
        connection.connection.dbapi_connection.execute("PRAGMA foreign_keys=ON")


def run_migrations_offline() -> None:
    context.configure(
        url=context.config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # db.alembic_config() passes a live Engine; the alembic CLI passes nothing
    # and we fall back to the URL in alembic.ini.
    connectable = context.config.attributes.get("connection")
    if connectable is None:
        from sqlalchemy import engine_from_config, pool

        connectable = engine_from_config(
            context.config.get_section(context.config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    if isinstance(connectable, Engine):
        with connectable.connect() as connection:
            _run(connection)
    else:
        _run(connectable)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
