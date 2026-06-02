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
    # Pool sizing has to match the upstream Postgres server's limits.
    # User reported: FATAL EMAXCONNSESSION max clients=15 in the bot
    # log — their managed Postgres tier (Supabase free etc.) caps
    # session-mode pooler at 15 simultaneous clients. We had been
    # configuring 30+20=50 which made our SQLAlchemy side think it had
    # capacity, but Postgres itself was rejecting the 16th connection.
    #
    # Safe default: 8+4=12 (leaves headroom under a 15-client cap).
    # Users on bigger DBs (Railway Postgres, RDS, etc.) should set
    # DB_POOL_SIZE=30 + DB_MAX_OVERFLOW=20 via env to get the higher
    # capacity. Document this in deploy notes.
    _engine_kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "8"))
    _engine_kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "4"))
    _engine_kwargs["pool_pre_ping"] = True
    # Recycle connections every 30min so stale ones from a Postgres
    # idle-timeout drop don't block forever waiting for a half-dead conn.
    _engine_kwargs["pool_recycle"] = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    # Timeout waiting for a free connection — fail fast (8s) instead
    # of hanging 30s and surfacing as "engine error" in the UI.
    _engine_kwargs["pool_timeout"] = int(os.getenv("DB_POOL_TIMEOUT", "8"))

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


def _explicit_v2_migration():
    """Directly add every new column from the v2 schema (futures + copy trading).
    Uses explicit PostgreSQL-safe SQL. Safe to run multiple times (IF NOT EXISTS).
    This runs BEFORE the ORM-based _lightweight_migrate() as a safety net."""
    _NEW_COLS = [
        # strategies table
        ("strategies", "allow_copy_trading", "BOOLEAN",  "DEFAULT false"),
        ("strategies", "default_leverage",   "INTEGER",  "DEFAULT 1"),
        # trades table
        ("trades", "market_type",       "TEXT",    "DEFAULT 'spot'"),
        ("trades", "leverage",          "INTEGER", "DEFAULT 1"),
        ("trades", "liquidation_price", "FLOAT",   ""),
        ("trades", "copy_source_id",    "INTEGER", ""),
    ]
    is_pg  = "postgresql" in engine.url.drivername or "postgres" in engine.url.drivername
    is_sq  = engine.url.drivername.startswith("sqlite")
    if not (is_pg or is_sq):
        return
    with engine.begin() as conn:
        for (table, col, dtype, dflt) in _NEW_COLS:
            try:
                if is_pg:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {dtype} {dflt}"
                    ))
                else:  # sqlite — no IF NOT EXISTS for ADD COLUMN
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {dtype} {dflt}"
                    ))
            except Exception:
                pass  # column already exists — safe to ignore


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
                        # PostgreSQL needs TRUE/FALSE keywords, not 0/1 for boolean columns
                        default_sql = f" DEFAULT {'TRUE' if d else 'FALSE'}"
                    elif isinstance(d, (int, float)):
                        default_sql = f" DEFAULT {d}"
                    elif isinstance(d, str):
                        default_sql = f" DEFAULT '{d}'"
                elif col.server_default is not None:
                    sd = getattr(col.server_default, 'arg', None)
                    if sd is not None:
                        default_sql = f" DEFAULT {sd}"

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
    # 2. ORM-based generic migration for any missing columns
    # Note: _explicit_v2_migration() removed — Alembic migration handles this now
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
