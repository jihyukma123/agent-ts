import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.db import init_db, open_db
from app.ticketing.create_models import TicketCreateAttemptV1
from app.ticketing.create_repository import TicketCreateAttemptRepository
from app.ticketing.models import ConsumedMutationEvidenceV1


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "sessions.db"
    init_db(path)
    return TicketCreateAttemptRepository(path, utc_now=lambda: NOW)


def attempt(identifier="attempt-1", *, retained_until=NOW + timedelta(hours=24)):
    body = b'{"title":"generated-test"}'
    return TicketCreateAttemptV1(
        create_attempt_id=identifier,
        user_id=7,
        originating_session_id="session-7",
        originating_conversation_id="conversation-7",
        normalized_payload_hash=hashlib.sha256(body).hexdigest(),
        canonical_request_bytes=body,
        request_bytes_hash=hashlib.sha256(body).hexdigest(),
        idempotency_key=f"key-{identifier}",
        created_at=NOW,
        retained_until=retained_until,
    )


def bind(repository, identifier="attempt-1"):
    return repository.bind_confirmation_evidence(
        identifier,
        ConsumedMutationEvidenceV1(mutation_id=f"mutation-{identifier}", confirmed_at=NOW),
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=attempt(identifier).normalized_payload_hash,
    )


def test_migration_is_additive_and_creates_only_owned_table(repository):
    with open_db(repository.db_path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        create_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(ticket_create_attempts)")
        }
    assert {"sessions", "pending_ticket_mutations", "ticket_cursor_bindings", "ticket_create_attempts"} <= names
    assert "claim_kind" not in create_columns and "claim_started_at" not in create_columns
    assert {
        "refresh_state",
        "refresh_base_credential_generation",
        "refresh_committed_credential_generation",
        "refresh_committed_at",
    } <= create_columns


def test_create_prepared_is_durable_and_idempotent_for_exact_row(repository):
    original = attempt()
    repository.create_prepared(original)
    assert repository.create_prepared(original) == original
    assert repository.get(original.create_attempt_id) == original


def test_actor_scope_allows_only_one_prepared_attempt(repository):
    repository.create_prepared(attempt())
    with pytest.raises(Exception):
        repository.create_prepared(attempt("attempt-2"))
    assert repository.supersede_prepared(
        user_id=7,
        session_id="session-7",
        conversation_id="conversation-7",
        expected_create_attempt_id="attempt-1",
    )
    repository.create_prepared(attempt("attempt-2"))


def test_confirmation_binding_is_idempotent_and_extends_retention_monotonically(repository):
    repository.create_prepared(attempt(retained_until=NOW + timedelta(hours=24)))
    confirmed = bind(repository)
    assert confirmed is not None
    assert confirmed.confirmed_mutation_id == "mutation-attempt-1"
    assert confirmed.retained_until == NOW + timedelta(hours=24)
    assert bind(repository) == confirmed


def test_binding_rejects_wrong_actor_or_payload(repository):
    repository.create_prepared(attempt())
    evidence = ConsumedMutationEvidenceV1(mutation_id="mutation", confirmed_at=NOW)
    assert repository.bind_confirmation_evidence(
        "attempt-1",
        evidence,
        user_id=8,
        session_id="session-7",
        conversation_id="conversation-7",
        normalized_payload_hash=attempt().normalized_payload_hash,
    ) is None


def test_concurrent_send_claim_has_one_winner_and_increments_once(repository):
    repository.create_prepared(attempt())
    bind(repository)

    def claim(number):
        return repository.claim_for_send(
            "attempt-1",
            claim_id=f"claim-{number}",
            claim_deadline_at=NOW + timedelta(seconds=11),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (1, 2)))
    assert sum(value is not None for value in results) == 1
    assert repository.get("attempt-1").attempt_count == 1


def test_stale_send_outcome_cannot_overwrite_winning_claim(repository):
    repository.create_prepared(attempt())
    bind(repository)
    repository.claim_for_send(
        "attempt-1", claim_id="winner", claim_deadline_at=NOW + timedelta(seconds=11)
    )
    assert not repository.record_send_outcome(
        "attempt-1", "loser", status="completed", last_outcome="created", result_ticket_id="generated-id"
    )
    assert repository.get("attempt-1").status == "in_flight"


def test_active_claim_cannot_be_taken_for_reconciliation_before_deadline(repository):
    repository.create_prepared(attempt())
    bind(repository)
    repository.claim_for_send(
        "attempt-1", claim_id="send", claim_deadline_at=NOW + timedelta(seconds=11)
    )
    assert not repository.begin_reconciliation(
        "attempt-1",
        expected_claim_id="send",
        now=NOW + timedelta(seconds=10),
    )
    assert repository.begin_reconciliation(
        "attempt-1",
        expected_claim_id="send",
        now=NOW + timedelta(seconds=11),
    )
    assert repository.get("attempt-1").claim_id == "send"


def test_reconciliation_preserves_send_claim_and_stale_result_writes_zero(repository):
    repository.create_prepared(attempt())
    bind(repository)
    repository.claim_for_send(
        "attempt-1", claim_id="send", claim_deadline_at=NOW + timedelta(seconds=11)
    )
    assert repository.record_send_outcome(
        "attempt-1", "send", status="ambiguous", last_outcome="transport_ambiguous"
    )
    assert repository.begin_reconciliation(
        "attempt-1",
        expected_claim_id="send",
        now=NOW,
    )
    assert not repository.record_reconciliation("attempt-1", "stale-send")
    assert repository.record_reconciliation("attempt-1", "send")
    settled = repository.get("attempt-1")
    assert settled.claim_id == "send" and settled.status == "retryable_after_reconcile"


def test_refresh_reservation_has_one_durable_owner(repository):
    repository.create_prepared(attempt())
    bind(repository)
    repository.claim_for_send(
        "attempt-1", claim_id="send", claim_deadline_at=NOW + timedelta(seconds=11)
    )
    repository.record_send_outcome(
        "attempt-1", "send", status="retryable_no_apply", last_outcome="token_expired_no_apply"
    )
    first = repository.reserve_refresh(
        "attempt-1", reservation_id="reserve-1", used_at=NOW, base_credential_generation=0
    )
    second = repository.reserve_refresh(
        "attempt-1", reservation_id="reserve-2", used_at=NOW, base_credential_generation=0
    )
    assert first is not None
    assert second is None
    assert first.refresh_state == "reserved"


def test_refresh_commit_is_fenced_and_enables_only_higher_generation_send(repository):
    repository.create_prepared(attempt())
    bind(repository)
    repository.claim_for_send(
        "attempt-1", claim_id="send", claim_deadline_at=NOW + timedelta(seconds=11)
    )
    repository.record_send_outcome(
        "attempt-1", "send", status="retryable_no_apply", last_outcome="token_expired_no_apply"
    )
    repository.reserve_refresh(
        "attempt-1", reservation_id="reserve-1", used_at=NOW, base_credential_generation=0
    )
    assert repository.claim_for_send(
        "attempt-1", claim_id="blocked", claim_deadline_at=NOW + timedelta(seconds=11)
    ) is None
    assert not repository.record_refresh_committed(
        "attempt-1", "wrong", committed_credential_generation=1, committed_at=NOW
    )
    assert repository.record_refresh_committed(
        "attempt-1", "reserve-1", committed_credential_generation=1, committed_at=NOW
    )
    assert repository.claim_for_send(
        "attempt-1", claim_id="send-2", claim_deadline_at=NOW + timedelta(seconds=11)
    ) is not None


def test_cleanup_keeps_row_before_and_deletes_at_exact_boundary(repository):
    repository.create_prepared(attempt())
    assert repository.cleanup_expired(now=NOW + timedelta(hours=24) - timedelta(microseconds=1)) == 0
    assert repository.cleanup_expired(now=NOW + timedelta(hours=24)) == 1


def test_restart_reads_same_bytes_key_and_claim_state(repository):
    repository.create_prepared(attempt())
    bind(repository)
    repository.claim_for_send(
        "attempt-1", claim_id="send", claim_deadline_at=NOW + timedelta(seconds=11)
    )
    restarted = TicketCreateAttemptRepository(repository.db_path, utc_now=lambda: NOW)
    observed = restarted.get("attempt-1")
    assert observed.canonical_request_bytes == attempt().canonical_request_bytes
    assert observed.idempotency_key == attempt().idempotency_key
    assert observed.claim_id == "send"


def test_completed_requires_ticket_id(repository):
    repository.create_prepared(attempt())
    bind(repository)
    repository.claim_for_send(
        "attempt-1", claim_id="send", claim_deadline_at=NOW + timedelta(seconds=11)
    )
    with pytest.raises(ValueError):
        repository.record_send_outcome("attempt-1", "send", status="completed", last_outcome="created")
