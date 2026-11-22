"""SQLite engine (WAL, foreign keys, busy timeout) and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _on_connect(dbapi_conn, _record) -> None:  # noqa: ANN001
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
        pool_size=8,
        max_overflow=8,
        pool_pre_ping=False,
    )
    event.listen(eng, "connect", _on_connect)

    # SQLAlchemy controls transactions (pysqlite's implicit handling is buggy). Every transaction
    # starts with BEGIN IMMEDIATE: the write lock is taken up-front and waiting for it honours
    # busy_timeout. Deferred transactions that read and then write can fail instantly with
    # SQLITE_BUSY (lock upgrade) under concurrency – unacceptable for the job queue.
    # Rule: never hold a transaction across a long await (commit first).
    @event.listens_for(eng, "connect")
    def _no_pysqlite_tx(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None

    @event.listens_for(eng, "begin")
    def _begin(conn):  # noqa: ANN001
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return eng


def init_engine(db_path: Path) -> Engine:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = make_engine(db_path)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("database not initialised")
    return _engine


def dispose_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    if _SessionLocal is None:
        raise RuntimeError("database not initialised")
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


async def run_db(fn, *args, **kwargs):  # noqa: ANN001, ANN201
    """Run blocking DB work off the event loop.

    Rule: async code never opens a transaction on the loop thread. BEGIN IMMEDIATE may wait
    for the write lock, and the current holder might need the loop to commit (deadlock).
    """
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


def check_writable() -> None:
    with get_engine().begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS _health (id INTEGER PRIMARY KEY, ts TEXT)"))
        conn.execute(text("INSERT OR REPLACE INTO _health (id, ts) VALUES (1, :ts)"), {"ts": utcnow().isoformat()})
