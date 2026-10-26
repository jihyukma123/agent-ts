"""현재 HTTP chat turn에서만 유효한 mutation 확인 근거."""

from __future__ import annotations

import hashlib
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CurrentTurnEvidenceV1:
    turn_id: str
    user_id: int
    session_id: str
    conversation_id: str
    raw_message_sha256: str
    decision: Literal["explicit_confirm", "explicit_cancel", "revise_or_ambiguous"]

    @classmethod
    def capture(
        cls, *, user_id: int, session_id: str, conversation_id: str, raw_message: str
    ) -> CurrentTurnEvidenceV1:
        normalized = unicodedata.normalize("NFKC", raw_message).strip().casefold()
        if normalized in ("확인", "confirm"):
            decision = "explicit_confirm"
        elif normalized in ("취소", "cancel"):
            decision = "explicit_cancel"
        else:
            decision = "revise_or_ambiguous"
        return cls(
            turn_id=secrets.token_urlsafe(24),
            user_id=user_id,
            session_id=session_id,
            conversation_id=conversation_id,
            raw_message_sha256=hashlib.sha256(raw_message.encode("utf-8")).hexdigest(),
            decision=decision,
        )
