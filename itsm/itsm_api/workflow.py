"""티켓 상태 전이 규칙.

전이마다 필요한 권한과 필수 입력이 다릅니다. 요청자 본인은 일부 전이(접수 상태 취소, 해결 후 7일 내 재오픈)를
권한 없이도 할 수 있습니다. 병합(merge)으로 인한 종료는 전이 API 가 아니라 병합 API 로만 일어납니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from itsm_api.auth import Principal
from itsm_api.timeutil import parse_iso

REOPEN_WINDOW = timedelta(days=7)

ALL_TRANSITION_FIELDS = ("assignee_id", "pending_reason", "resolution_code", "resolution_note", "reason", "note")


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    permission: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    requester_may: bool = False


TRANSITIONS: list[Transition] = [
    Transition("received", "assigned", "ticket:assign", optional=("assignee_id", "note")),
    Transition("received", "cancelled", "ticket:close", required=("reason",), requester_may=True),
    Transition("assigned", "investigating", "ticket:transition", optional=("note",)),
    Transition("assigned", "cancelled", "ticket:close", required=("reason",)),
    Transition("investigating", "pending", "ticket:transition", required=("pending_reason",), optional=("note",)),
    Transition("pending", "investigating", "ticket:transition", optional=("note",)),
    Transition("investigating", "monitoring", "ticket:transition", required=("note",)),
    Transition("monitoring", "investigating", "ticket:transition", required=("note",)),
    Transition("investigating", "resolved", "ticket:resolve", required=("resolution_code", "resolution_note")),
    Transition("monitoring", "resolved", "ticket:resolve", required=("resolution_code", "resolution_note")),
    Transition("resolved", "closed", "ticket:close", optional=("note",)),
    Transition("resolved", "investigating", "ticket:reopen", required=("reason",), requester_may=True),
]


def find(source: str, target: str) -> Transition | None:
    return next((t for t in TRANSITIONS if t.source == source and t.target == target), None)


def required_fields(transition: Transition, ticket) -> tuple[str, ...]:
    """티켓 상태에 따라 달라지는 필수 입력을 포함합니다. 담당자 없이 배정됨으로 바꿀 수는 없습니다."""
    if transition.target == "assigned" and ticket["assignee_id"] is None:
        return ("assignee_id",) + transition.required
    return transition.required


def is_permitted(transition: Transition, principal: Principal, ticket, now: datetime) -> bool:
    if principal.has(transition.permission):
        return True
    if not transition.requester_may or ticket["requester_id"] != principal.user_id:
        return False
    if transition.target == "investigating":  # 재오픈
        return ticket["resolved_at"] is not None and now - parse_iso(ticket["resolved_at"]) <= REOPEN_WINDOW
    return True


def allowed(principal: Principal, ticket, now: datetime) -> list[Transition]:
    if ticket["merged_into"]:
        return []
    return [t for t in TRANSITIONS if t.source == ticket["status"] and is_permitted(t, principal, ticket, now)]
