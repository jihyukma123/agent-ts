"""ITSM 웹 API 입출력 스키마.

티켓 관련 응답은 ITSM API(v2) 응답을 그대로 전달하므로 여기서 다시 정의하지 않습니다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc))


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
