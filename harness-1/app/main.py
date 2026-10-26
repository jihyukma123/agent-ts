"""ITSM Assistant(에이전트 서비스) FastAPI 진입점.

실행: uvicorn app.main:create_app --factory --port 8100
(import 시 DB를 만들지 않도록 모듈 수준 app 객체 대신 팩토리를 사용합니다.)
기존 ITSM 시스템과는 ITSM API(기본 http://127.0.0.1:8001)로만 연결됩니다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.routes import router as agent_router
from app.agent.service import AgentService
from app.config import Settings, load_settings
from app.db import init_db
from app.itsm_auth import ItsmAuthClient, ItsmError
from app.session import router as session_router

logger = logging.getLogger("itsm_agent")


def create_app(
    settings: Settings | None = None,
    agent_service: AgentService | None = None,
    itsm_client: ItsmAuthClient | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    init_db(settings.db_path)
    itsm = itsm_client or ItsmAuthClient(
        settings.itsm_base_url,
        settings.itsm_client_id,
        settings.itsm_client_secret,
        timeout=settings.itsm_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await itsm.aclose()

    app = FastAPI(title="ITSM Assistant", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.itsm = itsm
    app.state.agent_service = agent_service or AgentService(settings)
    if not app.state.agent_service.configured:
        logger.warning(".env.local 에 OPENAI_API_KEY 가 없어 Agent 채팅이 비활성화됩니다.")

    app.include_router(session_router)
    app.include_router(agent_router)

    @app.get("/api/health", tags=["system"])
    async def health() -> dict:
        try:
            itsm_health = await itsm.health()
            itsm_state = {"reachable": True, "version": itsm_health.get("version")}
        except ItsmError as exc:
            itsm_state = {"reachable": False, "error": str(exc)}
        return {
            "status": "ok",
            "itsm": {"base_url": settings.itsm_base_url, **itsm_state},
            "agent": {
                "configured": app.state.agent_service.configured,
                "model": settings.agent_model,
                "reasoning_effort": settings.agent_reasoning_effort,
            },
        }

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(settings.static_dir / "index.html")

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    return app
