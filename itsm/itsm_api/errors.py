"""오류 응답.

- v2: RFC 9457(구 7807) problem+json. `code` 는 기계 판독용 안정 코드, `detail` 은 사람이 읽는 설명입니다.
  오류 종류에 따라 확장 필드(`errors`, `allowed_transitions`, `current_version` 등)를 함께 반환합니다.
- v1(legacy): 기존 연동 호환을 위해 text/plain 한 줄 메시지를 유지합니다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("itsm_api")

PROBLEM_TYPE_BASE = "https://itsm.internal/problems/"

_DEFAULT_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    412: "Precondition Failed",
    422: "Unprocessable Content",
    428: "Precondition Required",
    429: "Too Many Requests",
    500: "Internal Server Error",
}


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        title: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        **extensions: Any,
    ):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title or _DEFAULT_TITLES.get(status, "Error")
        self.errors = errors
        self.headers = headers or {}
        self.extensions = extensions


def field_error(field: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"field": field, "code": code, "message": message, **extra}


def _is_legacy(request: Request) -> bool:
    return request.url.path.startswith("/api/v1/")


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def problem_response(request: Request, error: ApiError) -> Response:
    headers = dict(error.headers)
    if _is_legacy(request):
        return PlainTextResponse(f"ERROR {error.code.upper()}: {error.detail}", status_code=error.status, headers=headers)
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + error.code,
        "title": error.title,
        "status": error.status,
        "code": error.code,
        "detail": error.detail,
        "instance": request.url.path,
        "request_id": _request_id(request),
    }
    if error.errors:
        body["errors"] = error.errors
    body.update(error.extensions)
    return JSONResponse(body, status_code=error.status, headers=headers, media_type="application/problem+json")


def _validation_field(loc: tuple[Any, ...] | list[Any]) -> str:
    parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
    return ".".join(parts) or "request"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> Response:
        return problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        errors = [
            field_error(_validation_field(e.get("loc", ())), e.get("type", "invalid"), e.get("msg", "invalid value"))
            for e in exc.errors()
        ]
        if _is_legacy(request):
            first = errors[0] if errors else {"field": "request", "message": "invalid"}
            return PlainTextResponse(f"ERROR BAD_REQUEST: {first['field']} {first['message']}", status_code=400)
        return problem_response(
            request,
            ApiError(422, "validation_failed", "요청 값 검증에 실패했습니다. errors 의 필드별 내용을 확인하세요.", errors=errors),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        code = {404: "route_not_found", 405: "method_not_allowed"}.get(exc.status_code, f"http_{exc.status_code}")
        return problem_response(request, ApiError(exc.status_code, code, str(exc.detail)))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        logger.exception("처리되지 않은 오류 (request_id=%s)", _request_id(request))
        return problem_response(
            request,
            ApiError(500, "internal_error", "서버 내부 오류가 발생했습니다. request_id 와 함께 운영팀에 문의하세요."),
        )
