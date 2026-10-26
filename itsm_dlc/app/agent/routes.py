"""Agent 채팅 API. 모든 엔드포인트는 현재 사용자 세션이 필요합니다."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.schemas import AgentChatIn, AgentChatOut, AgentContextOut, AgentHistoryOut
from app.session import CurrentUser, get_current_user

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/chat", response_model=AgentChatOut, response_model_exclude_none=True)
async def chat(body: AgentChatIn, request: Request, user: CurrentUser = Depends(get_current_user)) -> AgentChatOut:
    result = await request.app.state.agent_service.chat(request, user, body.message)
    return AgentChatOut(
        reply=result.reply,
        presentation=result.presentation,
        context=AgentContextOut(user_id=user.id, username=user.username, role=user.role),
    )


@router.get("/messages", response_model=AgentHistoryOut, response_model_exclude_none=True)
async def messages(request: Request, user: CurrentUser = Depends(get_current_user)) -> AgentHistoryOut:
    return AgentHistoryOut(messages=await request.app.state.agent_service.history(request, user))


@router.delete("/messages", status_code=status.HTTP_204_NO_CONTENT)
async def clear_messages(request: Request, user: CurrentUser = Depends(get_current_user)) -> Response:
    await request.app.state.agent_service.clear_history(request, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
