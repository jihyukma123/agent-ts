"""화면용 티켓 API (ITSM API v2 중계).

브라우저는 ITSM API 를 직접 호출하지 않고 이 엔드포인트를 거칩니다. ITSM 웹은 세션의 위임 토큰으로 ITSM API 를 호출하고
응답을 그대로 전달합니다. 권한·가시성·검증은 모두 ITSM API 가 판단합니다.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request, status

from itsm_web.itsm_client import ItsmApiError, ItsmUnavailableError
from itsm_web.session import CurrentUser, get_current_user, get_itsm, itsm_http_error, itsm_token_for

router = APIRouter(prefix="/api", tags=["tickets (ITSM 중계)"])


async def _call(request: Request, user: CurrentUser, method: str, *args: Any, **kwargs: Any) -> Any:
    token = await itsm_token_for(request, user)
    try:
        return await getattr(get_itsm(request), method)(token, *args, **kwargs)
    except (ItsmApiError, ItsmUnavailableError) as exc:
        raise itsm_http_error(exc)


@router.get("/tickets")
async def list_tickets(
    request: Request,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    params: dict[str, Any] = {"limit": limit}
    if q:
        params["q"] = q
    if status_filter:
        params["status"] = status_filter
    if cursor:
        params["cursor"] = cursor
    return await _call(request, user, "list_tickets", params)


@router.get("/tickets/{ticket_id}")
async def read_ticket(ticket_id: str, request: Request, user: CurrentUser = Depends(get_current_user)) -> dict:
    return await _call(request, user, "get_ticket", ticket_id)


@router.get("/tickets/{ticket_id}/worklogs")
async def read_worklogs(
    ticket_id: str,
    request: Request,
    cursor: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    params: dict[str, Any] = {"limit": 50}
    if cursor:
        params["cursor"] = cursor
    return await _call(request, user, "list_worklogs", ticket_id, params)


@router.post("/tickets", status_code=status.HTTP_201_CREATED)
async def register_ticket(
    request: Request,
    body: Annotated[dict[str, Any], Body()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    # 요청자는 ITSM API 가 위임 토큰의 사용자로 정합니다. 본문 검증도 ITSM API 가 합니다.
    return await _call(request, user, "create_ticket", body, idempotency_key)


@router.get("/catalog/services")
async def catalog_services(request: Request, _user: CurrentUser = Depends(get_current_user)) -> list[dict]:
    try:
        return await get_itsm(request).list_services()
    except (ItsmApiError, ItsmUnavailableError) as exc:
        raise itsm_http_error(exc)


@router.get("/catalog/models")
async def catalog_models(request: Request, _user: CurrentUser = Depends(get_current_user)) -> list[dict]:
    try:
        return await get_itsm(request).list_models()
    except (ItsmApiError, ItsmUnavailableError) as exc:
        raise itsm_http_error(exc)
