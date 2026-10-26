"""토큰 발급과 현재 주체 조회."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from itsm_api.auth import Principal, authenticate_client, get_db, issue_token, require_user
from itsm_api.errors import ApiError
from itsm_api.routes.directory import load_user
from itsm_api.schemas import MeOut, TokenIn, TokenOut

router = APIRouter(prefix="/api/v2", tags=["auth"])


@router.post("/auth/token", response_model=TokenOut)
def token(body: TokenIn, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> TokenOut:
    """클라이언트 자격증명으로 서비스 토큰 또는 사용자 위임 토큰을 발급합니다."""
    client = authenticate_client(conn, body.client_id, body.client_secret)
    settings = request.app.state.settings
    now = request.app.state.clock.now()
    user_id = None
    if body.grant_type == "token_exchange":
        if not client["can_delegate"]:
            raise ApiError(403, "delegation_not_allowed", f"클라이언트 '{client['client_id']}' 은(는) 사용자 위임 토큰을 받을 수 없습니다.")
        if not body.subject:
            raise ApiError(400, "invalid_grant", "token_exchange 에는 subject(username)가 필요합니다.")
        user = conn.execute("SELECT id, active FROM users WHERE username = ?", (body.subject,)).fetchone()
        if user is None or not user["active"]:
            raise ApiError(400, "invalid_grant", f"사용자 '{body.subject}' 을(를) 대신할 수 없습니다(없거나 비활성).")
        user_id = user["id"]
    elif body.subject:
        raise ApiError(400, "invalid_grant", "client_credentials 에는 subject 를 보내지 않습니다.")
    access_token, expires_at = issue_token(conn, client["client_id"], user_id, settings.token_ttl_seconds, now)
    return TokenOut(
        access_token=access_token, expires_in=settings.token_ttl_seconds, expires_at=expires_at, subject=body.subject
    )


@router.get("/me", response_model=MeOut)
def me(principal: Principal = Depends(require_user()), conn: sqlite3.Connection = Depends(get_db)) -> MeOut:
    user = load_user(conn, principal.user_id)
    return MeOut(**user.model_dump(), permissions=sorted(principal.permissions))
