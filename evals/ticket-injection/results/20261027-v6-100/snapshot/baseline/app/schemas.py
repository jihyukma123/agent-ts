"""에이전트 서비스 API 입출력 스키마."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ticketing.models import TicketPresentationV1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- 사용자 / 세션 ----------


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    role_label: str
    team: str | None
    permissions: list[str] | None = Field(default=None, description="세션 사용자에만 포함")


class SessionSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class SessionOut(BaseModel):
    user: UserOut


# ---------- Agent ----------


class AgentChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=4000)


class AgentContextOut(BaseModel):
    user_id: int
    username: str
    role: str


class AgentChatOut(BaseModel):
    reply: str
    context: AgentContextOut
    presentation: TicketPresentationV1 | None = None


class AgentMessageOut(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    presentation: TicketPresentationV1 | None = None


class AgentHistoryOut(BaseModel):
    messages: list[AgentMessageOut]
