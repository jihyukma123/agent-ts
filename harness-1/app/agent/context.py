"""Agent 실행 context.

Runner.run(context=...) 로 전달되어 애플리케이션 코드(이후 추가될 도구)가 사용합니다.
값은 항상 백엔드가 세션으로 확인한 CurrentUser 에서 만들며, 사용자 메시지나 모델 출력으로 바꾸지 않습니다.
OpenAI API 키는 넣지 않습니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.itsm_tickets import TicketGateway
from app.session import CurrentUser


@dataclass(frozen=True)
class AgentUserContext:
    user_id: int
    username: str
    display_name: str
    role: str
    permissions: frozenset[str]
    ticket_gateway: TicketGateway | None = field(default=None, repr=False, compare=False)
    _failure_state: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_current_user(cls, user: CurrentUser, ticket_gateway: TicketGateway | None = None) -> AgentUserContext:
        return cls(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            permissions=user.permissions,
            ticket_gateway=ticket_gateway,
        )

    def has_permission(self, code: str) -> bool:
        return code in self.permissions

    def record_failure(self, tool_name: str, error_code: str) -> bool:
        """동일 도구·원인의 연속 실패를 기록하고 중지 상한 도달 여부를 반환합니다."""
        key = f"{tool_name}:{error_code}"
        count = int(self._failure_state.get("count", 0)) + 1 if self._failure_state.get("key") == key else 1
        self._failure_state.update(key=key, count=count)
        return count >= 2

    def record_success(self) -> None:
        self._failure_state.clear()
