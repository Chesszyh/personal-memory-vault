from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Final


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION: Final = 2
SCHEMA_SHA256: Final = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()


class DatabaseSchemaError(RuntimeError):
    """Raised when a database cannot be safely used with the current schema."""


def _user_schema_objects(database: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (str(row[0]), str(row[1]))
        for row in database.execute(
            "SELECT type, name FROM sqlite_schema "
            "WHERE type IN ('table', 'view', 'index', 'trigger') "
            "AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        )
    ]


def _initialize_schema(database: sqlite3.Connection) -> None:
    try:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        database.executescript(
            "BEGIN IMMEDIATE;\n"
            f"{schema_sql}\n"
            f"PRAGMA user_version = {SCHEMA_VERSION};\n"
            "COMMIT;\n"
        )
    except (OSError, sqlite3.Error) as error:
        if database.in_transaction:
            database.rollback()
        raise DatabaseSchemaError(
            f"failed to initialize database schema version {SCHEMA_VERSION}"
        ) from error

    initialized_version = int(database.execute("PRAGMA user_version").fetchone()[0])
    if initialized_version != SCHEMA_VERSION:
        raise DatabaseSchemaError(
            "database initialization produced schema version "
            f"{initialized_version}; expected {SCHEMA_VERSION}"
        )


def _validate_or_initialize_schema(database: sqlite3.Connection, path: Path) -> None:
    schema_version = int(database.execute("PRAGMA user_version").fetchone()[0])
    if schema_version == SCHEMA_VERSION:
        return

    if schema_version != 0:
        raise DatabaseSchemaError(
            f"database {path} has schema version {schema_version}; "
            f"expected {SCHEMA_VERSION}; automatic migrations are not available"
        )

    existing_objects = _user_schema_objects(database)
    if existing_objects:
        object_summary = ", ".join(
            f"{object_type} {name}" for object_type, name in existing_objects[:5]
        )
        if len(existing_objects) > 5:
            object_summary += f", and {len(existing_objects) - 5} more"
        raise DatabaseSchemaError(
            f"database {path} has schema version 0 but is non-empty "
            f"({object_summary}); refusing to initialize; expected {SCHEMA_VERSION}"
        )

    _initialize_schema(database)


def connect_database(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    try:
        _validate_or_initialize_schema(database, path)
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA journal_mode = WAL")
        database.execute("PRAGMA synchronous = FULL")
        database.row_factory = sqlite3.Row
        return database
    except BaseException:
        database.close()
        raise
