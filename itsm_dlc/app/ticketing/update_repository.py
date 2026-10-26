"""U3-owned durable ticket-update execution repository."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.db import open_db
from app.ticketing.models import UpdatePendingReplacementV1
from app.ticketing.models import PendingUpdateMutationV1
from app.ticketing.update_migration import verify_ticket_update_schema
from app.ticketing.update_models import (
    PendingUpdateExecutionV1,
    UpdateCredentialRefreshReservationV1,
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class PendingUpdateExecutionRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with open_db(db_path) as conn:
            verify_ticket_update_schema(conn)

    def stage(self, execution: PendingUpdateExecutionV1) -> str:
        if execution.status != "staged" or not execution.verify_patch_hash():
            raise ValueError("only an integrity-checked staged execution can be stored")
        with open_db(self.db_path) as conn, conn:
            conn.execute(
                """INSERT INTO pending_update_executions (
                    mutation_id, user_id, originating_session_id, originating_conversation_id,
                    ticket_id, base_version, serialization_version, canonical_patch_bytes,
                    patch_bytes_hash, if_match_version, status, created_at, expires_at,
                    retired_at, refresh_reservation_id, refresh_state, refresh_used_at,
                    refresh_base_credential_generation, refresh_committed_credential_generation,
                    refresh_committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._values(execution),
            )
        return execution.mutation_id

    def get(self, mutation_id: str) -> PendingUpdateExecutionV1 | None:
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM pending_update_executions WHERE mutation_id=?",
                (mutation_id,),
            ).fetchone()
        return self._execution(row) if row is not None else None

    def non_retired(self) -> tuple[PendingUpdateExecutionV1, ...]:
        """Return restart-recovery candidates without granting authority."""
        with open_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM pending_update_executions WHERE status!='retired' ORDER BY created_at"
            ).fetchall()
        return tuple(self._execution(row) for row in rows)

    def recover_or_retire(self, pending_repository, *, now: datetime | None = None) -> tuple[int, int]:
        """Reconcile U3 rows against U1 authority at startup/access.

        A U3 row never restores authority by itself: staged rows are promoted
        only from an exact U1 pending binding, and active/staged orphans are
        retired immediately.
        """
        now = now or datetime.now(timezone.utc)
        recovered = retired = 0
        for execution in self.non_retired():
            pending = pending_repository.get(execution.mutation_id)
            if execution.expires_at <= now or pending is None or pending.status != "pending":
                retired += int(self.retire(execution.mutation_id, now=now))
                continue
            try:
                update_pending = PendingUpdateMutationV1.model_validate(
                    pending.model_dump(mode="python")
                )
            except ValueError:
                retired += int(self.retire(execution.mutation_id, now=now))
                continue
            if execution.status == "staged":
                if self.recover_staged_from_pending(execution, update_pending):
                    recovered += 1
                else:
                    retired += int(self.retire(execution.mutation_id, now=now))
            elif not self._binding_matches(execution, update_pending):
                retired += int(self.retire(execution.mutation_id, now=now))
        return recovered, retired

    def activate_replacement(self, evidence: UpdatePendingReplacementV1) -> bool:
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            new = conn.execute(
                "SELECT * FROM pending_update_executions WHERE mutation_id=?",
                (evidence.new_mutation_id,),
            ).fetchone()
            if new is None or not self._matches_evidence(new, evidence):
                conn.rollback()
                return False
            actor = (evidence.user_id, evidence.session_id, evidence.conversation_id)
            if evidence.old_mutation_id is None:
                active = conn.execute(
                    """SELECT 1 FROM pending_update_executions
                       WHERE user_id=? AND originating_session_id=? AND originating_conversation_id=?
                         AND status='active' LIMIT 1""",
                    actor,
                ).fetchone()
                if active is not None:
                    conn.rollback()
                    return False
            else:
                retired = conn.execute(
                    """UPDATE pending_update_executions SET status='retired', retired_at=?
                       WHERE mutation_id=? AND user_id=? AND originating_session_id=?
                         AND originating_conversation_id=? AND status='active'""",
                    (_iso(datetime.now(timezone.utc)), evidence.old_mutation_id, *actor),
                ).rowcount
                if retired != 1:
                    conn.rollback()
                    return False
            promoted = conn.execute(
                """UPDATE pending_update_executions SET status='active'
                   WHERE mutation_id=? AND status='staged'""",
                (evidence.new_mutation_id,),
            ).rowcount
            if promoted != 1:
                conn.rollback()
                return False
            try:
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
        return True

    def find_active_for_actor_ticket(
        self,
        *,
        user_id: int,
        session_id: str,
        conversation_id: str,
        ticket_id: str,
        now: datetime | None = None,
    ) -> PendingUpdateExecutionV1 | None:
        now = now or datetime.now(timezone.utc)
        with open_db(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM pending_update_executions
                   WHERE user_id=? AND originating_session_id=? AND originating_conversation_id=?
                     AND ticket_id=? AND status='active' AND expires_at>?""",
                (user_id, session_id, conversation_id, ticket_id, _iso(now)),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("active update locator cardinality violation")
        return self._execution(rows[0]) if rows else None

    def find_staged_for_actor_ticket(
        self,
        *,
        user_id: int,
        session_id: str,
        conversation_id: str,
        ticket_id: str,
    ) -> tuple[PendingUpdateExecutionV1, ...]:
        """Return non-authorizing staged candidates for access reconciliation."""
        with open_db(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM pending_update_executions
                   WHERE user_id=? AND originating_session_id=?
                     AND originating_conversation_id=? AND ticket_id=?
                     AND status='staged' ORDER BY created_at, mutation_id""",
                (user_id, session_id, conversation_id, ticket_id),
            ).fetchall()
        return tuple(self._execution(row) for row in rows)

    def recover_staged_from_pending(
        self,
        execution: PendingUpdateExecutionV1,
        pending: PendingUpdateMutationV1,
    ) -> bool:
        """Finish the U1-committed/U3-not-activated crash window exactly."""
        if not all(
            (
                execution.status == "staged",
                pending.status == "pending",
                execution.mutation_id == pending.mutation_id,
                execution.user_id == pending.user_id,
                secrets.compare_digest(execution.originating_session_id, pending.session_id),
                secrets.compare_digest(execution.originating_conversation_id, pending.conversation_id),
                execution.ticket_id == pending.ticket_id,
                secrets.compare_digest(execution.patch_bytes_hash, pending.normalized_payload_hash),
                execution.base_version == pending.base_version,
                execution.verify_patch_hash(),
            )
        ):
            return False
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_rows = conn.execute(
                """SELECT mutation_id FROM pending_update_executions
                   WHERE user_id=? AND originating_session_id=? AND originating_conversation_id=?
                     AND status='active'""",
                (
                    execution.user_id,
                    execution.originating_session_id,
                    execution.originating_conversation_id,
                ),
            ).fetchall()
            if len(active_rows) > 1:
                conn.rollback()
                return False
            if active_rows:
                retired = conn.execute(
                    """UPDATE pending_update_executions SET status='retired', retired_at=?
                       WHERE mutation_id=? AND status='active'""",
                    (_iso(datetime.now(timezone.utc)), active_rows[0]["mutation_id"]),
                ).rowcount
                if retired != 1:
                    conn.rollback()
                    return False
            promoted = conn.execute(
                "UPDATE pending_update_executions SET status='active' WHERE mutation_id=? AND status='staged'",
                (execution.mutation_id,),
            ).rowcount
            if promoted != 1:
                conn.rollback()
                return False
            try:
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
        return True

    def retire(self, mutation_id: str, *, expected_status: str | None = None, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        status_filter = " AND status=?" if expected_status is not None else " AND status!='retired'"
        values: tuple[object, ...] = (mutation_id, expected_status) if expected_status is not None else (mutation_id,)
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                f"UPDATE pending_update_executions SET status='retired', retired_at=? WHERE mutation_id=?{status_filter}",
                (_iso(now), *values),
            ).rowcount
        return changed == 1

    def retire_scope(
        self,
        *,
        user_id: int | None = None,
        session_id: str | None = None,
        conversation_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        filters: list[str] = []
        values: list[object] = []
        for column, value in (
            ("user_id", user_id),
            ("originating_session_id", session_id),
            ("originating_conversation_id", conversation_id),
        ):
            if value is not None:
                filters.append(f"{column}=?")
                values.append(value)
        if not filters:
            raise ValueError("at least one lifecycle selector is required")
        with open_db(self.db_path) as conn, conn:
            return conn.execute(
                f"""UPDATE pending_update_executions SET status='retired', retired_at=?
                    WHERE {' AND '.join(filters)} AND status!='retired'""",
                (_iso(now or datetime.now(timezone.utc)), *values),
            ).rowcount

    def cleanup(self, *, now: datetime, retention_cutoff: datetime) -> int:
        with open_db(self.db_path) as conn, conn:
            retired = conn.execute(
                """UPDATE pending_update_executions SET status='retired', retired_at=?
                   WHERE status IN ('staged','active') AND expires_at<=?""",
                (_iso(now), _iso(now)),
            ).rowcount
            deleted = conn.execute(
                "DELETE FROM pending_update_executions WHERE status='retired' AND retired_at<=?",
                (_iso(retention_cutoff),),
            ).rowcount
        return retired + deleted

    def reserve_refresh_once(
        self,
        mutation_id: str,
        *,
        base_credential_generation: int,
        now: datetime | None = None,
    ) -> UpdateCredentialRefreshReservationV1 | None:
        now = now or datetime.now(timezone.utc)
        reservation_id = f"update.{secrets.token_urlsafe(24)}"
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE pending_update_executions
                   SET refresh_reservation_id=?, refresh_state='reserved', refresh_used_at=?,
                       refresh_base_credential_generation=?
                   WHERE mutation_id=? AND status='active' AND refresh_state IS NULL""",
                (reservation_id, _iso(now), base_credential_generation, mutation_id),
            ).rowcount
        if changed != 1:
            return None
        return UpdateCredentialRefreshReservationV1(
            mutation_id=mutation_id,
            refresh_reservation_id=reservation_id,
            refresh_used_at=now,
            base_credential_generation=base_credential_generation,
            refresh_state="reserved",
        )

    def mark_refresh_committed(
        self,
        mutation_id: str,
        *,
        committed_generation: int,
        now: datetime | None = None,
    ) -> bool:
        with open_db(self.db_path) as conn, conn:
            changed = conn.execute(
                """UPDATE pending_update_executions
                   SET refresh_state='committed', refresh_committed_credential_generation=?,
                       refresh_committed_at=?
                   WHERE mutation_id=? AND refresh_state='reserved'
                     AND refresh_base_credential_generation<?""",
                (committed_generation, _iso(now or datetime.now(timezone.utc)), mutation_id, committed_generation),
            ).rowcount
        return changed == 1

    def reconcile_refresh_after_restart(
        self,
        mutation_id: str,
        *,
        current_session_credential_generation: int,
        now: datetime | None = None,
    ) -> Literal["committed", "failed"] | None:
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT refresh_state, refresh_base_credential_generation
                   FROM pending_update_executions WHERE mutation_id=?""",
                (mutation_id,),
            ).fetchone()
            if row is None or row["refresh_state"] != "reserved":
                conn.rollback()
                return None
            base = row["refresh_base_credential_generation"]
            outcome = "committed" if isinstance(base, int) and current_session_credential_generation > base else "failed"
            conn.execute(
                """UPDATE pending_update_executions
                   SET refresh_state=?, refresh_committed_credential_generation=?, refresh_committed_at=?
                   WHERE mutation_id=? AND refresh_state='reserved'""",
                (
                    outcome,
                    current_session_credential_generation if outcome == "committed" else None,
                    _iso(now or datetime.now(timezone.utc)) if outcome == "committed" else None,
                    mutation_id,
                ),
            )
            conn.commit()
        return outcome

    @staticmethod
    def _matches_evidence(row: sqlite3.Row, evidence: UpdatePendingReplacementV1) -> bool:
        return all(
            (
                row["status"] == "staged",
                row["user_id"] == evidence.user_id,
                secrets.compare_digest(row["originating_session_id"], evidence.session_id),
                secrets.compare_digest(row["originating_conversation_id"], evidence.conversation_id),
                row["ticket_id"] == evidence.new_ticket_id,
                secrets.compare_digest(row["patch_bytes_hash"], evidence.new_normalized_payload_hash),
                row["base_version"] == evidence.new_base_version,
                hashlib.sha256(row["canonical_patch_bytes"]).hexdigest() == row["patch_bytes_hash"],
            )
        )

    @staticmethod
    def _binding_matches(
        execution: PendingUpdateExecutionV1, pending: PendingUpdateMutationV1
    ) -> bool:
        return all(
            (
                execution.mutation_id == pending.mutation_id,
                execution.user_id == pending.user_id,
                secrets.compare_digest(execution.originating_session_id, pending.session_id),
                secrets.compare_digest(
                    execution.originating_conversation_id, pending.conversation_id
                ),
                execution.ticket_id == pending.ticket_id,
                secrets.compare_digest(
                    execution.patch_bytes_hash, pending.normalized_payload_hash
                ),
                execution.base_version == pending.base_version,
                execution.verify_patch_hash(),
            )
        )

    @staticmethod
    def _values(execution: PendingUpdateExecutionV1) -> tuple[object, ...]:
        return (
            execution.mutation_id,
            execution.user_id,
            execution.originating_session_id,
            execution.originating_conversation_id,
            execution.ticket_id,
            execution.base_version,
            execution.serialization_version,
            execution.canonical_patch_bytes,
            execution.patch_bytes_hash,
            execution.if_match_version,
            execution.status,
            _iso(execution.created_at),
            _iso(execution.expires_at),
            _iso(execution.retired_at) if execution.retired_at else None,
            execution.refresh_reservation_id,
            execution.refresh_state,
            _iso(execution.refresh_used_at) if execution.refresh_used_at else None,
            execution.refresh_base_credential_generation,
            execution.refresh_committed_credential_generation,
            _iso(execution.refresh_committed_at) if execution.refresh_committed_at else None,
        )

    @staticmethod
    def _execution(row: sqlite3.Row) -> PendingUpdateExecutionV1:
        data = dict(row)
        for name in ("created_at", "expires_at", "retired_at", "refresh_used_at", "refresh_committed_at"):
            data[name] = _parse(data[name])
        return PendingUpdateExecutionV1.model_validate(data)
