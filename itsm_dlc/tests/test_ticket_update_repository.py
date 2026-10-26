from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.db import init_db, open_db
from app.ticketing.models import UpdatePendingReplacementV1
from app.ticketing.update_models import PendingUpdateExecutionV1, TicketPatchInV1
from app.ticketing.update_repository import PendingUpdateExecutionRepository


def _execution(mutation_id: str, **overrides) -> PendingUpdateExecutionV1:
    canonical = TicketPatchInV1(title=f"title-{mutation_id}").canonical_patch()
    now = datetime.now(timezone.utc)
    data = dict(
        mutation_id=mutation_id,
        user_id=7,
        originating_session_id="session-generated",
        originating_conversation_id="conversation-generated",
        ticket_id="ticket-generated",
        base_version=3,
        canonical_patch_bytes=canonical.body_bytes,
        patch_bytes_hash=canonical.sha256,
        if_match_version='"3"',
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    data.update(overrides)
    return PendingUpdateExecutionV1(**data)


def _evidence(execution: PendingUpdateExecutionV1, old: str | None = None):
    return UpdatePendingReplacementV1(
        old_mutation_id=old,
        new_mutation_id=execution.mutation_id,
        user_id=execution.user_id,
        session_id=execution.originating_session_id,
        conversation_id=execution.originating_conversation_id,
        new_ticket_id=execution.ticket_id,
        new_normalized_payload_hash=execution.patch_bytes_hash,
        new_base_version=execution.base_version,
        affected_old_rows=1 if old else 0,
    )


def _repo(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    return db, PendingUpdateExecutionRepository(db)


def test_stage_and_get_round_trip_exact_server_fields(tmp_path):
    _db, repo = _repo(tmp_path)
    execution = _execution("mutation-generated")
    assert repo.stage(execution) == execution.mutation_id
    restored = repo.get(execution.mutation_id)
    assert restored == execution
    assert restored.patch().selected_payload() == {"title": "title-mutation-generated"}


def test_prepare_activation_produces_one_active_row(tmp_path):
    db, repo = _repo(tmp_path)
    execution = _execution("new-generated")
    repo.stage(execution)
    assert repo.activate_replacement(_evidence(execution)) is True
    assert repo.get(execution.mutation_id).status == "active"
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_update_executions WHERE status='active'").fetchone()[0] == 1


def test_revise_atomically_retires_exact_old_and_promotes_new(tmp_path):
    db, repo = _repo(tmp_path)
    old = _execution("old-generated")
    repo.stage(old)
    assert repo.activate_replacement(_evidence(old))
    new = _execution("new-generated", base_version=4, if_match_version='"4"')
    repo.stage(new)
    assert repo.activate_replacement(_evidence(new, old.mutation_id))
    assert repo.get(old.mutation_id).status == "retired"
    assert repo.get(new.mutation_id).status == "active"
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_update_executions WHERE status='active'").fetchone()[0] == 1


def test_activation_rejects_wrong_binding_and_preserves_old(tmp_path):
    _db, repo = _repo(tmp_path)
    old = _execution("old-generated")
    repo.stage(old)
    assert repo.activate_replacement(_evidence(old))
    new = _execution("new-generated")
    repo.stage(new)
    evidence = _evidence(new, old.mutation_id).model_copy(update={"new_ticket_id": "other-generated"})
    assert repo.activate_replacement(evidence) is False
    assert repo.get(old.mutation_id).status == "active"
    assert repo.get(new.mutation_id).status == "staged"


def test_prepare_does_not_replace_existing_active_row(tmp_path):
    _db, repo = _repo(tmp_path)
    old = _execution("old-generated")
    repo.stage(old)
    assert repo.activate_replacement(_evidence(old))
    new = _execution("new-generated")
    repo.stage(new)
    assert repo.activate_replacement(_evidence(new)) is False


def test_exact_actor_ticket_locator_is_isolated(tmp_path):
    _db, repo = _repo(tmp_path)
    execution = _execution("locator-generated")
    repo.stage(execution)
    assert repo.activate_replacement(_evidence(execution))
    found = repo.find_active_for_actor_ticket(
        user_id=7,
        session_id="session-generated",
        conversation_id="conversation-generated",
        ticket_id="ticket-generated",
    )
    assert found is not None and found.mutation_id == execution.mutation_id
    assert repo.find_active_for_actor_ticket(
        user_id=8,
        session_id="session-generated",
        conversation_id="conversation-generated",
        ticket_id="ticket-generated",
    ) is None


def test_refresh_reservation_is_durable_and_one_shot(tmp_path):
    _db, repo = _repo(tmp_path)
    execution = _execution("refresh-generated")
    repo.stage(execution)
    assert repo.activate_replacement(_evidence(execution))
    reservation = repo.reserve_refresh_once(execution.mutation_id, base_credential_generation=2)
    assert reservation is not None and reservation.refresh_state == "reserved"
    assert repo.reserve_refresh_once(execution.mutation_id, base_credential_generation=2) is None
    assert repo.mark_refresh_committed(execution.mutation_id, committed_generation=3)
    restored = repo.get(execution.mutation_id)
    assert restored.refresh_state == "committed"
    assert restored.refresh_committed_credential_generation == 3


@pytest.mark.parametrize("generation,expected", [(3, "committed"), (2, "failed")])
def test_refresh_reconciliation_never_reissues_reservation(tmp_path, generation, expected):
    _db, repo = _repo(tmp_path)
    execution = _execution(f"refresh-{generation}")
    repo.stage(execution)
    assert repo.activate_replacement(_evidence(execution))
    assert repo.reserve_refresh_once(execution.mutation_id, base_credential_generation=2)
    assert repo.reconcile_refresh_after_restart(
        execution.mutation_id,
        current_session_credential_generation=generation,
    ) == expected
    assert repo.reserve_refresh_once(execution.mutation_id, base_credential_generation=generation) is None


def test_retire_scope_and_cleanup_are_idempotent(tmp_path):
    db, repo = _repo(tmp_path)
    execution = _execution("cleanup-generated")
    repo.stage(execution)
    assert repo.activate_replacement(_evidence(execution))
    assert repo.retire_scope(conversation_id="conversation-generated") == 1
    assert repo.retire_scope(conversation_id="conversation-generated") == 0
    now = datetime.now(timezone.utc) + timedelta(days=2)
    assert repo.cleanup(now=now, retention_cutoff=now) == 1
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_update_executions").fetchone()[0] == 0


def test_tampered_row_fails_closed_on_read(tmp_path):
    db, repo = _repo(tmp_path)
    execution = _execution("tampered-generated")
    repo.stage(execution)
    with open_db(db) as conn, conn:
        conn.execute("UPDATE pending_update_executions SET patch_bytes_hash=?", ("0" * 64,))
    with pytest.raises(ValidationError, match="stored patch hash mismatch"):
        repo.get(execution.mutation_id)
