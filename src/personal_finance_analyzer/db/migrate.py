"""Programmatic entry point for running Alembic migrations (used by the `migrate` CLI command
and by tests).

Builds an Alembic `Config` pointing at the in-package `migrations/` directory, so migrations work
whether run from a source checkout or an installed wheel — neither depends on the repository's
`alembic.ini` being present on disk (that file exists for developers running the `alembic` CLI
directly).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def build_alembic_config(db_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    if db_url is not None:
        config.set_main_option("sqlalchemy.url", db_url)
    return config


def upgrade_head(db_url: str | None = None) -> None:
    """Run all migrations up to ``head``. ``db_url`` overrides the default (the data directory's
    `finance.db`, honoring `PFA_HOME`)."""
    config = build_alembic_config(db_url)
    command.upgrade(config, "head")
