"""Transactional ``ticket_tools_update_v1`` SQLite migration."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable


UPDATE_MIGRATION_ID = "ticket_tools_update_v1"
UPDATE_SCHEMA_VERSION = 1

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_update_executions (
    mutation_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    originating_session_id TEXT NOT NULL,
    originating_conversation_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    base_version INTEGER NOT NULL CHECK (base_version >= 1),
    serialization_version INTEGER NOT NULL CHECK (serialization_version = 1),
    canonical_patch_bytes BLOB NOT NULL,
    patch_bytes_hash TEXT NOT NULL,
    if_match_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged','active','retired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    retired_at TEXT,
    refresh_reservation_id TEXT UNIQUE,
    refresh_state TEXT CHECK (refresh_state IN ('reserved','committed','failed')),
    refresh_used_at TEXT,
    refresh_base_credential_generation INTEGER,
    refresh_committed_credential_generation INTEGER,
    refresh_committed_at TEXT
)
""".strip()

_LOCATOR_SQL = """
CREATE INDEX IF NOT EXISTS idx_pending_update_execution_locator
ON pending_update_executions(
    user_id, originating_session_id, originating_conversation_id, ticket_id, status
)
""".strip()

_ACTIVE_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_update_execution_active_scope
ON pending_update_executions(user_id, originating_session_id, originating_conversation_id)
WHERE status = 'active'
""".strip()

_MARKER_SQL = """
CREATE TABLE IF NOT EXISTS ticket_tools_update_v1_migrations (
    migration_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    schema_fingerprint TEXT NOT NULL
)
""".strip()

def _canonical_sql(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    return normalized.replace("create table if not exists", "create table").replace(
        "create index if not exists", "create index"
    ).replace("create unique index if not exists", "create unique index")


_EXPECTED_DEFINITIONS = tuple(
    _canonical_sql(value) for value in (_TABLE_SQL, _LOCATOR_SQL, _ACTIVE_SQL)
)
UPDATE_SCHEMA_FINGERPRINT = hashlib.sha256(
    "\n".join(_EXPECTED_DEFINITIONS).encode("utf-8")
).hexdigest()

_EXPECTED_COLUMNS = (
    "mutation_id",
    "user_id",
    "originating_session_id",
    "originating_conversation_id",
    "ticket_id",
    "base_version",
    "serialization_version",
    "canonical_patch_bytes",
    "patch_bytes_hash",
    "if_match_version",
    "status",
    "created_at",
    "expires_at",
    "retired_at",
    "refresh_reservation_id",
    "refresh_state",
    "refresh_used_at",
    "refresh_base_credential_generation",
    "refresh_committed_credential_generation",
    "refresh_committed_at",
)

_EXPECTED_TABLE_INFO = (
    ("mutation_id", "TEXT", 0, None, 1),
    ("user_id", "INTEGER", 1, None, 0),
    ("originating_session_id", "TEXT", 1, None, 0),
    ("originating_conversation_id", "TEXT", 1, None, 0),
    ("ticket_id", "TEXT", 1, None, 0),
    ("base_version", "INTEGER", 1, None, 0),
    ("serialization_version", "INTEGER", 1, None, 0),
    ("canonical_patch_bytes", "BLOB", 1, None, 0),
    ("patch_bytes_hash", "TEXT", 1, None, 0),
    ("if_match_version", "TEXT", 1, None, 0),
    ("status", "TEXT", 1, None, 0),
    ("created_at", "TEXT", 1, None, 0),
    ("expires_at", "TEXT", 1, None, 0),
    ("retired_at", "TEXT", 0, None, 0),
    ("refresh_reservation_id", "TEXT", 0, None, 0),
    ("refresh_state", "TEXT", 0, None, 0),
    ("refresh_used_at", "TEXT", 0, None, 0),
    ("refresh_base_credential_generation", "INTEGER", 0, None, 0),
    ("refresh_committed_credential_generation", "INTEGER", 0, None, 0),
    ("refresh_committed_at", "TEXT", 0, None, 0),
)

_EXPECTED_INDEXES = {
    "idx_pending_update_execution_locator": (
        0,
        0,
        (
            "user_id",
            "originating_session_id",
            "originating_conversation_id",
            "ticket_id",
            "status",
        ),
    ),
    "uq_pending_update_execution_active_scope": (
        1,
        1,
        ("user_id", "originating_session_id", "originating_conversation_id"),
    ),
}


def verify_ticket_update_schema(conn: sqlite3.Connection) -> None:
    table_info = tuple(
        (row[1], row[2].upper(), row[3], row[4], row[5])
        for row in conn.execute("PRAGMA table_info(pending_update_executions)")
    )
    columns = tuple(row[0] for row in table_info)
    index_rows = {
        row[1]: (row[2], row[4])
        for row in conn.execute("PRAGMA index_list(pending_update_executions)")
    }
    definitions = []
    for object_type, name in (
        ("table", "pending_update_executions"),
        ("index", "idx_pending_update_execution_locator"),
        ("index", "uq_pending_update_execution_active_scope"),
    ):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (object_type, name)
        ).fetchone()
        definitions.append(_canonical_sql(row[0]) if row and isinstance(row[0], str) else "")
    marker = conn.execute(
        """SELECT schema_version, schema_fingerprint
           FROM ticket_tools_update_v1_migrations WHERE migration_id=?""",
        (UPDATE_MIGRATION_ID,),
    ).fetchone()
    if columns != _EXPECTED_COLUMNS or table_info != _EXPECTED_TABLE_INFO:
        raise RuntimeError("ticket update schema fingerprint mismatch")
    for name, (unique, partial, expected_columns) in _EXPECTED_INDEXES.items():
        if index_rows.get(name) != (unique, partial):
            raise RuntimeError("ticket update index fingerprint mismatch")
        actual_columns = tuple(
            row[2]
            for row in conn.execute(f'PRAGMA index_xinfo("{name}")')
            if row[5] == 1
        )
        if actual_columns != expected_columns:
            raise RuntimeError("ticket update index fingerprint mismatch")
    actual_fingerprint = hashlib.sha256("\n".join(definitions).encode("utf-8")).hexdigest()
    if tuple(definitions) != _EXPECTED_DEFINITIONS or actual_fingerprint != UPDATE_SCHEMA_FINGERPRINT:
        raise RuntimeError("ticket update schema definition mismatch")
    if marker is None or (marker[0], marker[1]) != (
        UPDATE_SCHEMA_VERSION,
        UPDATE_SCHEMA_FINGERPRINT,
    ):
        raise RuntimeError("ticket update migration marker mismatch")


def migrate_ticket_updates(
    conn: sqlite3.Connection,
    *,
    _after_schema_hook: Callable[[], None] | None = None,
) -> None:
    """Apply or verify U3 after U1 schema setup and before plugin assembly."""
    conn.execute("SAVEPOINT ticket_tools_update_v1")
    try:
        conn.execute(_MARKER_SQL)
        marker = conn.execute(
            "SELECT schema_version, schema_fingerprint FROM ticket_tools_update_v1_migrations WHERE migration_id=?",
            (UPDATE_MIGRATION_ID,),
        ).fetchone()
        if marker is not None:
            verify_ticket_update_schema(conn)
            conn.execute("RELEASE SAVEPOINT ticket_tools_update_v1")
            return
        conn.execute(_TABLE_SQL)
        conn.execute(_LOCATOR_SQL)
        conn.execute(_ACTIVE_SQL)
        if _after_schema_hook is not None:
            _after_schema_hook()
        conn.execute(
            """INSERT INTO ticket_tools_update_v1_migrations
               (migration_id, schema_version, schema_fingerprint) VALUES (?, ?, ?)""",
            (UPDATE_MIGRATION_ID, UPDATE_SCHEMA_VERSION, UPDATE_SCHEMA_FINGERPRINT),
        )
        verify_ticket_update_schema(conn)
        conn.execute("RELEASE SAVEPOINT ticket_tools_update_v1")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ticket_tools_update_v1")
        conn.execute("RELEASE SAVEPOINT ticket_tools_update_v1")
        raise
