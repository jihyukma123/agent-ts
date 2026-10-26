"""Agent 실행 context.

Runner.run(context=...) 로 전달되어 애플리케이션 코드(이후 추가될 도구)가 사용합니다.
값은 항상 백엔드가 세션으로 확인한 CurrentUser 에서 만들며, 사용자 메시지나 모델 출력으로 바꾸지 않습니다.
OpenAI API 키는 넣지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.session import CurrentUser

if TYPE_CHECKING:
    from app.ticketing.lifecycle import CurrentTurnEvidenceV1
    from app.ticketing.tools import TicketRuntime


@dataclass(frozen=True)
class AgentUserContext:
    user_id: int
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    # Runner context 내부에서만 쓰이며 function-tool schema나 직렬화 경계에 포함되지 않는다.
    ticket_runtime: TicketRuntime | None = field(default=None, repr=False, compare=False)
    current_turn_evidence: CurrentTurnEvidenceV1 | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_current_user(
        cls,
        user: CurrentUser,
        *,
        ticket_runtime: TicketRuntime | None = None,
        current_turn_evidence: CurrentTurnEvidenceV1 | None = None,
    ) -> AgentUserContext:
        return cls(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            permissions=user.permissions,
            ticket_runtime=ticket_runtime,
            current_turn_evidence=current_turn_evidence,
        )

    def has_permission(self, code: str) -> bool:
        return code in self.permissions
