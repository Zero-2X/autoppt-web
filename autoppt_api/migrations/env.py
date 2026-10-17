"""Alembic environment: migrations for the AutoPPT API database."""

from __future__ import annotations

from alembic import context

from autoppt_api import models  # noqa: F401  (registers the tables)
from autoppt_api.config import Settings
from autoppt_api.db import Base, make_engine

target_metadata = Base.metadata
url = Settings().database_url


def run_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=url.startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = make_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=url.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
