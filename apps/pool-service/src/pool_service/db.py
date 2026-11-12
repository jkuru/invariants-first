"""SQLite connections and schema setup."""
from __future__ import annotations

import pathlib
import sqlite3

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")
STORE_REFUSAL = "R-20:"       # the message the schema's triggers raise (the store's own check)


def connect(path: str | pathlib.Path) -> sqlite3.Connection:
    """A connection in autocommit mode; explicit transactions use BEGIN IMMEDIATE."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def apply_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Create the tables and the R-20 triggers on this connection (idempotent)."""
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def memory() -> sqlite3.Connection:
    """A fresh in-memory database with the schema (tests, benchmarks)."""
    return apply_schema(connect(":memory:"))


def init(path: str | pathlib.Path) -> None:
    """Create the database file in WAL mode with the schema."""
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        apply_schema(conn)
    finally:
        conn.close()



