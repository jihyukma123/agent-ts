"""ITSM 웹 FastAPI 애플리케이션 진입점 (화면 + 세션 + 화면용 티켓 API).

실행: uvicorn itsm_web.main:create_app --factory --port 8000
(import 시 DB를 만들지 않도록 모듈 수준 app 객체 대신 팩토리를 사용합니다.)
ITSM 데이터는 별도 프로세스인 ITSM API(itsm_api/, 기본 http://127.0.0.1:8001)에서 가져옵니다.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from itsm_web.agent import router as agent_router
from itsm_web.agent_client import AgentClient
from itsm_web.config import Settings, load_settings
from itsm_web.db import init_db
from itsm_web.itsm_client import ItsmClient, ItsmError
from itsm_web.session import router as session_router
from itsm_web.tickets import router as tickets_router


def create_app(
    settings: Settings | None = None,
    itsm_client: ItsmClient | None = None,
    agent_client: AgentClient | None = None,
    harness_agent_client: AgentClient | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    init_db(settings.db_path)
    itsm = itsm_client or ItsmClient(
        settings.itsm_base_url,
        settings.itsm_client_id,
        settings.itsm_client_secret,
        timeout=settings.itsm_timeout_seconds,
    )
    agent = agent_client or AgentClient(
        settings.agent_base_url,
        timeout=settings.agent_timeout_seconds,
    )
    harness_agent = harness_agent_client or AgentClient(
        settings.harness_agent_base_url,
        timeout=settings.agent_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await itsm.aclose()
        await agent.aclose()
        await harness_agent.aclose()

    app = FastAPI(title="ITSM Web", version="0.3.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.itsm = itsm
    app.state.agent = agent
    app.state.agent_clients = {"baseline": agent, "harness": harness_agent}

    app.include_router(session_router)
    app.include_router(tickets_router)
    app.include_router(agent_router)

    @app.get("/api/health", tags=["system"])
    async def health() -> dict:
        try:
            itsm_health = await itsm.health()
            itsm_state = {"reachable": True, "version": itsm_health.get("version")}
        except ItsmError as exc:
            itsm_state = {"reachable": False, "error": str(exc)}
        return {"status": "ok", "itsm": {"base_url": settings.itsm_base_url, **itsm_state}}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(settings.static_dir / "index.html")

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    return app
