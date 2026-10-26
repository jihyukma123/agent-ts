"""ITSM 웹과 별도 Assistant 서비스 사이의 same-origin BFF 계약."""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from itsm_web.agent_client import AGENT_SESSION_COOKIE, AgentClient
from itsm_web.main import create_app
from tests.conftest import login, make_itsm_client, make_settings


class FakeAgent:
    def __init__(self) -> None:
        self.sessions: dict[str, int] = {}
        self.messages: dict[str, list[dict]] = {}
        self.switches: list[int] = []

    def _session(self, request: httpx.Request) -> str | None:
        prefix = f"{AGENT_SESSION_COOKIE}="
        for item in request.headers.get("cookie", "").split(";"):
            item = item.strip()
            if item.startswith(prefix):
                return item[len(prefix):]
        return None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        session = self._session(request)
        if path == "/api/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "itsm": {"reachable": True, "version": "2.4.0"},
                    "agent": {"configured": True, "model": "test-model", "reasoning_effort": "low"},
                },
            )
        if path == "/api/session" and request.method == "GET":
            if session not in self.sessions:
                return httpx.Response(401, json={"detail": {"code": "no_session", "message": "세션 없음"}})
            user_id = self.sessions[session]
            return httpx.Response(200, json={"user": {"id": user_id, "username": f"user-{user_id}"}})
        if path == "/api/session" and request.method == "POST":
            user_id = json.loads(request.content)["user_id"]
            session = f"agent-{user_id}-{len(self.switches) + 1}"
            self.sessions[session] = user_id
            self.messages[session] = []
            self.switches.append(user_id)
            return httpx.Response(
                200,
                json={"user": {"id": user_id, "username": f"user-{user_id}"}},
                headers={"set-cookie": f"{AGENT_SESSION_COOKIE}={session}; HttpOnly; Path=/; SameSite=lax"},
            )
        if session not in self.sessions:
            return httpx.Response(401, json={"detail": {"code": "no_session", "message": "세션 없음"}})
        if path == "/api/agent/messages" and request.method == "GET":
            return httpx.Response(200, json={"messages": self.messages[session]})
        if path == "/api/agent/messages" and request.method == "DELETE":
            self.messages[session] = []
            return httpx.Response(204)
        if path == "/api/agent/chat" and request.method == "POST":
            message = json.loads(request.content)["message"]
            presentation = None
            if message == "티켓 등록해줘":
                presentation = {
                    "version": 1,
                    "kind": "ticket_success",
                    "blocks": [
                        {
                            "type": "fields",
                            "fields": [
                                {
                                    "key": "ticket_id",
                                    "label": "티켓 ID",
                                    "value": {"text": "INC-037", "origin": "itsm_untrusted"},
                                }
                            ],
                        }
                    ],
                }
            self.messages[session].extend(
                [
                    {"role": "user", "text": message},
                    {"role": "assistant", "text": f"응답: {message}", "presentation": presentation},
                ]
            )
            return httpx.Response(
                200,
                json={
                    "reply": f"응답: {message}",
                    "context": {"user_id": self.sessions[session], "username": "test", "role": "requester"},
                    "presentation": presentation,
                },
            )
        return httpx.Response(404)


def test_assistant_bff_connects_chat_and_history_with_opaque_cookie(tmp_path):
    upstream = FakeAgent()
    agent = AgentClient("http://agent.test", transport=httpx.MockTransport(upstream))
    app = create_app(make_settings(tmp_path), make_itsm_client(tmp_path), agent)

    with TestClient(app) as client:
        user = login(client, "member01")
        health = client.get("/api/assistant/health").json()
        assert health == {
            "reachable": True,
            "configured": True,
            "model": "test-model",
            "reasoning_effort": "low",
            "itsm": {"reachable": True, "version": "2.4.0"},
        }

        connected = client.post("/api/assistant/session")
        assert connected.status_code == 200 and connected.json()["user"]["id"] == user["id"]
        assert client.cookies.get(AGENT_SESSION_COOKIE).startswith(f"agent-{user['id']}-")

        reply = client.post("/api/assistant/chat", json={"message": "내 티켓 보여줘"})
        assert reply.status_code == 200 and reply.json()["reply"] == "응답: 내 티켓 보여줘"
        assert client.get("/api/assistant/messages").json()["messages"][0]["role"] == "user"
        created = client.post("/api/assistant/chat", json={"message": "티켓 등록해줘"}).json()
        assert created["presentation"]["kind"] == "ticket_success"
        assert created["presentation"]["blocks"][0]["fields"][0]["value"]["text"] == "INC-037"
        assert client.delete("/api/assistant/messages").status_code == 204
        assert client.get("/api/assistant/messages").json() == {"messages": []}


def test_assistant_bff_rebinds_agent_session_to_current_web_user(tmp_path):
    upstream = FakeAgent()
    agent = AgentClient("http://agent.test", transport=httpx.MockTransport(upstream))
    app = create_app(make_settings(tmp_path), make_itsm_client(tmp_path), agent)

    with TestClient(app) as client:
        first = login(client, "member01")
        client.post("/api/assistant/session")
        second = login(client, "desk01")
        assert first["id"] != second["id"]

        assert client.get("/api/assistant/messages").status_code == 200
        assert upstream.switches == [first["id"], second["id"]]


def test_assistant_bff_requires_web_session_and_valid_message(tmp_path):
    upstream = FakeAgent()
    agent = AgentClient("http://agent.test", transport=httpx.MockTransport(upstream))
    app = create_app(make_settings(tmp_path), make_itsm_client(tmp_path), agent)

    with TestClient(app) as client:
        assert client.post("/api/assistant/session").status_code == 401
        login(client, "member01")
        invalid = client.post("/api/assistant/chat", json={"message": "   "})
        assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "invalid_message"


def test_assistant_health_reports_unreachable_without_breaking_itsm(tmp_path):
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    agent = AgentClient("http://agent.test", transport=httpx.MockTransport(unavailable))
    app = create_app(make_settings(tmp_path), make_itsm_client(tmp_path), agent)

    with TestClient(app) as client:
        assert client.get("/api/assistant/health").json() == {"reachable": False, "configured": False}
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200


def test_variants_keep_history_reset_and_user_binding_separate(tmp_path):
    before, after = FakeAgent(), FakeAgent()
    app = create_app(
        make_settings(tmp_path), make_itsm_client(tmp_path),
        AgentClient("http://before.test", transport=httpx.MockTransport(before)),
        AgentClient("http://after.test", transport=httpx.MockTransport(after)),
    )
    with TestClient(app) as client:
        first = login(client, "member01")
        for variant, message in (("baseline", "적용 전 질문"), ("harness", "적용 후 질문")):
            assert client.get(f"/api/assistant/health?variant={variant}").json()["reachable"]
            assert client.post(f"/api/assistant/chat?variant={variant}", json={"message": message}).status_code == 200
        assert client.cookies.get("itsm_harness_agent_session")
        assert client.cookies.get(AGENT_SESSION_COOKIE)
        assert client.get("/api/assistant/messages?variant=baseline").json()["messages"][0]["text"] == "적용 전 질문"
        assert client.get("/api/assistant/messages?variant=harness").json()["messages"][0]["text"] == "적용 후 질문"
        assert client.delete("/api/assistant/messages?variant=harness").status_code == 204
        assert client.get("/api/assistant/messages?variant=harness").json()["messages"] == []
        assert len(client.get("/api/assistant/messages?variant=baseline").json()["messages"]) == 2
        second = login(client, "desk01")
        for variant in ("baseline", "harness"):
            assert client.get(f"/api/assistant/messages?variant={variant}").json()["messages"] == []
        assert before.switches == after.switches == [first["id"], second["id"]]
        assert client.get("/api/assistant/health?variant=http://unknown.test").status_code == 422
        assert client.post("/api/assistant/chat?variant=unknown", json={"message": "hi"}).status_code == 422


def test_new_browser_does_not_inherit_shared_http_client_cookie(tmp_path):
    upstream = FakeAgent()
    app = create_app(make_settings(tmp_path), make_itsm_client(tmp_path),
                     AgentClient("http://agent.test", transport=httpx.MockTransport(upstream)))
    with TestClient(app) as client:
        login(client, "member01")
        client.post("/api/assistant/chat", json={"message": "첫 브라우저 대화"})
        cookie = client.cookies.get(AGENT_SESSION_COOKIE)
        client.cookies.clear()
        login(client, "member01")
        assert client.get("/api/assistant/messages").json()["messages"] == []
        assert client.cookies.get(AGENT_SESSION_COOKIE) != cookie


def test_harness_unavailable_does_not_break_baseline(tmp_path):
    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)

    app = create_app(
        make_settings(tmp_path), make_itsm_client(tmp_path),
        AgentClient("http://before.test", transport=httpx.MockTransport(FakeAgent())),
        AgentClient("http://after.test", transport=httpx.MockTransport(unavailable)),
    )
    with TestClient(app) as client:
        login(client, "member01")
        assert not client.get("/api/assistant/health?variant=harness").json()["reachable"]
        assert client.post("/api/assistant/session?variant=harness").status_code == 503
        assert client.post("/api/assistant/chat?variant=baseline", json={"message": "조회"}).status_code == 200
