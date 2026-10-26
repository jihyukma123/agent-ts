from __future__ import annotations

import sqlite3

import pytest

from app.db import connect, init_db, open_db
from app.ticketing.update_migration import (
    UPDATE_MIGRATION_ID,
    UPDATE_SCHEMA_FINGERPRINT,
    migrate_ticket_updates,
    verify_ticket_update_schema,
)
from app.ticketing.update_repository import PendingUpdateExecutionRepository


def test_update_migration_runs_after_u1_and_creates_expected_inventory(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    with open_db(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        marker = conn.execute(
            "SELECT schema_fingerprint FROM ticket_tools_update_v1_migrations WHERE migration_id=?",
            (UPDATE_MIGRATION_ID,),
        ).fetchone()[0]
    assert {"pending_ticket_mutations", "pending_update_executions"}.issubset(tables)
    assert {"idx_pending_update_execution_locator", "uq_pending_update_execution_active_scope"}.issubset(indexes)
    assert marker == UPDATE_SCHEMA_FINGERPRINT


def test_update_migration_is_idempotent(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    with open_db(db) as conn, conn:
        migrate_ticket_updates(conn)
        migrate_ticket_updates(conn)
        assert conn.execute("SELECT COUNT(*) FROM ticket_tools_update_v1_migrations").fetchone()[0] == 1


def test_update_migration_rolls_back_forced_partial_failure(tmp_path):
    db = tmp_path / "state.db"
    conn = connect(db)
    try:
        with pytest.raises(RuntimeError, match="forced"):
            migrate_ticket_updates(conn, _after_schema_hook=lambda: (_ for _ in ()).throw(RuntimeError("forced")))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "pending_update_executions" not in tables
        assert "ticket_tools_update_v1_migrations" not in tables
    finally:
        conn.close()


def test_fingerprint_mismatch_fails_closed(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    with open_db(db) as conn, conn:
        conn.execute("UPDATE ticket_tools_update_v1_migrations SET schema_fingerprint='tampered'")
    with open_db(db) as conn:
        with pytest.raises(RuntimeError, match="marker mismatch"):
            verify_ticket_update_schema(conn)


@pytest.mark.parametrize(
    "replacement",
    (
        "CREATE INDEX uq_pending_update_execution_active_scope ON pending_update_executions(user_id, originating_session_id, originating_conversation_id) WHERE status='active'",
        "CREATE UNIQUE INDEX uq_pending_update_execution_active_scope ON pending_update_executions(user_id, originating_session_id, originating_conversation_id) WHERE status='staged'",
        "CREATE UNIQUE INDEX uq_pending_update_execution_active_scope ON pending_update_executions(user_id, originating_session_id, originating_conversation_id)",
    ),
)
def test_named_active_index_tamper_fails_before_repository_activation(tmp_path, replacement):
    db = tmp_path / "state.db"
    init_db(db)
    with open_db(db) as conn, conn:
        conn.execute("DROP INDEX uq_pending_update_execution_active_scope")
        conn.execute(replacement)
    with pytest.raises(RuntimeError, match="index|definition"):
        PendingUpdateExecutionRepository(db)


def test_named_table_column_contract_tamper_fails_before_repository_activation(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    with open_db(db) as conn, conn:
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pending_update_executions'"
        ).fetchone()[0]
        locator_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_pending_update_execution_locator'"
        ).fetchone()[0]
        active_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_pending_update_execution_active_scope'"
        ).fetchone()[0]
        conn.execute("DROP INDEX idx_pending_update_execution_locator")
        conn.execute("DROP INDEX uq_pending_update_execution_active_scope")
        conn.execute("ALTER TABLE pending_update_executions RENAME TO pending_update_executions_old")
        conn.execute(
            table_sql.replace(
                "base_version INTEGER NOT NULL CHECK (base_version >= 1)",
                "base_version TEXT NOT NULL",
            )
        )
        conn.execute(locator_sql)
        conn.execute(active_sql)
        conn.execute("DROP TABLE pending_update_executions_old")
    with pytest.raises(RuntimeError, match="schema"):
        PendingUpdateExecutionRepository(db)


def test_missing_marker_prevents_repository_and_plugin_activation(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    with open_db(db) as conn, conn:
        conn.execute(
            "DELETE FROM ticket_tools_update_v1_migrations WHERE migration_id=?",
            (UPDATE_MIGRATION_ID,),
        )
    with pytest.raises(RuntimeError, match="marker mismatch"):
        PendingUpdateExecutionRepository(db)


def test_prior_reader_can_open_additively_migrated_database(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("SELECT id, user_id FROM sessions").fetchall()
        conn.execute("SELECT mutation_id, operation FROM pending_ticket_mutations").fetchall()


def test_prior_reader_rollback_path_never_drops_update_namespace_or_rows(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    with open_db(db) as conn, conn:
        conn.execute(
            """INSERT INTO pending_update_executions (
                mutation_id, user_id, originating_session_id, originating_conversation_id,
                ticket_id, base_version, serialization_version, canonical_patch_bytes,
                patch_bytes_hash, if_match_version, status, created_at, expires_at
            ) VALUES ('compat-row', 1, 's', 'c', 'T-1', 1, 1, x'7B7D',
                      'compat-hash', '"1"', 'retired',
                      '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z')"""
        )
    # Simulate the immediately prior application opening and transacting only
    # against namespaces it knows; additive U3 state must remain untouched.
    with sqlite3.connect(db) as prior:
        prior.execute("BEGIN")
        prior.execute("SELECT id, user_id FROM sessions").fetchall()
        prior.rollback()
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_update_executions WHERE mutation_id='compat-row'"
        ).fetchone()[0] == 1


def test_reset_recreates_update_namespace(tmp_path):
    from app.db import reset_db

    db = tmp_path / "state.db"
    history = tmp_path / "history.db"
    init_db(db)
    reset_db(db, history)
    with open_db(db) as conn:
        verify_ticket_update_schema(conn)
