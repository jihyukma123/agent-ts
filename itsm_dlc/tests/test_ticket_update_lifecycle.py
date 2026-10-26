from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent.service import AgentService
from app.db import init_db, open_db
from app.ticketing.capability import DelegatedCredential, TicketRequestCapability
from app.ticketing.models import PendingUpdateMutationV1
from app.ticketing.repositories import PendingMutationRepository
from app.ticketing.update_models import PendingUpdateExecutionV1, TicketPatchInV1
from app.ticketing.update_repository import PendingUpdateExecutionRepository
from tests.conftest import make_settings


def _execution(mutation_id: str, *, status: str = "staged") -> PendingUpdateExecutionV1:
    now = datetime.now(timezone.utc)
    canonical = TicketPatchInV1(title="new").canonical_patch()
    return PendingUpdateExecutionV1(
        mutation_id=mutation_id,
        user_id=7,
        originating_session_id="session",
        originating_conversation_id="conversation",
        ticket_id="T-1",
        base_version=4,
        canonical_patch_bytes=canonical.body_bytes,
        patch_bytes_hash=canonical.sha256,
        if_match_version='"4"',
        status=status,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )


def _pending(execution: PendingUpdateExecutionV1) -> PendingUpdateMutationV1:
    return PendingUpdateMutationV1(
        mutation_id=execution.mutation_id,
        user_id=execution.user_id,
        session_id=execution.originating_session_id,
        conversation_id=execution.originating_conversation_id,
        ticket_id=execution.ticket_id,
        normalized_payload_hash=execution.patch_bytes_hash,
        base_version=execution.base_version,
        expires_at=execution.expires_at,
    )


def _publish_u1(repository: PendingMutationRepository, execution: PendingUpdateExecutionV1):
    async def credential():
        return DelegatedCredential("unused")

    capability = TicketRequestCapability(
        execution.user_id,
        execution.originating_session_id,
        execution.originating_conversation_id,
        credential,
        credential,
    )
    return repository.replace_update_pending(capability, None, _pending(execution))


def test_startup_recovery_promotes_only_exact_u1_committed_staged_row(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    u1 = PendingMutationRepository(db)
    u3 = PendingUpdateExecutionRepository(db)
    exact = _execution("exact")
    orphan = _execution("orphan")
    u3.stage(exact)
    u3.stage(orphan)
    assert _publish_u1(u1, exact) is not None
    recovered, retired = u3.recover_or_retire(u1)
    assert (recovered, retired) == (1, 1)
    assert u3.get("exact").status == "active"
    assert u3.get("orphan").status == "retired"


def test_terminal_u1_pending_retires_active_execution(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    u1 = PendingMutationRepository(db)
    u3 = PendingUpdateExecutionRepository(db)
    execution = _execution("active")
    u3.stage(execution)
    assert _publish_u1(u1, execution) is not None
    assert u3.recover_or_retire(u1) == (1, 0)
    u1.invalidate(conversation_id="conversation")
    assert u3.recover_or_retire(u1) == (0, 1)
    assert u3.get("active").status == "retired"


def test_clear_history_retires_update_authority_under_service_scope(tmp_path):
    settings = make_settings(tmp_path)
    init_db(settings.db_path)
    service = AgentService(settings, model=None)
    service.update_execution_repository = PendingUpdateExecutionRepository(settings.db_path)
    execution = _execution("clear-me")
    service.update_execution_repository.stage(execution)
    assert _publish_u1(service.pending_repository, execution) is not None
    service.update_execution_repository.recover_or_retire(service.pending_repository)
    service.update_execution_repository.retire_scope(conversation_id="conversation")
    service.pending_repository.invalidate(conversation_id="conversation")
    assert service.update_execution_repository.get("clear-me").status == "retired"


def test_startup_reconciles_committed_session_generation_without_second_refresh(tmp_path):
    settings = make_settings(tmp_path)
    init_db(settings.db_path)
    service = AgentService(settings, model=None)
    repository = PendingUpdateExecutionRepository(settings.db_path)
    service.update_execution_repository = repository
    execution = _execution("refresh-recovery")
    repository.stage(execution)
    assert _publish_u1(service.pending_repository, execution) is not None
    repository.recover_or_retire(service.pending_repository)
    assert repository.reserve_refresh_once(
        execution.mutation_id, base_credential_generation=0
    ) is not None
    with open_db(settings.db_path) as conn, conn:
        conn.execute(
            """INSERT INTO sessions (
                id, user_id, username, display_name, role, role_label, team,
                permissions, conversation_id, itsm_token, itsm_token_expires_at,
                created_at, session_expires_at, itsm_credential_generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "session", 7, "user", "User", "admin", "Admin", None, "[]",
                "conversation", "opaque-token", "2099-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z", 1,
            ),
        )
    service.startup_cleanup()
    recovered = repository.get(execution.mutation_id)
    assert recovered is not None
    assert recovered.refresh_state == "committed"
    assert recovered.refresh_committed_credential_generation == 1


# Session and conversation lifecycle are shared U1 boundaries modified by U3;
# include their established regressions in the exact-file coverage contract.
from tests.test_session import *  # noqa: F403,E402,F401
from tests.test_ticket_lifecycle import *  # noqa: F403,E402,F401
