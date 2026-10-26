"""ITSM API 애플리케이션 진입점.

실행: uvicorn itsm_api.main:create_app --factory --port 8001
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request

from itsm_api.auth import RateLimiter
from itsm_api.config import ItsmApiSettings, load_settings
from itsm_api.db import init_db
from itsm_api.errors import install_error_handlers
from itsm_api.routes import auth, catalog, directory, exports, legacy_v1, problems, tickets
from itsm_api.routes.exports import ExportRunner
from itsm_api.timeutil import Clock

V1_SUNSET = "Wed, 31 Mar 2027 00:00:00 GMT"


def create_app(settings: ItsmApiSettings | None = None, clock: Clock | None = None) -> FastAPI:
    settings = settings or load_settings()
    clock = clock or Clock()
    init_db(settings.db_path)

    docs = settings.enable_docs
    app = FastAPI(
        title="ITSM API",
        version="2.4.0",
        description="사내 IT 서비스 관리 시스템 API. 모든 데이터는 실험용 모의 데이터입니다.",
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.state.clock = clock
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.export_runner = ExportRunner(settings.db_path, clock, settings.export_step_seconds)
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        remaining = getattr(request.state, "rate_limit_remaining", None)
        if remaining is not None:
            response.headers["X-RateLimit-Limit"] = str(settings.rate_limit_per_minute)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = V1_SUNSET
            response.headers["Link"] = '</api/v2/tickets>; rel="successor-version"'
        return response

    for module in (auth, directory, catalog, tickets, problems, exports, legacy_v1):
        app.include_router(module.router)

    @app.get("/api/v2/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "version": app.version, "mock_data": True}

    return app
