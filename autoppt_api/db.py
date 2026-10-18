"""Database engine and sessions (SQLAlchemy 2.0; SQLite for development, PostgreSQL in production)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        if ":///" in url and not url.endswith(":memory:"):
            from pathlib import Path

            Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        path = url.split(":///", 1)[1] if ":///" in url else ":memory:"

        def connect() -> sqlite3.Connection:
            # A one-hour soak (2026-10-18) failed once with "unable to open database file" while opening a
            # connection; a short retry rides out a momentary lock on the file (e.g. a virus scanner).
            for attempt in range(5):
                try:
                    return sqlite3.connect(path, check_same_thread=False, timeout=30)
                except sqlite3.OperationalError as exc:
                    if "unable to open" not in str(exc) or attempt == 4:
                        raise
                    time.sleep(0.2 * (attempt + 1))
            raise AssertionError("unreachable")

        # As many pooled connections as request threads: overflow connections were opened and closed
        # thousands of times an hour under load.
        engine = (create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}) if path == ":memory:"
                  else create_engine(url, creator=connect, poolclass=QueuePool, pool_size=40, max_overflow=10))

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(connection, _record):  # pragma: no cover - driver hook
            connection.isolation_level = None  # let SQLAlchemy's begin event issue BEGIN itself
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(connection):  # pragma: no cover - driver hook
            # Take the write lock up front: two requests can never both read a balance
            # and then both freeze it (PostgreSQL uses SELECT ... FOR UPDATE instead).
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
