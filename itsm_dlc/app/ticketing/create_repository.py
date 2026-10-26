"""Durable U2 create ledger with affected-row compare-and-set transitions."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import SCHEMA, migrate_ticket_create_attempts, open_db
from app.ticketing.create_models import (
    CreateSendClaimV1,
    TicketCreateAttemptV1,
)
from app.ticketing.models import ConsumedMutationEvidenceV1


RETENTION = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class TicketCreateAttemptRepository:
    def __init__(self, db_path: Path, *, utc_now: Callable[[], datetime] = _utc_now):
        self.db_path = db_path
        self._utc_now = utc_now
        with open_db(db_path) as conn, conn:
            conn.executescript(SCHEMA)
            migrate_ticket_create_attempts(conn)

    def create_prepared(self, attempt: TicketCreateAttemptV1) -> TicketCreateAttemptV1:
        if attempt.status != "prepared" or attempt.confirmed_mutation_id is not None:
            raise ValueError("new create attempt must be unconfirmed prepared state")
        if attempt.created_at is None or attempt.retained_until is None:
            raise ValueError("created_at and retained_until are required")
        if attempt.retained_until < attempt.created_at + RETENTION:
            raise ValueError("initial retention must be at least 24 hours")
        values = (
            attempt.create_attempt_id,
            attempt.user_id,
            attempt.originating_session_id,
            attempt.originating_conversation_id,
            attempt.normalized_payload_hash,
            attempt.canonical_request_bytes,
            attempt.serialization_version,
            attempt.request_bytes_hash,
            attempt.idempotency_key,
            attempt.attempt_count,
            attempt.status,
            _iso(attempt.created_at),
            _iso(attempt.retained_until),
        )
        try:
            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    """INSERT INTO ticket_create_attempts
                       (create_attempt_id,user_id,originating_session_id,originating_conversation_id,
                        normalized_payload_hash,canonical_request_bytes,serialization_version,
                        request_bytes_hash,idempotency_key,attempt_count,status,created_at,retained_until)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
        except sqlite3.IntegrityError:
            existing = self.get(attempt.create_attempt_id)
            if existing == attempt:
                return existing
            raise
        return attempt

    def get(self, create_attempt_id: str) -> TicketCreateAttemptV1 | None:
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM ticket_create_attempts WHERE create_attempt_id = ?", (create_attempt_id,)
            ).fetchone()
        return self._attempt(row) if row is not None else None

    def find_prepared_for_binding(
        self, *, user_id: int, session_id: str, conversation_id: str, normalized_payload_hash: str
    ) -> TicketCreateAttemptV1 | None:
        with open_db(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM ticket_create_attempts
                   WHERE user_id=? AND originating_session_id=? AND originating_conversation_id=?
                     AND normalized_payload_hash=? AND status='prepared'""",
                (user_id, session_id, conversation_id, normalized_payload_hash),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("multiple prepared create attempts violate actor scope")
        return self._attempt(rows[0]) if rows else None

    def find_prepared_for_actor(
        self, *, user_id: int, session_id: str, conversation_id: str
    ) -> TicketCreateAttemptV1 | None:
        with open_db(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM ticket_create_attempts
                   WHERE user_id=? AND originating_session_id=? AND originating_conversation_id=?
                     AND status='prepared'""",
                (user_id, session_id, conversation_id),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("multiple prepared create attempts violate actor scope")
        return self._attempt(rows[0]) if rows else None

    def find_for_consumed_pending(
        self,
        *,
        user_id: int,
        session_id: str,
        conversation_id: str,
        normalized_payload_hash: str,
        mutation_id: str,
    ) -> TicketCreateAttemptV1 | None:
        """Find the one durable attempt recoverable from a consumed U1 review.

        An evidence-free prepared row represents a consume-before-bind crash and
        may only be republished. A retryable row with the same bound mutation
        and a durable refresh reservation may resume its remaining send budget.
        """
        with open_db(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM ticket_create_attempts
                   WHERE user_id=? AND originating_session_id=? AND originating_conversation_id=?
                     AND normalized_payload_hash=?
                     AND (
                       (status='prepared' AND confirmed_mutation_id IS NULL AND confirmed_at IS NULL)
                       OR
                       (status='retryable_no_apply' AND confirmed_mutation_id=?
                         AND confirmed_at IS NOT NULL
                         AND refresh_reservation_id IS NOT NULL AND refresh_used_at IS NOT NULL
                         AND refresh_state IN ('reserved','committed'))
                     )""",
                (user_id, session_id, conversation_id, normalized_payload_hash, mutation_id),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("multiple recoverable create attempts violate actor scope")
        return self._attempt(rows[0]) if rows else None

    def supersede_prepared(
        self,
        *,
        user_id: int,
        session_id: str,
        conversation_id: str,
        expected_create_attempt_id: str,
        reason: str = "review_superseded",
    ) -> bool:
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET status='terminal_failure', last_outcome=?
                   WHERE create_attempt_id=? AND user_id=? AND originating_session_id=?
                     AND originating_conversation_id=? AND status='prepared'
                     AND confirmed_mutation_id IS NULL AND confirmed_at IS NULL""",
                (reason, expected_create_attempt_id, user_id, session_id, conversation_id),
            ).rowcount
        return changed == 1

    def bind_confirmation_evidence(
        self,
        create_attempt_id: str,
        evidence: ConsumedMutationEvidenceV1,
        *,
        user_id: int,
        session_id: str,
        conversation_id: str,
        normalized_payload_hash: str,
    ) -> TicketCreateAttemptV1 | None:
        retained_until = evidence.confirmed_at + RETENTION
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM ticket_create_attempts WHERE create_attempt_id=?", (create_attempt_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            current = self._attempt(row)
            actor_matches = (
                current.user_id == user_id
                and current.originating_session_id == session_id
                and current.originating_conversation_id == conversation_id
                and current.normalized_payload_hash == normalized_payload_hash
            )
            if not actor_matches or current.status != "prepared":
                conn.rollback()
                return None
            if current.confirmed_mutation_id is not None:
                conn.rollback()
                return current if (
                    current.confirmed_mutation_id == evidence.mutation_id
                    and current.confirmed_at == evidence.confirmed_at
                ) else None
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET confirmed_mutation_id=?, confirmed_at=?,
                       retained_until=CASE WHEN retained_until < ? THEN ? ELSE retained_until END
                   WHERE create_attempt_id=? AND status='prepared'
                     AND confirmed_mutation_id IS NULL AND confirmed_at IS NULL""",
                (
                    evidence.mutation_id,
                    _iso(evidence.confirmed_at),
                    _iso(retained_until),
                    _iso(retained_until),
                    create_attempt_id,
                ),
            ).rowcount
            conn.commit()
        return self.get(create_attempt_id) if changed == 1 else None

    def claim_for_send(
        self,
        create_attempt_id: str,
        *,
        claim_id: str,
        claim_deadline_at: datetime,
    ) -> CreateSendClaimV1 | None:
        if claim_deadline_at <= self._utc_now():
            raise ValueError("claim deadline must be in the future")
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET status='in_flight', attempt_count=attempt_count+1, claim_id=?,
                       claim_deadline_at=?
                   WHERE create_attempt_id=?
                     AND status IN ('prepared','retryable_after_reconcile','retryable_no_apply')
                     AND confirmed_mutation_id IS NOT NULL AND confirmed_at IS NOT NULL
                     AND attempt_count < 2
                     AND refresh_state NOT IN ('reserved','failed')
                     AND (status != 'retryable_no_apply' OR refresh_state='committed')""",
                (claim_id, _iso(claim_deadline_at), create_attempt_id),
            ).rowcount
        attempt = self.get(create_attempt_id)
        if changed != 1 or attempt is None:
            return None
        return CreateSendClaimV1(attempt=attempt, claim_id=claim_id, deadline_at=claim_deadline_at)

    def record_send_outcome(
        self,
        create_attempt_id: str,
        claim_id: str,
        *,
        status: str,
        last_outcome: str,
        result_ticket_id: str | None = None,
    ) -> bool:
        if status not in ("completed", "retryable_no_apply", "terminal_failure", "ambiguous"):
            raise ValueError("unsupported send outcome")
        if (status == "completed") != bool(result_ticket_id):
            raise ValueError("completed requires a ticket ID and other states forbid one")
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET status=?, last_outcome=?, result_ticket_id=?
                   WHERE create_attempt_id=? AND status='in_flight'
                     AND claim_id=?""",
                (status, last_outcome, result_ticket_id, create_attempt_id, claim_id),
            ).rowcount
        return changed == 1

    def begin_reconciliation(
        self,
        create_attempt_id: str,
        *,
        expected_claim_id: str,
        now: datetime,
    ) -> bool:
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET last_outcome=last_outcome
                   WHERE create_attempt_id=? AND status IN ('in_flight','ambiguous')
                     AND claim_id=? AND (
                       status='ambiguous'
                       OR (status='in_flight' AND claim_deadline_at <= ?)
                     )""",
                (create_attempt_id, expected_claim_id, _iso(now)),
            ).rowcount
        return changed == 1

    def record_reconciliation(
        self,
        create_attempt_id: str,
        expected_claim_id: str,
        *,
        ticket_id: str | None = None,
        outcome: str = "lookup_unavailable",
    ) -> bool:
        current = self.get(create_attempt_id)
        if current is None or current.claim_id != expected_claim_id:
            return False
        if ticket_id:
            status = "completed"
        elif current.attempt_count < 2:
            status = "retryable_after_reconcile"
        else:
            status = "ambiguous"
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET status=?, result_ticket_id=?, last_outcome=?
                   WHERE create_attempt_id=? AND status IN ('in_flight','ambiguous') AND claim_id=?""",
                (status, ticket_id, outcome, create_attempt_id, expected_claim_id),
            ).rowcount
        return changed == 1

    def reserve_refresh(
        self,
        create_attempt_id: str,
        *,
        reservation_id: str,
        used_at: datetime,
        base_credential_generation: int,
    ) -> TicketCreateAttemptV1 | None:
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET refresh_reservation_id=?, refresh_used_at=?,
                       refresh_base_credential_generation=?, refresh_state='reserved'
                   WHERE create_attempt_id=? AND status='retryable_no_apply'
                     AND refresh_reservation_id IS NULL AND refresh_used_at IS NULL
                     AND refresh_state='unused'
                     AND attempt_count < 2""",
                (
                    reservation_id,
                    _iso(used_at),
                    base_credential_generation,
                    create_attempt_id,
                ),
            ).rowcount
        if changed != 1:
            return None
        return self.get(create_attempt_id)

    def record_refresh_committed(
        self,
        create_attempt_id: str,
        reservation_id: str,
        *,
        committed_credential_generation: int,
        committed_at: datetime,
    ) -> bool:
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET refresh_state='committed', refresh_committed_credential_generation=?,
                       refresh_committed_at=?
                   WHERE create_attempt_id=? AND refresh_state='reserved'
                     AND refresh_reservation_id=?
                     AND refresh_base_credential_generation < ?""",
                (
                    committed_credential_generation,
                    _iso(committed_at),
                    create_attempt_id,
                    reservation_id,
                    committed_credential_generation,
                ),
            ).rowcount
        return changed == 1

    def record_refresh_failed(
        self, create_attempt_id: str, reservation_id: str, *, outcome: str
    ) -> bool:
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts
                   SET refresh_state='failed', status='terminal_failure', last_outcome=?
                   WHERE create_attempt_id=? AND refresh_state='reserved'
                     AND refresh_reservation_id=?""",
                (outcome, create_attempt_id, reservation_id),
            ).rowcount
        return changed == 1

    def terminate_unconfirmed(self, create_attempt_id: str, *, reason: str) -> bool:
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE ticket_create_attempts SET status='terminal_failure', last_outcome=?
                   WHERE create_attempt_id=? AND status='prepared'
                     AND confirmed_mutation_id IS NULL AND confirmed_at IS NULL""",
                (reason, create_attempt_id),
            ).rowcount
        return changed == 1

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        boundary = now or self._utc_now()
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """DELETE FROM ticket_create_attempts
                   WHERE retained_until <= ? AND status != 'in_flight'""",
                (_iso(boundary),),
            ).rowcount
        return changed

    @staticmethod
    def _attempt(row: sqlite3.Row) -> TicketCreateAttemptV1:
        data = dict(row)
        data.pop("claim_kind", None)
        data.pop("claim_started_at", None)
        data["canonical_request_bytes"] = bytes(data["canonical_request_bytes"])
        for name in (
            "confirmed_at",
            "claim_deadline_at",
            "refresh_used_at",
            "refresh_committed_at",
            "created_at",
            "retained_until",
        ):
            data[name] = _parse(data[name])
        return TicketCreateAttemptV1(**data)
