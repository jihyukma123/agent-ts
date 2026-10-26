"""U1이 소유하는 cursor, pending mutation, presentation SQLite 저장소."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

from app.db import open_db
from app.ticketing.capability import TicketRequestCapability
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import (
    CanonicalTicketQueryV1,
    ConsumedMutationEvidenceV1,
    PendingCreateMutationV1,
    PendingMutationBindingV1,
    PendingTicketMutationV1,
    PendingUpdateMutationV1,
    TicketPresentationV1,
    UpdatePendingReplacementV1,
)
from app.ticketing.update_models import UpdateCredentialRefreshReservationV1


STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_ticket_mutations (
    mutation_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'update')),
    user_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    normalized_payload_hash TEXT NOT NULL,
    ticket_id TEXT,
    base_version INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending','confirmed','cancelled','invalidated','expired','consumed')),
    confirmed_at TEXT,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_ticket_mutations_scope
ON pending_ticket_mutations(user_id, session_id, conversation_id, operation, status);

CREATE TABLE IF NOT EXISTS ticket_cursor_bindings (
    continuation_ref TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    query_serialization_version INTEGER NOT NULL,
    canonical_normalized_query_json TEXT NOT NULL,
    normalized_query_hash TEXT NOT NULL,
    upstream_cursor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_cursor_bindings_scope
ON ticket_cursor_bindings(user_id, session_id, conversation_id);
"""

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_message_presentations (
    conversation_id TEXT NOT NULL,
    assistant_sequence INTEGER NOT NULL,
    assistant_text_sha256 TEXT NOT NULL,
    presentation_version INTEGER NOT NULL CHECK (presentation_version = 1),
    canonical_presentation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, assistant_sequence)
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PendingMutationRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with open_db(db_path) as conn, conn:
            conn.executescript(STATE_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_ticket_mutations)")}
            if "confirmed_at" not in columns:
                conn.execute("ALTER TABLE pending_ticket_mutations ADD COLUMN confirmed_at TEXT")

    def save_review(self, mutation: PendingCreateMutationV1) -> None:
        """Publish a create review; update reviews must use replace_update_pending."""
        if mutation.operation != "create" or mutation.ticket_id is not None or mutation.base_version is not None:
            raise ValueError("save_review accepts create mutations only")
        with open_db(self.db_path) as conn, conn:
            conn.execute(
                """
                UPDATE pending_ticket_mutations SET status = 'invalidated'
                WHERE user_id = ? AND session_id = ? AND conversation_id = ? AND operation = ?
                  AND status IN ('pending', 'confirmed')
                """,
                (mutation.user_id, mutation.session_id, mutation.conversation_id, mutation.operation),
            )
            conn.execute(
                """
                INSERT INTO pending_ticket_mutations
                (mutation_id, operation, user_id, session_id, conversation_id, normalized_payload_hash,
                 ticket_id, base_version, status, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mutation.mutation_id,
                    mutation.operation,
                    mutation.user_id,
                    mutation.session_id,
                    mutation.conversation_id,
                    mutation.normalized_payload_hash,
                    mutation.ticket_id,
                    mutation.base_version,
                    mutation.status,
                    _iso(mutation.expires_at),
                ),
            )

    def replace_update_pending(
        self,
        capability: TicketRequestCapability,
        expected_old_mutation_id: str | None,
        mutation: PendingUpdateMutationV1,
    ) -> UpdatePendingReplacementV1 | None:
        """Publish one update review without weakening a confirmed review.

        Prepare requires no current pending update. Revise requires the exact
        pending row named by the caller. The old-row transition and insertion
        share one immediate transaction, so an insertion or validation failure
        leaves both the old and proposed state unchanged.
        """
        actor = (capability.user_id, capability.session_id, capability.conversation_id)
        if (
            mutation.status != "pending"
            or mutation.expires_at <= _utc_now()
            or (mutation.user_id, mutation.session_id, mutation.conversation_id) != actor
        ):
            return None

        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _iso(_utc_now())
            confirmed = conn.execute(
                """SELECT 1 FROM pending_ticket_mutations
                   WHERE user_id=? AND session_id=? AND conversation_id=?
                     AND operation='update' AND status='confirmed' AND expires_at>?
                   LIMIT 1""",
                (*actor, now),
            ).fetchone()
            if confirmed is not None:
                conn.rollback()
                return None

            replaced_id: str | None = None
            if expected_old_mutation_id is None:
                pending = conn.execute(
                    """SELECT 1 FROM pending_ticket_mutations
                       WHERE user_id=? AND session_id=? AND conversation_id=?
                         AND operation='update' AND status='pending' AND expires_at>?
                       LIMIT 1""",
                    (*actor, now),
                ).fetchone()
                if pending is not None:
                    conn.rollback()
                    return None
            else:
                old = conn.execute(
                    """SELECT mutation_id FROM pending_ticket_mutations
                       WHERE mutation_id=? AND user_id=? AND session_id=? AND conversation_id=?
                         AND operation='update' AND status='pending' AND expires_at>?""",
                    (expected_old_mutation_id, *actor, now),
                ).fetchone()
                if old is None:
                    conn.rollback()
                    return None
                replaced_id = old["mutation_id"]

            try:
                if replaced_id is not None:
                    changed = conn.execute(
                        """UPDATE pending_ticket_mutations SET status='invalidated'
                           WHERE mutation_id=? AND status='pending'""",
                        (replaced_id,),
                    ).rowcount
                    if changed != 1:
                        conn.rollback()
                        return None
                conn.execute(
                    """INSERT INTO pending_ticket_mutations
                       (mutation_id, operation, user_id, session_id, conversation_id,
                        normalized_payload_hash, ticket_id, base_version, status, expires_at)
                       VALUES (?, 'update', ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        mutation.mutation_id,
                        mutation.user_id,
                        mutation.session_id,
                        mutation.conversation_id,
                        mutation.normalized_payload_hash,
                        mutation.ticket_id,
                        mutation.base_version,
                        _iso(mutation.expires_at),
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return None
            conn.commit()

        return UpdatePendingReplacementV1(
            old_mutation_id=replaced_id,
            new_mutation_id=mutation.mutation_id,
            user_id=mutation.user_id,
            session_id=mutation.session_id,
            conversation_id=mutation.conversation_id,
            new_ticket_id=mutation.ticket_id,
            new_normalized_payload_hash=mutation.normalized_payload_hash,
            new_base_version=mutation.base_version,
            affected_old_rows=1 if replaced_id is not None else 0,
        )

    def get(self, mutation_id: str) -> PendingTicketMutationV1 | None:
        with open_db(self.db_path) as conn, conn:
            row = conn.execute("SELECT * FROM pending_ticket_mutations WHERE mutation_id = ?", (mutation_id,)).fetchone()
        if row is None:
            return None
        mutation = self._mutation(row)
        if mutation.status in ("pending", "confirmed") and mutation.expires_at <= _utc_now():
            with open_db(self.db_path) as conn, conn:
                conn.execute(
                    "UPDATE pending_ticket_mutations SET status = 'expired' WHERE mutation_id = ? AND status IN ('pending','confirmed')",
                    (mutation_id,),
                )
            return mutation.model_copy(update={"status": "expired"})
        return mutation

    def current_update_pending(
        self, capability: TicketRequestCapability, ticket_id: str
    ) -> PendingUpdateMutationV1 | None:
        """Return the exact actor/ticket U1 pending authority for access recovery."""
        with open_db(self.db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM pending_ticket_mutations
                   WHERE operation='update' AND status='pending'
                     AND user_id=? AND session_id=? AND conversation_id=? AND ticket_id=?""",
                (
                    capability.user_id,
                    capability.session_id,
                    capability.conversation_id,
                    ticket_id,
                ),
            ).fetchall()
        if len(rows) != 1:
            return None
        mutation = self._mutation(rows[0])
        if mutation.expires_at <= _utc_now():
            self.get(mutation.mutation_id)
            return None
        try:
            return PendingUpdateMutationV1.model_validate(mutation.model_dump(mode="python"))
        except ValueError:
            return None

    def verify_update_refresh_reservation(
        self,
        reservation: UpdateCredentialRefreshReservationV1,
        *,
        user_id: int,
        session_id: str,
        conversation_id: str,
        credential_generation: int,
    ) -> bool:
        """Atomically authenticate consumed U1 and active U3 durable authority."""
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT e.*, p.operation AS pending_operation,
                          p.user_id AS pending_user_id,
                          p.session_id AS pending_session_id,
                          p.conversation_id AS pending_conversation_id,
                          p.normalized_payload_hash AS pending_payload_hash,
                          p.ticket_id AS pending_ticket_id,
                          p.base_version AS pending_base_version,
                          p.status AS pending_status
                   FROM pending_ticket_mutations p
                   LEFT JOIN pending_update_executions e ON e.mutation_id=p.mutation_id
                   WHERE p.mutation_id=?""",
                (reservation.mutation_id,),
            ).fetchone()
            required_text = (
                "refresh_reservation_id",
                "originating_session_id",
                "pending_session_id",
                "originating_conversation_id",
                "pending_conversation_id",
                "patch_bytes_hash",
                "pending_payload_hash",
            )
            valid = (
                row is not None
                and all(isinstance(row[name], str) for name in required_text)
                and all(
                    (
                        row["status"] == "active",
                        row["refresh_state"] == "reserved",
                        isinstance(row["refresh_reservation_id"], str),
                        secrets.compare_digest(
                            row["refresh_reservation_id"], reservation.refresh_reservation_id
                        ),
                        row["refresh_base_credential_generation"] == credential_generation,
                        reservation.base_credential_generation == credential_generation,
                        row["user_id"] == user_id == row["pending_user_id"],
                        secrets.compare_digest(row["originating_session_id"], session_id),
                        secrets.compare_digest(row["pending_session_id"] or "", session_id),
                        secrets.compare_digest(
                            row["originating_conversation_id"], conversation_id
                        ),
                        secrets.compare_digest(
                            row["pending_conversation_id"] or "", conversation_id
                        ),
                        row["pending_operation"] == "update",
                        row["pending_status"] == "consumed",
                        row["ticket_id"] == row["pending_ticket_id"],
                        row["base_version"] == row["pending_base_version"],
                        secrets.compare_digest(
                            row["patch_bytes_hash"], row["pending_payload_hash"] or ""
                        ),
                    )
                )
            )
            conn.rollback()
        return bool(valid)

    def confirm(
        self, binding: PendingMutationBindingV1, evidence: CurrentTurnEvidenceV1
    ) -> PendingTicketMutationV1 | None:
        """현재 raw turn의 독립 확인과 exact binding을 한 transaction에서 검사한다."""
        if evidence.decision != "explicit_confirm" or (
            evidence.user_id,
            evidence.session_id,
            evidence.conversation_id,
        ) != (binding.user_id, binding.session_id, binding.conversation_id):
            return None
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pending_ticket_mutations WHERE mutation_id = ?", (binding.mutation_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            mutation = self._mutation(row)
            if mutation.expires_at <= _utc_now() and mutation.status in ("pending", "confirmed"):
                conn.execute(
                    "UPDATE pending_ticket_mutations SET status = 'expired' WHERE mutation_id = ? AND status IN ('pending','confirmed')",
                    (binding.mutation_id,),
                )
                conn.commit()
                return None
            if mutation.status != "pending" or not self._matches(mutation, binding):
                conn.rollback()
                return None
            confirmed_at = _utc_now()
            changed = conn.execute(
                """UPDATE pending_ticket_mutations
                   SET status = 'confirmed', confirmed_at = ?
                   WHERE mutation_id = ? AND status = 'pending'""",
                (_iso(confirmed_at), binding.mutation_id),
            ).rowcount
            conn.commit()
        return mutation.model_copy(update={"status": "confirmed"}) if changed == 1 else None

    def consume_once(self, binding: PendingMutationBindingV1) -> ConsumedMutationEvidenceV1 | None:
        """exact confirmed binding을 mutation transport 전에 한 번만 소비한다."""
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pending_ticket_mutations WHERE mutation_id = ?", (binding.mutation_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            mutation = self._mutation(row)
            if mutation.expires_at <= _utc_now() and mutation.status in ("pending", "confirmed"):
                conn.execute(
                    "UPDATE pending_ticket_mutations SET status = 'expired' WHERE mutation_id = ? AND status IN ('pending','confirmed')",
                    (binding.mutation_id,),
                )
                conn.commit()
                return None
            if mutation.status != "confirmed" or not self._matches(mutation, binding):
                conn.rollback()
                return None
            confirmed_at_raw = row["confirmed_at"]
            if not isinstance(confirmed_at_raw, str) or not confirmed_at_raw:
                conn.rollback()
                return None
            changed = conn.execute(
                "UPDATE pending_ticket_mutations SET status = 'consumed' WHERE mutation_id = ? AND status = 'confirmed'",
                (binding.mutation_id,),
            ).rowcount
            conn.commit()
        if changed != 1:
            return None
        return ConsumedMutationEvidenceV1(
            mutation_id=binding.mutation_id,
            confirmed_at=_parse(confirmed_at_raw),
        )

    def confirm_and_consume_update(
        self,
        binding: PendingMutationBindingV1,
        evidence: CurrentTurnEvidenceV1,
    ) -> ConsumedMutationEvidenceV1 | None:
        """Atomically validate, confirm and consume one update mutation.

        The ``confirmed`` value exists only within this transaction. No caller
        can observe or reuse it after commit, which is the U3 C-02 authority
        boundary. The U2 create-specific two-call API remains unchanged.
        """
        if binding.operation != "update" or evidence.decision != "explicit_confirm":
            return None
        if (
            evidence.user_id,
            evidence.session_id,
            evidence.conversation_id,
        ) != (binding.user_id, binding.session_id, binding.conversation_id):
            return None
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pending_ticket_mutations WHERE mutation_id = ?",
                (binding.mutation_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            mutation = self._mutation(row)
            now = _utc_now()
            if mutation.expires_at <= now and mutation.status == "pending":
                conn.execute(
                    "UPDATE pending_ticket_mutations SET status='expired' WHERE mutation_id=? AND status='pending'",
                    (binding.mutation_id,),
                )
                conn.commit()
                return None
            if mutation.status != "pending" or not self._matches(mutation, binding):
                conn.rollback()
                return None
            confirmed_at = _iso(now)
            confirmed = conn.execute(
                """UPDATE pending_ticket_mutations
                   SET status='confirmed', confirmed_at=?
                   WHERE mutation_id=? AND status='pending'""",
                (confirmed_at, binding.mutation_id),
            ).rowcount
            if confirmed != 1:
                conn.rollback()
                return None
            consumed = conn.execute(
                """UPDATE pending_ticket_mutations
                   SET status='consumed'
                   WHERE mutation_id=? AND status='confirmed' AND confirmed_at=?""",
                (binding.mutation_id, confirmed_at),
            ).rowcount
            if consumed != 1:
                conn.rollback()
                return None
            conn.commit()
        return ConsumedMutationEvidenceV1(
            mutation_id=binding.mutation_id,
            confirmed_at=now,
        )

    def cancel_update(
        self,
        binding: PendingMutationBindingV1,
        evidence: CurrentTurnEvidenceV1,
    ) -> bool:
        if binding.operation != "update" or evidence.decision != "explicit_cancel":
            return False
        if (
            evidence.user_id,
            evidence.session_id,
            evidence.conversation_id,
        ) != (binding.user_id, binding.session_id, binding.conversation_id):
            return False
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM pending_ticket_mutations WHERE mutation_id=?",
                (binding.mutation_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            mutation = self._mutation(row)
            if mutation.status != "pending" or not self._matches(mutation, binding):
                conn.rollback()
                return False
            changed = conn.execute(
                "UPDATE pending_ticket_mutations SET status='cancelled' WHERE mutation_id=? AND status='pending'",
                (binding.mutation_id,),
            ).rowcount
            conn.commit()
        return changed == 1

    @staticmethod
    def _mutation(row: sqlite3.Row) -> PendingTicketMutationV1:
        data = dict(row)
        data.pop("confirmed_at", None)
        data["expires_at"] = _parse(data["expires_at"])
        return PendingTicketMutationV1.model_validate(data)

    @staticmethod
    def _matches(mutation: PendingTicketMutationV1, binding: PendingMutationBindingV1) -> bool:
        return all(
            (
                mutation.mutation_id == binding.mutation_id,
                mutation.operation == binding.operation,
                mutation.user_id == binding.user_id,
                secrets.compare_digest(mutation.session_id, binding.session_id),
                secrets.compare_digest(mutation.conversation_id, binding.conversation_id),
                secrets.compare_digest(mutation.normalized_payload_hash, binding.normalized_payload_hash),
                mutation.ticket_id == binding.ticket_id,
                mutation.base_version == binding.base_version,
            )
        )

    def invalidate(self, *, user_id: int | None = None, session_id: str | None = None, conversation_id: str | None = None) -> int:
        filters: list[str] = []
        values: list[Any] = []
        for column, value in (("user_id", user_id), ("session_id", session_id), ("conversation_id", conversation_id)):
            if value is not None:
                filters.append(f"{column} = ?")
                values.append(value)
        if not filters:
            raise ValueError("at least one lifecycle selector is required")
        with open_db(self.db_path) as conn, conn:
            cursor = conn.execute(
                f"UPDATE pending_ticket_mutations SET status = 'invalidated' WHERE ({' AND '.join(filters)}) AND status IN ('pending','confirmed')",
                values,
            )
            return cursor.rowcount


@dataclass(frozen=True)
class ResolvedCursor:
    query: CanonicalTicketQueryV1
    upstream_cursor: str


class ContinuationReferenceGenerator:
    """URL-safe continuation reference를 만드는 명시적 entropy boundary."""

    def __init__(
        self,
        random_bytes: Callable[[int], bytes] | None = None,
        *,
        entropy_bits: int = 256,
        source_is_csprng: bool | None = None,
    ):
        if entropy_bits < 128 or entropy_bits % 8:
            raise ValueError("continuation references require at least 128 whole bits of entropy")
        if random_bytes is None:
            self._random_bytes = secrets.token_bytes
        else:
            if source_is_csprng is not True:
                raise ValueError("an injected continuation source must attest CSPRNG strength")
            self._random_bytes = random_bytes
        self.entropy_bits = entropy_bits

    def generate(self) -> str:
        byte_count = self.entropy_bits // 8
        raw = self._random_bytes(byte_count)
        if not isinstance(raw, bytes) or len(raw) != byte_count:
            raise ValueError("continuation source returned an invalid entropy sample")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class CursorRepository:
    def __init__(
        self,
        db_path: Path,
        *,
        lifetime: timedelta = timedelta(minutes=15),
        reference_generator: ContinuationReferenceGenerator | None = None,
    ):
        self.db_path = db_path
        self.lifetime = lifetime
        self.reference_generator = reference_generator or ContinuationReferenceGenerator()
        with open_db(db_path) as conn, conn:
            conn.executescript(STATE_SCHEMA)

    def issue(self, capability: TicketRequestCapability, query: CanonicalTicketQueryV1, upstream_cursor: str) -> str:
        if not upstream_cursor:
            raise ValueError("upstream cursor is required")
        canonical = query.canonical_json()
        ref = self.reference_generator.generate()
        now = _utc_now()
        with open_db(self.db_path) as conn, conn:
            conn.execute(
                """
                INSERT INTO ticket_cursor_bindings
                (continuation_ref, user_id, session_id, conversation_id, query_serialization_version,
                 canonical_normalized_query_json, normalized_query_hash, upstream_cursor, created_at, expires_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    ref,
                    capability.user_id,
                    capability.session_id,
                    capability.conversation_id,
                    canonical,
                    _sha(canonical),
                    upstream_cursor,
                    _iso(now),
                    _iso(now + self.lifetime),
                ),
            )
        return ref

    def resolve_once(self, capability: TicketRequestCapability, continuation_ref: str) -> ResolvedCursor | None:
        with open_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM ticket_cursor_bindings WHERE continuation_ref = ?", (continuation_ref,)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            data = dict(row)
            if _parse(data["expires_at"]) <= _utc_now():
                conn.execute("DELETE FROM ticket_cursor_bindings WHERE continuation_ref = ?", (continuation_ref,))
                conn.commit()
                return None
            canonical = data["canonical_normalized_query_json"]
            matches = (
                data["user_id"] == capability.user_id
                and secrets.compare_digest(data["session_id"], capability.session_id)
                and secrets.compare_digest(data["conversation_id"], capability.conversation_id)
                and data["query_serialization_version"] == 1
                and secrets.compare_digest(data["normalized_query_hash"], _sha(canonical))
            )
            if not matches:
                conn.rollback()
                return None
            try:
                query = CanonicalTicketQueryV1.model_validate_json(canonical)
            except ValueError:
                conn.rollback()
                return None
            consumed = conn.execute(
                "DELETE FROM ticket_cursor_bindings WHERE continuation_ref = ?", (continuation_ref,)
            ).rowcount
            conn.commit()
        return ResolvedCursor(query=query, upstream_cursor=data["upstream_cursor"]) if consumed == 1 else None

    def invalidate(self, *, user_id: int | None = None, session_id: str | None = None, conversation_id: str | None = None) -> int:
        filters: list[str] = []
        values: list[Any] = []
        for column, value in (("user_id", user_id), ("session_id", session_id), ("conversation_id", conversation_id)):
            if value is not None:
                filters.append(f"{column} = ?")
                values.append(value)
        if not filters:
            raise ValueError("at least one lifecycle selector is required")
        with open_db(self.db_path) as conn, conn:
            cursor = conn.execute(f"DELETE FROM ticket_cursor_bindings WHERE {' AND '.join(filters)}", values)
            return cursor.rowcount

    def cleanup_expired(self) -> int:
        with open_db(self.db_path) as conn, conn:
            cursor = conn.execute("DELETE FROM ticket_cursor_bindings WHERE expires_at <= ?", (_iso(_utc_now()),))
            return cursor.rowcount


@dataclass(frozen=True)
class PersistedAssistantMessage:
    sequence: int
    text: str
    text_sha256: str


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("text"))
    return ""


class PresentationRepository:
    def __init__(self, history_db_path: Path):
        self.db_path = history_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(HISTORY_SCHEMA)

    def latest_assistant(self, conversation_id: str) -> PersistedAssistantMessage | None:
        if not self.db_path.exists():
            return None
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, message_data FROM agent_messages WHERE session_id = ? ORDER BY id DESC",
                (conversation_id,),
            ).fetchall()
        for sequence, raw in rows:
            try:
                item = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if item.get("role") == "assistant" and item.get("type", "message") == "message":
                text = _content_text(item.get("content"))
                if text:
                    return PersistedAssistantMessage(sequence, text, _sha(text))
        return None

    def put_and_readback(
        self, conversation_id: str, message: PersistedAssistantMessage, presentation: TicketPresentationV1
    ) -> TicketPresentationV1 | None:
        canonical = presentation.model_dump_json()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT message_data FROM agent_messages WHERE session_id = ? AND id = ?",
                (conversation_id, message.sequence),
            ).fetchone()
            if row is None:
                return None
            try:
                item = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                return None
            if _sha(_content_text(item.get("content"))) != message.text_sha256:
                return None
            conn.execute(
                """
                INSERT OR REPLACE INTO ticket_message_presentations
                (conversation_id, assistant_sequence, assistant_text_sha256, presentation_version,
                 canonical_presentation_json, created_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (conversation_id, message.sequence, message.text_sha256, canonical, _iso(_utc_now())),
            )
            conn.commit()
            saved = conn.execute(
                """SELECT canonical_presentation_json FROM ticket_message_presentations
                   WHERE conversation_id = ? AND assistant_sequence = ? AND assistant_text_sha256 = ?""",
                (conversation_id, message.sequence, message.text_sha256),
            ).fetchone()
        if saved is None:
            return None
        try:
            return TicketPresentationV1.model_validate_json(saved[0])
        except ValueError:
            return None

    def load(self, conversation_id: str, sequence: int, text: str) -> TicketPresentationV1 | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT canonical_presentation_json FROM ticket_message_presentations
                   WHERE conversation_id = ? AND assistant_sequence = ? AND assistant_text_sha256 = ?
                     AND presentation_version = 1""",
                (conversation_id, sequence, _sha(text)),
            ).fetchone()
        if row is None:
            return None
        try:
            return TicketPresentationV1.model_validate_json(row[0])
        except ValueError:
            return None

    def message_rows(self, conversation_id: str) -> list[tuple[int, dict[str, Any]]]:
        with sqlite3.connect(self.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT id, message_data FROM agent_messages WHERE session_id = ? ORDER BY id ASC", (conversation_id,)
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        result: list[tuple[int, dict[str, Any]]] = []
        for sequence, raw in rows:
            try:
                item = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict):
                result.append((sequence, item))
        return result

    def clear(self, conversation_id: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM ticket_message_presentations WHERE conversation_id = ?", (conversation_id,))
            conn.commit()
            return cursor.rowcount

    def cleanup_orphans(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            try:
                cursor = conn.execute(
                    """
                    DELETE FROM ticket_message_presentations
                    WHERE NOT EXISTS (
                      SELECT 1 FROM agent_messages m
                      WHERE m.session_id = ticket_message_presentations.conversation_id
                        AND m.id = ticket_message_presentations.assistant_sequence
                    )
                    """
                )
            except sqlite3.OperationalError:
                return 0
            conn.commit()
            return cursor.rowcount
