"""Database helpers for the render API."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.storage import resolve_storage_root

# Mirror the storage layout helpers so local development defaults to the same
# directory structure Docker uses while still working on developer laptops.
_storage_root = resolve_storage_root()
_default_db_url = "sqlite:///" + (_storage_root / "db.sqlite3").as_posix()
DB_URL = os.getenv("DB_URL", _default_db_url)


def _ensure_sqlite_directory(url: str) -> None:
    try:
        parsed = make_url(url)
    except Exception:
        return

    if parsed.drivername != "sqlite" or not parsed.database or parsed.database == ":memory:":
        return

    db_path = Path(parsed.database).expanduser()
    if not db_path.is_absolute():
        db_path = db_path.resolve()

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Unable to create SQLite directory '{db_path.parent}' for DB_URL '{url}': {exc}"
        ) from exc


_ensure_sqlite_directory(DB_URL)
engine = create_engine(DB_URL, future=True)
SessionLocal = sessionmaker(bind=engine, future=True, expire_on_commit=False)
Base = declarative_base()


def init_db() -> None:
    """Create database tables if they do not exist."""
    # Import models lazily so SQLAlchemy metadata is populated.
    from app import models  # noqa: F401

    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Session:
    """Provide a transactional scope around a series of operations."""
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
