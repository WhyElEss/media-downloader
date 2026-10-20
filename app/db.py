import sqlite3
import threading

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    csrf TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    url TEXT NOT NULL,
    engine TEXT NOT NULL,
    platform TEXT NOT NULL,
    title TEXT,
    format TEXT NOT NULL,
    deliver INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    title TEXT,
    spec TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    downloaded INTEGER,
    total INTEGER,
    files TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    started REAL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS items_job ON items(job_id);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    DATA.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DATA / "mediadl.sqlite3", check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)


def query(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args=()) -> int:
    with _lock:
        return _conn.execute(sql, args).lastrowid


def transaction(fn):
    """Run fn(conn) inside one write transaction and return its result."""
    with _lock:
        _conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(_conn)
        except BaseException:
            _conn.execute("ROLLBACK")
            raise
        _conn.execute("COMMIT")
        return result
