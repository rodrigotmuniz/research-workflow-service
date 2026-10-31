"""Alembic environment: uses the app's settings and metadata."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from rws.config import get_settings
from rws.db import Base, make_engine
from rws.models import Flow, Run  # noqa: F401 - register tables on the metadata

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)

target_metadata = Base.metadata


def run_migrations() -> None:
    url = context.config.attributes.get("database_url") or get_settings().database_url
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
