"""Alembic environment: uses the app's settings and metadata."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context

from rws.config import Settings
from rws.db import Base, make_engine
from rws.models import Flow, Run  # noqa: F401 - register tables on the metadata

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)

target_metadata = Base.metadata


def run_migrations() -> None:
    # Only the database URL is needed, so migrations run without the API's auth settings.
    url = (
        context.config.attributes.get("database_url")
        or os.environ.get("RWS_DATABASE_URL")
        or Settings.model_fields["database_url"].default
    )
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
