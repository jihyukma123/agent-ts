from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.db import init_db, open_db
from app.session import load_current_user
from app.ticketing.capability import DelegatedCredential, TicketRequestCapability
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import (
    CanonicalTicketQueryV1,
    PendingMutationBindingV1,
    PendingTicketMutationV1,
    PendingUpdateMutationV1,
    TicketPresentationV1,
)
from app.ticketing.repositories import (
    ContinuationReferenceGenerator,
    CursorRepository,
    PendingMutationRepository,
    PresentationRepository,
)


async def _token():
    return DelegatedCredential("delegated")


def _cap(user_id=7, session_id="session-a", conversation_id="conversation-a"):
    return TicketRequestCapability(user_id, session_id, conversation_id, _token, _token)


def _mutation(operation: str, mutation_id: str, **overrides):
    data = dict(
        mutation_id=mutation_id,
        operation=operation,
        user_id=7,
        session_id="session-a",
        conversation_id="conversation-a",
        normalized_payload_hash="a" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    if operation == "update":
        data.update(ticket_id="generated-id", base_version=3)
    data.update(overrides)
    return PendingTicketMutationV1(**data)


def _binding(mutation: PendingTicketMutationV1, **overrides):
    data = mutation.model_dump(
        include={
            "mutation_id",
            "operation",
            "user_id",
            "session_id",
            "conversation_id",
            "normalized_payload_hash",
            "ticket_id",
            "base_version",
        }
    )
    data.update(overrides)
    return PendingMutationBindingV1.model_validate(data)


def _update(mutation_id: str, **overrides) -> PendingUpdateMutationV1:
    return PendingUpdateMutationV1.model_validate(
        _mutation("update", mutation_id, **overrides).model_dump()
    )


def _evidence(decision="explicit_confirm", **overrides):
    data = dict(
        turn_id="turn-generated",
        user_id=7,
        session_id="session-a",
        conversation_id="conversation-a",
        raw_message_sha256="b" * 64,
        decision=decision,
    )
    data.update(overrides)
    return CurrentTurnEvidenceV1(**data)


LEGACY_SESSION_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, username TEXT NOT NULL,
    display_name TEXT NOT NULL, role TEXT NOT NULL, role_label TEXT NOT NULL,
    team TEXT, permissions TEXT NOT NULL, conversation_id TEXT NOT NULL UNIQUE,
    itsm_token TEXT NOT NULL, itsm_token_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def test_pending_repository_round_trips_both_variants(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    repo.save_review(_mutation("create", "create-1"))
    assert repo.replace_update_pending(_cap(), None, _update("update-1")) is not None
    assert repo.get("create-1").operation == "create"
    assert repo.get("update-1").ticket_id == "generated-id"


def test_pending_replacement_is_per_operation(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    repo.save_review(_mutation("create", "create-old"))
    assert repo.replace_update_pending(_cap(), None, _update("update-kept")) is not None
    repo.save_review(_mutation("create", "create-new"))
    assert repo.get("create-old").status == "invalidated"
    assert repo.get("create-new").status == "pending"
    assert repo.get("update-kept").status == "pending"


def test_update_prepare_requires_an_empty_pending_scope(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    first = _update("update-first")
    assert repo.replace_update_pending(_cap(), None, first).mutation_id == first.mutation_id
    assert repo.replace_update_pending(_cap(), None, _update("update-second")) is None
    assert repo.get("update-first").status == "pending"
    assert repo.get("update-second") is None


def test_update_revise_replaces_only_the_exact_pending_row(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    old = _update("update-old")
    assert repo.replace_update_pending(_cap(), None, old) is not None
    new = _update("update-new", normalized_payload_hash="c" * 64, base_version=4)
    result = repo.replace_update_pending(_cap(), old.mutation_id, new)
    assert result is not None
    assert result.model_dump() == {
        "mutation_id": "update-new",
        "replaced_mutation_id": "update-old",
    }
    assert repo.get("update-old").status == "invalidated"
    assert repo.get("update-new").status == "pending"


def test_confirmed_update_blocks_prepare_and_revise(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    old = _update("update-confirmed")
    assert repo.replace_update_pending(_cap(), None, old) is not None
    assert repo.confirm(_binding(old), _evidence()) is not None
    assert repo.replace_update_pending(_cap(), None, _update("blocked-prepare")) is None
    assert repo.replace_update_pending(_cap(), old.mutation_id, _update("blocked-revise")) is None
    assert repo.get(old.mutation_id).status == "confirmed"
    assert repo.get("blocked-prepare") is None and repo.get("blocked-revise") is None


def test_save_review_rejects_update_bypass_and_preserves_confirmed_authority(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    confirmed = _update("update-confirmed-bypass")
    assert repo.replace_update_pending(_cap(), None, confirmed) is not None
    assert repo.confirm(_binding(confirmed), _evidence()) is not None
    patch_calls = []

    with pytest.raises(ValueError, match="create mutations only"):
        repo.save_review(_update("update-bypass"))

    assert repo.get(confirmed.mutation_id).status == "confirmed"
    assert repo.get("update-bypass") is None
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_ticket_mutations WHERE operation='update' AND status='pending'"
        ).fetchone()[0] == 0
    assert patch_calls == []


def test_update_replacement_rejects_actor_or_expected_old_mismatch_without_changes(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    old = _update("update-owned")
    assert repo.replace_update_pending(_cap(), None, old) is not None
    assert repo.replace_update_pending(_cap(user_id=8), old.mutation_id, _update("wrong-actor")) is None
    assert repo.replace_update_pending(_cap(), "not-the-old-row", _update("wrong-old")) is None
    assert repo.get(old.mutation_id).status == "pending"
    assert repo.get("wrong-actor") is None and repo.get("wrong-old") is None


def test_update_replacement_insert_failure_rolls_back_old_row(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    old = _update("update-current")
    duplicate = _mutation("create", "duplicate-id")
    assert repo.replace_update_pending(_cap(), None, old) is not None
    repo.save_review(duplicate)
    proposed = _update("duplicate-id", normalized_payload_hash="d" * 64)
    assert repo.replace_update_pending(_cap(), old.mutation_id, proposed) is None
    assert repo.get(old.mutation_id).status == "pending"
    assert repo.get("duplicate-id").operation == "create"


def test_pending_lifecycle_invalidation_is_idempotent(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    repo.save_review(_mutation("create", "create-1"))
    assert repo.invalidate(conversation_id="conversation-a") == 1
    assert repo.invalidate(conversation_id="conversation-a") == 0
    with pytest.raises(ValueError):
        repo.invalidate()


@pytest.mark.parametrize("operation", ["create", "update"])
def test_pending_confirm_and_atomic_consume_once_preserve_variant_binding(operation, tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    mutation = _mutation(operation, f"{operation}-confirm")
    binding = _binding(mutation)
    if operation == "update":
        assert repo.replace_update_pending(_cap(), None, _update(mutation.mutation_id)) is not None
    else:
        repo.save_review(mutation)
    confirmed = repo.confirm(binding, _evidence())
    assert confirmed is not None and confirmed.status == "confirmed"
    assert (confirmed.ticket_id, confirmed.base_version) == (mutation.ticket_id, mutation.base_version)
    consumed = repo.consume_once(binding)
    assert consumed is not None
    assert set(consumed.model_dump()) == {"mutation_id", "confirmed_at"}
    assert consumed.mutation_id == mutation.mutation_id
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT status FROM pending_ticket_mutations WHERE mutation_id = ?",
            (mutation.mutation_id,),
        ).fetchone()[0] == "consumed"
    assert repo.consume_once(binding) is None
    assert repo.confirm(binding, _evidence()) is None


def test_pending_consume_concurrency_returns_exactly_one_minimal_evidence(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    mutation = _mutation("create", "create-concurrent")
    binding = _binding(mutation)
    repo.save_review(mutation)
    assert repo.confirm(binding, _evidence()) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repo.consume_once(binding), range(2)))

    evidence = [result for result in results if result is not None]
    assert len(evidence) == 1
    assert set(evidence[0].model_dump()) == {"mutation_id", "confirmed_at"}


def test_consume_before_bind_preserves_u2_attempt_and_requires_new_confirmation(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    with open_db(db) as conn, conn:
        conn.execute(
            """INSERT INTO ticket_create_attempts (
                   create_attempt_id,
                   user_id,
                   originating_session_id,
                   originating_conversation_id,
                   normalized_payload_hash,
                   canonical_request_bytes,
                   serialization_version,
                   request_bytes_hash,
                   idempotency_key,
                   attempt_count,
                   status,
                   created_at,
                   retained_until
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "attempt-1",
                7,
                "session-1",
                "conversation-1",
                "a" * 64,
                b'{"title":"same bytes"}',
                1,
                "b" * 64,
                "stable-idempotency-key",
                0,
                "prepared",
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
            ),
        )
        before = tuple(conn.execute("SELECT * FROM ticket_create_attempts").fetchone())

    repo = PendingMutationRepository(db)
    first = _mutation("create", "create-before-crash")
    repo.save_review(first)
    assert repo.confirm(_binding(first), _evidence()) is not None
    consumed = repo.consume_once(_binding(first))
    transport_calls = {"post": 0, "patch": 0}
    assert consumed is not None
    assert set(consumed.model_dump()) == {"mutation_id", "confirmed_at"}

    # Crash before U2 binds evidence: U1 cannot replay evidence or authorize transport.
    assert repo.consume_once(_binding(first)) is None
    second = _mutation("create", "create-after-restart")
    repo.save_review(second)
    assert repo.consume_once(_binding(second)) is None
    assert repo.confirm(_binding(second), _evidence()) is not None
    assert repo.consume_once(_binding(second)) is not None
    assert transport_calls == {"post": 0, "patch": 0}
    with open_db(db) as conn:
        after = tuple(conn.execute("SELECT * FROM ticket_create_attempts").fetchone())
    assert after == before


@pytest.mark.parametrize(
    "binding_override,evidence_override",
    [
        ({"user_id": 8}, {}),
        ({"session_id": "session-other"}, {}),
        ({"conversation_id": "conversation-other"}, {}),
        ({"normalized_payload_hash": "c" * 64}, {}),
        ({}, {"decision": "revise_or_ambiguous"}),
        ({}, {"user_id": 8}),
    ],
)
def test_pending_confirmation_rejects_mismatch_and_non_confirmation_without_transition(
    binding_override, evidence_override, tmp_path
):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    mutation = _mutation("create", "create-mismatch")
    repo.save_review(mutation)
    assert repo.confirm(_binding(mutation, **binding_override), _evidence(**evidence_override)) is None
    assert repo.get(mutation.mutation_id).status == "pending"


@pytest.mark.parametrize(
    "binding_override",
    [
        {"user_id": 8},
        {"session_id": "session-other"},
        {"normalized_payload_hash": "d" * 64},
        {"ticket_id": "different-generated-id"},
        {"base_version": 4},
    ],
)
def test_pending_consume_mismatch_returns_no_evidence_and_preserves_owner(binding_override, tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    mutation = _mutation("update", "update-consume-mismatch")
    owner_binding = _binding(mutation)
    assert repo.replace_update_pending(_cap(), None, _update(mutation.mutation_id)) is not None
    assert repo.confirm(owner_binding, _evidence()) is not None
    assert repo.consume_once(_binding(mutation, **binding_override)) is None
    assert repo.get(mutation.mutation_id).status == "confirmed"
    assert repo.consume_once(owner_binding) is not None


def test_pending_expired_invalidated_and_wrong_target_never_confirm_or_consume(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    expired = _mutation("update", "update-expired", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    with open_db(db) as conn, conn:
        conn.execute(
            """INSERT INTO pending_ticket_mutations
               (mutation_id, operation, user_id, session_id, conversation_id,
                normalized_payload_hash, ticket_id, base_version, status, expires_at)
               VALUES (?, 'update', ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                expired.mutation_id,
                expired.user_id,
                expired.session_id,
                expired.conversation_id,
                expired.normalized_payload_hash,
                expired.ticket_id,
                expired.base_version,
                expired.expires_at.isoformat().replace("+00:00", "Z"),
            ),
        )
    assert repo.confirm(_binding(expired), _evidence()) is None
    assert repo.get(expired.mutation_id).status == "expired"

    active = _mutation("update", "update-active")
    assert repo.replace_update_pending(_cap(), None, _update(active.mutation_id)) is not None
    wrong_target = _binding(active, ticket_id="different-generated-id")
    assert repo.confirm(wrong_target, _evidence()) is None
    repo.invalidate(conversation_id=active.conversation_id)
    assert repo.confirm(_binding(active), _evidence()) is None
    assert repo.consume_once(_binding(active)) is None


def test_cursor_round_trip_is_once_only_and_hides_upstream_value(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = CursorRepository(db)
    query = CanonicalTicketQueryV1(q="network", status=["received"])
    ref = repo.issue(_cap(), query, "upstream-secret-cursor")
    assert "upstream" not in ref
    resolved = repo.resolve_once(_cap(), ref)
    assert resolved.query == query and resolved.upstream_cursor == "upstream-secret-cursor"
    assert repo.resolve_once(_cap(), ref) is None


@pytest.mark.parametrize(
    "capability",
    [_cap(user_id=8), _cap(session_id="session-b"), _cap(conversation_id="conversation-b")],
)
def test_cursor_binding_mismatch_fails_closed_without_consuming_owner_reference(capability, tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = CursorRepository(db)
    ref = repo.issue(_cap(), CanonicalTicketQueryV1(q="network"), "raw")
    assert repo.resolve_once(capability, ref) is None
    assert repo.resolve_once(_cap(), ref).upstream_cursor == "raw"


def test_cursor_hash_tamper_and_expiry_fail_closed(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = CursorRepository(db, lifetime=timedelta(seconds=-1))
    expired = repo.issue(_cap(), CanonicalTicketQueryV1(), "raw")
    assert repo.resolve_once(_cap(), expired) is None

    repo = CursorRepository(db)
    valid = repo.issue(_cap(), CanonicalTicketQueryV1(), "raw")
    with open_db(db) as conn, conn:
        conn.execute("UPDATE ticket_cursor_bindings SET normalized_query_hash = ? WHERE continuation_ref = ?", ("0" * 64, valid))
    assert repo.resolve_once(_cap(), valid) is None
    with open_db(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?", (valid,)
        ).fetchone()[0] == 1


def test_cursor_startup_cleanup_deletes_only_expired_rows(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    expired_repo = CursorRepository(db, lifetime=timedelta(seconds=-1))
    expired_repo.issue(_cap(), CanonicalTicketQueryV1(q="old"), "raw-old")
    current_repo = CursorRepository(db)
    current = current_repo.issue(_cap(), CanonicalTicketQueryV1(q="current"), "raw-current")
    assert current_repo.cleanup_expired() == 1
    assert current_repo.resolve_once(_cap(), current).upstream_cursor == "raw-current"


def _history_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE agent_sessions (session_id TEXT PRIMARY KEY);
            CREATE TABLE agent_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, message_data TEXT);
            """
        )
        conn.execute("INSERT INTO agent_sessions VALUES ('conversation-a')")
        conn.execute(
            "INSERT INTO agent_messages(session_id, message_data) VALUES (?, ?)",
            ("conversation-a", json.dumps({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "reply"}]})),
        )


def _presentation():
    return TicketPresentationV1.model_validate(
        {"version": 1, "kind": "ticket_list", "blocks": [{"type": "paragraph", "text": {"text": "1건", "origin": "server_allowlist"}}]}
    )


def test_presentation_exact_correlation_round_trip_and_restart(tmp_path):
    db = tmp_path / "history.db"
    _history_db(db)
    repo = PresentationRepository(db)
    message = repo.latest_assistant("conversation-a")
    assert message is not None and message.text == "reply"
    assert repo.put_and_readback("conversation-a", message, _presentation()) == _presentation()
    restarted = PresentationRepository(db)
    assert restarted.load("conversation-a", message.sequence, "reply") == _presentation()
    assert restarted.load("conversation-a", message.sequence, "changed") is None


def test_presentation_corrupt_or_unknown_metadata_falls_back(tmp_path):
    db = tmp_path / "history.db"
    _history_db(db)
    repo = PresentationRepository(db)
    message = repo.latest_assistant("conversation-a")
    repo.put_and_readback("conversation-a", message, _presentation())
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE ticket_message_presentations SET canonical_presentation_json = '{}' ")
        conn.commit()
    assert repo.load("conversation-a", message.sequence, "reply") is None


def test_presentation_clear_and_orphan_cleanup_are_idempotent(tmp_path):
    db = tmp_path / "history.db"
    _history_db(db)
    repo = PresentationRepository(db)
    message = repo.latest_assistant("conversation-a")
    repo.put_and_readback("conversation-a", message, _presentation())
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM agent_messages")
        conn.commit()
    assert repo.cleanup_orphans() == 1
    assert repo.cleanup_orphans() == 0
    assert repo.clear("conversation-a") == 0


def test_legacy_session_migration_is_additive_and_backfills_absolute_expiry(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(LEGACY_SESSION_SCHEMA)
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sid", 7, "member", "Member", "requester", "Requester", None,
                '["ticket:read"]', "conversation", "delegated-secret",
                "2026-01-02T00:00:00Z", "2026-01-01T03:04:05Z",
            ),
        )
        before_columns = [tuple(row[1:6]) for row in conn.execute("PRAGMA table_info(sessions)")]
        before_values = conn.execute("SELECT * FROM sessions").fetchone()

    init_db(db)
    init_db(db)

    with sqlite3.connect(db) as conn:
        after_columns = [tuple(row[1:6]) for row in conn.execute("PRAGMA table_info(sessions)")]
        after_values = conn.execute("SELECT * FROM sessions").fetchone()
    assert after_columns[:12] == before_columns
    assert after_columns[12][0:3] == ("session_expires_at", "TEXT", 1)
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA table_info(sessions)").fetchall()[12][4] == "'1970-01-01T00:00:00Z'"
    assert after_values[:12] == before_values
    assert after_values[12] == "2026-01-01T11:04:05Z"


def test_migrated_session_schema_accepts_prior_release_create_restore_login_logout_writes(tmp_path):
    db = tmp_path / "prior-release.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(LEGACY_SESSION_SCHEMA)
    init_db(db)

    original_columns = (
        "id, user_id, username, display_name, role, role_label, team, permissions, "
        "conversation_id, itsm_token, itsm_token_expires_at, created_at"
    )
    values = (
        "prior-sid", 7, "member", "Member", "requester", "Requester", None,
        '["ticket:read"]', "prior-conversation", "prior-token",
        "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z",
    )
    with open_db(db) as conn, conn:
        conn.execute(
            f"INSERT INTO sessions ({original_columns}) VALUES ({','.join('?' for _ in values)})",
            values,
        )
        restored = conn.execute(
            f"SELECT {original_columns} FROM sessions WHERE id = ?", ("prior-sid",)
        ).fetchone()
        assert tuple(restored) == values
        user = load_current_user(conn, "prior-sid")
        assert user is not None and user.session_expires_at == datetime(1970, 1, 1, tzinfo=timezone.utc)
        conn.execute(
            "UPDATE sessions SET itsm_token = ?, itsm_token_expires_at = ? WHERE id = ?",
            ("prior-token-refreshed", "2026-01-03T00:00:00Z", "prior-sid"),
        )
        assert conn.execute(
            "SELECT itsm_token FROM sessions WHERE id = ?", ("prior-sid",)
        ).fetchone()[0] == "prior-token-refreshed"
        conn.execute("DELETE FROM sessions WHERE id = ?", ("prior-sid",))
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize("created_at", ["", "not-a-time"])
def test_legacy_session_with_untrustworthy_creation_time_is_backfilled_expired(tmp_path, created_at):
    db = tmp_path / "legacy-malformed.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(LEGACY_SESSION_SCHEMA)
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sid", 7, "u", "U", "r", "R", None, "[]", "c", "secret", "2026-01-02T00:00:00Z", created_at),
        )
    init_db(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT session_expires_at FROM sessions").fetchone()[0] == "1970-01-01T00:00:00Z"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"entropy_bits": 127},
        {"random_bytes": lambda size: b"x" * size},
        {"random_bytes": lambda size: b"x" * size, "source_is_csprng": False},
    ],
)
def test_continuation_reference_rejects_weak_or_unattested_sources(kwargs):
    with pytest.raises(ValueError):
        ContinuationReferenceGenerator(**kwargs)


def test_continuation_reference_rejects_invalid_source_sample():
    generator = ContinuationReferenceGenerator(lambda size: b"short", source_is_csprng=True)
    with pytest.raises(ValueError):
        generator.generate()
