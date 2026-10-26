"""Agent 채팅 API. 모든 엔드포인트는 현재 사용자 세션이 필요합니다."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.schemas import AgentChatIn, AgentChatOut, AgentContextOut, AgentHistoryOut
from app.itsm_tickets import ItsmTicketGateway, TicketGatewayError
from app.session import CurrentUser, get_current_user, get_itsm, itsm_token_for

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/chat", response_model=AgentChatOut)
async def chat(body: AgentChatIn, request: Request, user: CurrentUser = Depends(get_current_user)) -> AgentChatOut:
    async def token_provider(force_refresh: bool) -> str:
        try:
            return await itsm_token_for(request, user, force_refresh=force_refresh)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            unavailable = detail.get("code") == "itsm_unavailable"
            raise TicketGatewayError(
                error_code="itsm_unavailable" if unavailable else "itsm_auth_failed",
                field=None,
                reason="ITSM 시스템에 연결할 수 없습니다." if unavailable else "ITSM 사용자 인증을 갱신하지 못했습니다.",
                retryable=unavailable,
                hint="잠시 후 다시 시도하세요." if unavailable else "다시 로그인하세요.",
            ) from None

    gateway = ItsmTicketGateway(get_itsm(request), token_provider)
    reply = await request.app.state.agent_service.chat(user, body.message, ticket_gateway=gateway)
    return AgentChatOut(reply=reply, context=AgentContextOut(user_id=user.id, username=user.username, role=user.role))


@router.get("/messages", response_model=AgentHistoryOut)
async def messages(request: Request, user: CurrentUser = Depends(get_current_user)) -> AgentHistoryOut:
    return AgentHistoryOut(messages=await request.app.state.agent_service.history(user.conversation_id))


@router.delete("/messages", status_code=status.HTTP_204_NO_CONTENT)
async def clear_messages(request: Request, user: CurrentUser = Depends(get_current_user)) -> Response:
    await request.app.state.agent_service.clear_history(user.conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
