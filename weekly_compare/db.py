"""SQLite access helpers.

Design notes
------------
* The database runs in WAL mode so readers keep seeing the last committed
  snapshot while an import transaction is open.
* Imports use ``BEGIN IMMEDIATE`` (one writer at a time) and only swap the
  ``current_reports`` pointer after the new immutable report version, its
  hourly summary and its raw receipt lines have all been written.  A failure
  therefore rolls back everything and leaves the previous report in place.
* Comparison reads happen inside a single read transaction and are computed by
  one SQL statement.  Every store row and every total in a response therefore
  comes from one committed snapshot.

Legacy databases
----------------
Store identity is based on ``stores.code_key`` (the Python Unicode
``casefold`` of the submitted code, computed in application code).  Older
databases only have ``stores.code``; :func:`migrate_schema` adds the column in
place with ``ALTER TABLE`` and backfills it, so an existing database file keeps
working without an export/import step.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS stores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    code_key    TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_versions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id              INTEGER NOT NULL REFERENCES stores(id),
    business_date         TEXT NOT NULL,
    revision              INTEGER NOT NULL CHECK (revision >= 1),
    complete_hour_count   INTEGER NOT NULL CHECK (complete_hour_count >= 0),
    total_sales_cents     INTEGER NOT NULL,
    receipt_line_count    INTEGER NOT NULL DEFAULT 0 CHECK (receipt_line_count >= 0),
    created_at            TEXT NOT NULL,
    UNIQUE (store_id, business_date, revision)
);

CREATE TABLE IF NOT EXISTS report_hours (
    report_version_id  INTEGER NOT NULL REFERENCES report_versions(id) ON DELETE CASCADE,
    hour                INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    sales_cents         INTEGER NOT NULL,
    PRIMARY KEY (report_version_id, hour)
);

CREATE TABLE IF NOT EXISTS receipt_lines (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    report_version_id  INTEGER NOT NULL REFERENCES report_versions(id) ON DELETE CASCADE,
    hour               INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    amount_cents       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS current_reports (
    store_id           INTEGER NOT NULL REFERENCES stores(id),
    business_date      TEXT NOT NULL,
    report_version_id  INTEGER NOT NULL REFERENCES report_versions(id),
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (store_id, business_date)
);

CREATE INDEX IF NOT EXISTS idx_report_versions_store_date
    ON report_versions (store_id, business_date);
CREATE INDEX IF NOT EXISTS idx_receipt_lines_version_hour
    ON receipt_lines (report_version_id, hour);
CREATE INDEX IF NOT EXISTS idx_current_reports_date
    ON current_reports (business_date);
"""


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema without losing data.

    Every step is idempotent: fresh databases already match and the migration
    becomes a no-op.  The ``code_key`` backfill deliberately leaves duplicates
    in place — a legacy database that stored the same physical store under
    several spellings keeps every original row; the conflict is handled by the
    service layer (frozen store) rather than by collapsing data silently.

    Several gunicorn workers can run this at the same instant: two of them may
    pass the ``PRAGMA`` check before either has added the column, in which case
    SQLite raises "duplicate column name" instead of a lock error.  That is the
    expected race and is treated as success by re-reading the column list.
    """

    if not _has_column(conn, "stores", "code_key"):
        try:
            conn.execute(
                "ALTER TABLE stores ADD COLUMN code_key TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            if not _has_column(conn, "stores", "code_key"):  # pragma: no cover
                raise
    # Backfill with Python's Unicode casefold (SQLite has no equivalent).
    # Rows whose key is missing/blank get recomputed; existing values are kept
    # so the migration is idempotent and never rewrites already-correct data.
    rows = conn.execute("SELECT id, code FROM stores").fetchall()
    conn.executemany(
        "UPDATE stores SET code_key = ? WHERE id = ? AND (code_key = '' OR code_key IS NULL)",
        [(row["code"].casefold(), row["id"]) for row in rows],
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stores_code_key ON stores (code_key)"
    )


def connect(path: str | Path) -> sqlite3.Connection:
    """Open an autocommit connection with sane SQLite pragmas.

    ``isolation_level=None`` means transactions are always explicit, which
    makes the read-snapshot and write-transaction helpers below unambiguous.
    """

    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str | Path) -> None:
    """Create the schema if needed.  Safe to call from every worker at boot.

    Several gunicorn workers call this concurrently.  Switching to WAL and
    creating tables both need a brief write lock, so contention is retried with
    backoff instead of crashing a worker at boot.
    """

    db_path = Path(path)
    if db_path.parent and str(db_path.parent) not in ("", "."):
        db_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: sqlite3.OperationalError | None = None
    for attempt in range(10):
        conn = connect(db_path)
        try:
            # WAL is persistent in the database file; setting it once is enough.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(SCHEMA_SQL)
            # Additive, idempotent migration for databases created by older
            # versions of the application (adds/backfills stores.code_key).
            migrate_schema(conn)
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            time.sleep(0.05 * (attempt + 1))
        finally:
            conn.close()
    raise last_error  # type: ignore[misc]


@contextmanager
def read_snapshot(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Run a group of reads against one stable committed snapshot.

    The first SELECT after ``BEGIN`` pins the snapshot.  A concurrent writer
    committing later cannot change what subsequent SELECTs in this context see.
    """

    conn = connect(path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


@contextmanager
def write_transaction(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Serialise writers and make a multi-table replacement all-or-nothing."""

    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


def healthy(path: str | Path) -> bool:
    try:
        conn = connect(path)
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False
