import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# DATABASE_URL examples:
#   sqlite:///./data/autotrade.db                (dev, default)
#   postgresql+psycopg://user:pw@host:5432/db    (production)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/autotrade.db")

# Normalize "postgres://" / "postgresql://" -> the explicit psycopg-v3 driver
# so the install only needs psycopg[binary] and not psycopg2.
url = make_url(DATABASE_URL)
if url.drivername in ("postgres", "postgresql"):
    url = url.set(drivername="postgresql+psycopg")

_engine_kwargs: dict = {"echo": False, "future": True}
if url.drivername.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Sensible production defaults for Postgres. Override via env if needed.
    _engine_kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "10"))
    _engine_kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "5"))
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(url, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _lightweight_migrate():
    """Idempotent schema migration that ADDs missing columns for both SQLite
    (dev) and PostgreSQL (production).  We do NOT drop or alter existing
    columns — only safe ADD operations.

    SQLite  : Uses plain ALTER TABLE … ADD COLUMN (ignores errors per-column).
    Postgres: Uses ALTER TABLE … ADD COLUMN IF NOT EXISTS (native, safe).
    """
    is_sqlite = engine.url.drivername.startswith("sqlite")
    is_pg     = "postgresql" in engine.url.drivername or "postgres" in engine.url.drivername

    if not (is_sqlite or is_pg):
        return  # unknown driver — skip

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue

                col_type = col.type.compile(engine.dialect)

                # Build DEFAULT fragment
                default_sql = ""
                if col.default is not None and getattr(col.default, "arg", None) is not None:
                    d = col.default.arg
                    if isinstance(d, bool):
                        default_sql = f" DEFAULT {1 if d else 0}"
                    elif isinstance(d, (int, float)):
                        default_sql = f" DEFAULT {d}"
                    elif isinstance(d, str):
                        default_sql = f" DEFAULT '{d}'"

                try:
                    if is_pg:
                        # PostgreSQL: ADD COLUMN IF NOT EXISTS is idempotent
                        conn.execute(text(
                            f"ALTER TABLE {table.name} "
                            f"ADD COLUMN IF NOT EXISTS {col.name} {col_type}{default_sql}"
                        ))
                    else:
                        # SQLite: no IF NOT EXISTS for ADD COLUMN — rely on exception swallow
                        conn.execute(text(
                            f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}{default_sql}"
                        ))
                except Exception:
                    pass  # column already exists or DB error — safe to ignore


def init_db():
    """Idempotent bootstrap — safe to call on every startup."""
    if engine.url.drivername.startswith("sqlite"):
        os.makedirs("data", exist_ok=True)
    # 1. Create any missing tables (new tables from new models)
    Base.metadata.create_all(bind=engine)
    # 2. Add any missing columns to existing tables (ALTER TABLE ADD COLUMN)
    _lightweight_migrate()
    # 3. Ensure indexes exist
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            for ix in table.indexes:
                try:
                    ix.create(bind=conn, checkfirst=True)
                except Exception:
                    pass
    # 4. Run it a second time to catch race conditions on first deploy
    _lightweight_migrate()
