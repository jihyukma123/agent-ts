"""Same-origin BFF routes for the separate ITSM Assistant service."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from itsm_web.agent_client import AGENT_SESSION_COOKIE, AgentClient, AgentResponse, AgentUnavailableError
from itsm_web.session import CurrentUser, get_current_user

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
SESSION_COOKIES = {
    "baseline": AGENT_SESSION_COOKIE,
    "harness": "itsm_harness_agent_session",
}


def _variant(request: Request) -> str:
    variant = request.query_params.get("variant", "baseline")
    if variant not in SESSION_COOKIES:
        raise HTTPException(422, detail={"code": "invalid_variant", "message": "baseline 또는 harness를 선택하세요."})
    return variant


def _client(request: Request) -> AgentClient:
    return request.app.state.agent_clients[_variant(request)]


def _cookie(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIES[_variant(request)])


def _set_cookie(request: Request, response: Response, value: str | None) -> None:
    if value:
        response.set_cookie(SESSION_COOKIES[_variant(request)], value, httponly=True, samesite="lax", path="/")


def _raise_upstream(result: AgentResponse) -> None:
    if result.status_code < 400:
        return
    detail: Any = result.data.get("detail") if isinstance(result.data, dict) else None
    if not isinstance(detail, dict):
        detail = {
            "code": "assistant_request_failed",
            "message": f"Agent 요청에 실패했습니다. (HTTP {result.status_code})",
        }
    raise HTTPException(status_code=result.status_code, detail=detail)


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "assistant_unavailable",
            "message": "ITSM Agent 서비스에 연결하지 못했습니다.",
        },
    )


async def _ensure(request: Request, user: CurrentUser) -> tuple[str | None, AgentResponse]:
    incoming = _cookie(request)
    try:
        result = await _client(request).ensure_session(user.id, incoming)
    except AgentUnavailableError:
        raise _unavailable() from None
    _raise_upstream(result)
    return result.session_cookie or incoming, result


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    try:
        result = await _client(request).health()
    except AgentUnavailableError:
        return {"reachable": False, "configured": False}
    if result.status_code != 200 or not isinstance(result.data, dict):
        return {"reachable": False, "configured": False}
    agent = result.data.get("agent") if isinstance(result.data.get("agent"), dict) else {}
    return {
        "reachable": True,
        "configured": bool(agent.get("configured")),
        "model": agent.get("model"),
        "reasoning_effort": agent.get("reasoning_effort"),
        "itsm": result.data.get("itsm"),
    }


@router.post("/session")
async def ensure_session(
    request: Request,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
) -> Any:
    _session_cookie, result = await _ensure(request, user)
    _set_cookie(request, response, result.session_cookie)
    return result.data


async def _proxy(
    method: str,
    path: str,
    request: Request,
    response: Response,
    user: CurrentUser,
    *,
    body: dict[str, Any] | None = None,
) -> Any:
    session_cookie, session = await _ensure(request, user)
    try:
        result = await _client(request).request(
            method,
            path,
            session_cookie=session_cookie,
            json=body,
        )
    except AgentUnavailableError:
        raise _unavailable() from None
    _raise_upstream(result)
    _set_cookie(request, response, result.session_cookie or session.session_cookie)
    if result.status_code == status.HTTP_204_NO_CONTENT:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    return result.data


@router.get("/messages")
async def messages(
    request: Request,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
) -> Any:
    return await _proxy("GET", "/api/agent/messages", request, response, user)


@router.post("/chat")
async def chat(
    body: dict[str, Any],
    request: Request,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
) -> Any:
    message = body.get("message")
    if not isinstance(message, str) or not message.strip() or len(message.strip()) > 4000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_message", "message": "1~4000자의 메시지를 입력하세요."},
        )
    return await _proxy(
        "POST",
        "/api/agent/chat",
        request,
        response,
        user,
        body={"message": message.strip()},
    )


@router.delete("/messages", status_code=status.HTTP_204_NO_CONTENT)
async def clear_messages(
    request: Request,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
) -> None:
    await _proxy("DELETE", "/api/agent/messages", request, response, user)
