"""
Alembic environment. Picks DATABASE_URL from the .env file (or actual env)
so migrations target the same database the app uses. Production deploys
should run `alembic upgrade head` once at startup, before uvicorn.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine.url import make_url

load_dotenv()

# Import models so target_metadata sees every table.
from backend.models.database import Base  # noqa: E402
from backend.models import config as _config_model  # noqa: E402,F401
from backend.models import strategy as _strategy_model  # noqa: E402,F401
from backend.models import trade as _trade_model  # noqa: E402,F401
from backend.models import audit as _audit_model  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./data/autotrade.db")
url = make_url(DB_URL)
if url.drivername in ("postgres", "postgresql"):
    url = url.set(drivername="postgresql+psycopg")
# hide_password=False is required — SQLAlchemy 2.0 str(url) masks the password
config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))


def run_migrations_offline() -> None:
    context.configure(
        url=str(url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Enable batch mode on sqlite so ALTER TABLE works.
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
