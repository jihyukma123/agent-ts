from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from agents import Model, ModelResponse, Usage
from fastapi.testclient import TestClient
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from app.agent.service import AgentService
from app.config import PROJECT_ROOT, Settings
from app.itsm_auth import ItsmAuthClient
from app.main import create_app

CLIENT_ID, CLIENT_SECRET = "itsm-agent", "itsm-agent-dev-secret"

_TEAMS = {1: {"id": 1, "slug": "service-desk", "name": "서비스데스크"}}
FAKE_USERS = [
    # (id, username, display_name, role, role_label, team_id, active, permissions)
    (1, "admin01", "김하늘", "admin", "관리자", 1, True, ["ticket:create", "ticket:read", "ticket:resolve"]),
    (2, "member01", "박바다", "requester", "요청자", None, True, ["ticket:create", "ticket:read"]),
    (5, "desk01", "이서준", "agent_l1", "서비스데스크(L1)", 1, True, ["ticket:assign", "ticket:create", "ticket:read"]),
    (17, "desk03", "문지아", "agent_l1", "서비스데스크(L1)", 1, False, ["ticket:read"]),
]


def _problem(status: int, code: str, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"status": status, "code": code, "detail": detail},
                          headers={"content-type": "application/problem+json"})  # fmt: skip


class FakeItsm:
    """ITSM API 의 인증·디렉터리 엔드포인트만 흉내 내는 테스트용 서버 (httpx.MockTransport).

    받은 요청은 requests 에 기록합니다. 업무 API(/api/v2/tickets 등)는 구현하지 않습니다.
    """

    def __init__(self, token_ttl: timedelta = timedelta(minutes=15)):
        self.token_ttl = token_ttl
        self.requests: list[httpx.Request] = []
        self.tokens: dict[str, str | None] = {}  # token -> username (None = 서비스 토큰)
        self.users = {u[0]: u for u in FAKE_USERS}

    def _user_json(self, u: tuple) -> dict[str, Any]:
        uid, username, name, role, label, team_id, active, _perms = u
        return {"id": uid, "username": username, "display_name": name, "email": f"{username}@corp.example",
                "role": role, "role_label": label, "team": _TEAMS.get(team_id), "active": active}  # fmt: skip

    def _auth(self, request: httpx.Request) -> str | None | httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.tokens:
            return _problem(401, "invalid_token", "유효하지 않은 액세스 토큰입니다.")
        return self.tokens[token]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v2/health":
            return httpx.Response(200, json={"status": "ok", "version": "2.4.0", "mock_data": True})
        if path == "/api/v2/auth/token":
            body = json.loads(request.content)
            if (body["client_id"], body["client_secret"]) != (CLIENT_ID, CLIENT_SECRET):
                return _problem(401, "invalid_client", "클라이언트 인증에 실패했습니다.")
            subject = body.get("subject")
            if subject is not None:
                user = next((u for u in self.users.values() if u[1] == subject), None)
                if user is None or not user[6]:
                    return _problem(400, "invalid_grant", "대신할 수 없는 사용자")
            token = f"tok-{subject or 'service'}-{len(self.tokens)}"
            self.tokens[token] = subject
            expires_at = datetime.now(timezone.utc) + self.token_ttl
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer",
                                             "expires_in": int(self.token_ttl.total_seconds()),
                                             "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                             "subject": subject})  # fmt: skip
        who = self._auth(request)
        if isinstance(who, httpx.Response):
            return who
        if path == "/api/v2/users":
            items = [self._user_json(u) for u in self.users.values() if u[6]]
            return httpx.Response(200, json={"items": items, "page": {"limit": 100, "next_cursor": None, "has_more": False}})
        if path.startswith("/api/v2/users/"):
            user = self.users.get(int(path.rsplit("/", 1)[1]))
            if user is None:
                return _problem(404, "user_not_found", "사용자가 없습니다.")
            return httpx.Response(200, json=self._user_json(user))
        if path == "/api/v2/me":
            if who is None:
                return _problem(403, "user_token_required", "사용자 위임 토큰이 필요합니다.")
            user = next(u for u in self.users.values() if u[1] == who)
            return httpx.Response(200, json={**self._user_json(user), "permissions": user[7]})
        return _problem(404, "route_not_found", "Not Found")

    def client(self) -> ItsmAuthClient:
        return ItsmAuthClient("http://itsm.test", CLIENT_ID, CLIENT_SECRET, transport=httpx.MockTransport(self.handler))

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def make_settings(tmp_path: Path, api_key: str | None = None) -> Settings:
    return Settings(
        db_path=tmp_path / "agent.db",
        agent_history_db_path=tmp_path / "agent_history.db",
        static_dir=PROJECT_ROOT / "static",
        openai_api_key=api_key,
        itsm_base_url="http://itsm.test",
    )


class FakeModel(Model):
    """네트워크 없이 SDK Runner 를 실제로 돌리기 위한 모델. 호출 인자를 기록합니다."""

    def __init__(self, reply: str = "도구 연결이 아직 준비되지 않았습니다.", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs):
        self.calls.append(
            {
                "system_instructions": system_instructions,
                "input": input,
                "model_settings": model_settings,
                "tools": tools,
                "handoffs": handoffs,
            }
        )
        if self.error is not None:
            raise self.error
        message = ResponseOutputMessage(
            id=f"msg_{len(self.calls)}",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text=self.reply, annotations=[])],
        )
        return ModelResponse(output=[message], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def fake_itsm() -> FakeItsm:
    return FakeItsm()


@pytest.fixture
def client(settings: Settings, fake_itsm: FakeItsm) -> TestClient:
    """API 키가 없는 기본 구성."""
    with TestClient(create_app(settings, itsm_client=fake_itsm.client())) as c:
        yield c


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def agent_client(settings: Settings, fake_model: FakeModel, fake_itsm: FakeItsm) -> TestClient:
    """SDK Agent/Runner 는 그대로 쓰고 모델만 FakeModel 로 바꾼 구성."""
    service = AgentService(settings, model=fake_model)
    with TestClient(create_app(settings, agent_service=service, itsm_client=fake_itsm.client())) as c:
        yield c


def login(client: TestClient, username: str) -> dict:
    users = {u["username"]: u for u in client.get("/api/users").json()}
    res = client.post("/api/session", json={"user_id": users[username]["id"]})
    assert res.status_code == 200, res.text
    return res.json()["user"]
