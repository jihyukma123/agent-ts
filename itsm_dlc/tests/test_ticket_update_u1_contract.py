from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.db import init_db, open_db
from app.ticketing.capability import DelegatedCredential, TicketRequestCapability
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import PendingMutationBindingV1, PendingUpdateMutationV1, UpdatePendingReplacementV1
from app.ticketing.repositories import PendingMutationRepository


async def _credential():
    return DelegatedCredential("delegated")


def _cap(user_id=7):
    return TicketRequestCapability(user_id, "session-generated", "conversation-generated", _credential, _credential)


def _mutation(mutation_id: str, *, payload_hash: str = "a" * 64, base_version: int = 3):
    return PendingUpdateMutationV1(
        mutation_id=mutation_id,
        user_id=7,
        session_id="session-generated",
        conversation_id="conversation-generated",
        ticket_id="ticket-generated",
        normalized_payload_hash=payload_hash,
        base_version=base_version,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )


def _binding(mutation: PendingUpdateMutationV1):
    return PendingMutationBindingV1(
        mutation_id=mutation.mutation_id,
        operation="update",
        user_id=mutation.user_id,
        session_id=mutation.session_id,
        conversation_id=mutation.conversation_id,
        ticket_id=mutation.ticket_id,
        normalized_payload_hash=mutation.normalized_payload_hash,
        base_version=mutation.base_version,
    )


def _evidence(decision="explicit_confirm"):
    return CurrentTurnEvidenceV1(
        turn_id="turn-generated",
        user_id=7,
        session_id="session-generated",
        conversation_id="conversation-generated",
        raw_message_sha256="b" * 64,
        decision=decision,
    )


def test_prepare_returns_complete_committed_replacement_evidence(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    result = PendingMutationRepository(db).replace_update_pending(_cap(), None, _mutation("new-generated"))
    assert isinstance(result, UpdatePendingReplacementV1)
    assert result.old_mutation_id is None and result.new_mutation_id == "new-generated"
    assert result.committed is True and result.affected_old_rows == 0 and result.inserted_new_rows == 1
    assert result.new_ticket_id == "ticket-generated" and result.new_base_version == 3


def test_revise_returns_exact_old_row_evidence(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    old = _mutation("old-generated")
    assert repo.replace_update_pending(_cap(), None, old)
    new = _mutation("new-generated", payload_hash="c" * 64, base_version=4)
    result = repo.replace_update_pending(_cap(), old.mutation_id, new)
    assert result is not None
    assert (result.old_mutation_id, result.affected_old_rows) == (old.mutation_id, 1)
    assert result.new_normalized_payload_hash == "c" * 64


def test_atomic_confirm_and_consume_has_no_observable_confirmed_state(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    mutation = _mutation("confirm-generated")
    assert repo.replace_update_pending(_cap(), None, mutation)
    consumed = repo.confirm_and_consume_update(_binding(mutation), _evidence())
    assert consumed is not None and consumed.mutation_id == mutation.mutation_id
    assert repo.get(mutation.mutation_id).status == "consumed"
    with open_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_ticket_mutations WHERE status='confirmed'").fetchone()[0] == 0


def test_atomic_consume_rejects_wrong_turn_actor_and_binding(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    mutation = _mutation("protected-generated")
    assert repo.replace_update_pending(_cap(), None, mutation)
    assert repo.confirm_and_consume_update(_binding(mutation), _evidence("revise_or_ambiguous")) is None
    wrong = _binding(mutation).model_copy(update={"ticket_id": "other-generated"})
    assert repo.confirm_and_consume_update(wrong, _evidence()) is None
    assert repo.get(mutation.mutation_id).status == "pending"


def test_atomic_consume_has_one_concurrent_winner(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    mutation = _mutation("race-generated")
    assert repo.replace_update_pending(_cap(), None, mutation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repo.confirm_and_consume_update(_binding(mutation), _evidence()), range(2)))
    assert sum(result is not None for result in results) == 1


def test_existing_separate_confirm_consume_api_still_works(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    repo = PendingMutationRepository(db)
    mutation = _mutation("legacy-generated")
    assert repo.replace_update_pending(_cap(), None, mutation)
    assert repo.confirm(_binding(mutation), _evidence()) is not None
    assert repo.consume_once(_binding(mutation)) is not None

# The U3 exact-file coverage gate owns the shared U1 provider changes. Re-export
# its established provider regression cases so that exact U3 execution measures
# the whole approved shared-file surface instead of relying on the later full run.
from tests.test_ticket_repositories import *  # noqa: F403,E402,F401
