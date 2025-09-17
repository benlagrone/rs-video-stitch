"""Database helpers for the render API."""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

DB_URL = os.getenv("DB_URL", "sqlite:////videos/db.sqlite3")
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
