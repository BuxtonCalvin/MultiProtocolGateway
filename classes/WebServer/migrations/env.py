"""
Alembic migrations environment.
Imports Base from models.py so autogenerate can detect schema changes.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import Engine, engine_from_config, pool

# Add WebServer package root to path so relative imports work
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from classes.WebServer.models import Base  # noqa: E402

_log: logging.Logger = logging.getLogger(__name__)

config = context.config


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url: str | None = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # required for SQLite column alterations
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable: Engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
