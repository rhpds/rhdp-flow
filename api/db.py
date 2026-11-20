"""SQLAlchemy engine / session for durable Flow workspace state.

``RHDP_DATABASE_URL`` defaults to a local SQLite file so unit tests and
``dev.sh`` work without Postgres. Production OpenShift sets a Postgres URL.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger("rhdp_flow.api")

DEFAULT_SQLITE = f"sqlite:///{Path.home() / '.rhdp-flow' / 'flow.db'}"


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def database_url() -> str:
    return (os.environ.get("RHDP_DATABASE_URL") or DEFAULT_SQLITE).strip()


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    url = database_url()
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # Ensure parent dir exists for file-backed sqlite
        if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
            path = url.replace("sqlite:///", "", 1)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _sqlite_pragma(dbapi_conn, _connection_record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Session:
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create tables and run lightweight migrations."""
    # Import models so metadata is populated
    try:
        from api import dependency_models  # noqa: F401
    except ImportError:
        pass  # FLOW keeps dependency tables in lib/dependency_store
    from api import workspace_models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    # Lightweight column adds for sqlite/postgres without full Alembic in v1
    with engine.begin() as conn:
        if database_url().startswith("sqlite"):
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(workspace_state)")).fetchall()
            }
            if "asset_passwords_json" not in cols and cols:
                conn.execute(text("ALTER TABLE workspace_state ADD COLUMN asset_passwords_json TEXT"))
        # Postgres: create_all handles new installs; ALTER IF NOT EXISTS via try
        elif "workspace_state" in Base.metadata.tables:
            try:
                conn.execute(
                    text(
                        "ALTER TABLE workspace_state "
                        "ADD COLUMN IF NOT EXISTS asset_passwords_json TEXT"
                    )
                )
            except Exception:  # noqa: BLE001
                pass
    logger.info("Flow DB initialized (%s)", database_url().split("@")[-1] if "@" in database_url() else database_url())


def reset_db_for_tests() -> None:
    """Drop and recreate all tables — tests only."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    try:
        from api import dependency_models  # noqa: F401
    except ImportError:
        pass
    from api import workspace_models  # noqa: F401

    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def healthcheck() -> bool:
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("DB healthcheck failed")
        return False
