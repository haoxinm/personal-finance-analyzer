"""Alembic environment.

The database URL is resolved at runtime from `personal_finance_analyzer.core.paths.db_path()` (which honors
`PFA_HOME`), never hardcoded in `alembic.ini` — this repository has no environment-
specific alembic.ini files to keep in sync.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from personal_finance_analyzer.core.paths import db_path
from personal_finance_analyzer.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    override = config.get_main_option("sqlalchemy.url")
    if override and not override.startswith("driver://"):
        return override
    return f"sqlite:///{db_path()}"


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
