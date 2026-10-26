"""사용자 전환·서버 측 세션·ITSM 위임 토큰, 세션 필수, ITSM 연결 실패."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.itsm_auth import ItsmAuthClient
from app.main import create_app
from tests.conftest import FakeItsm, login, make_settings


def test_users_come_from_itsm_directory(client: TestClient):
    users = client.get("/api/users").json()
    assert [u["username"] for u in users] == ["admin01", "member01", "desk01"]  # 비활성 사용자 제외
    assert users[2]["role_label"] == "서비스데스크(L1)" and users[2]["team"] == "서비스데스크"
    assert all(u["permissions"] is None for u in users)


@pytest.mark.parametrize("username,role", [("admin01", "admin"), ("member01", "requester"), ("desk01", "agent_l1")])
def test_switch_user_sets_server_session(client: TestClient, username, role):
    user = login(client, username)
    assert user["role"] == role and user["permissions"]
    assert client.get("/api/session").json()["user"]["username"] == username


def test_session_uses_agent_client_and_stores_token_server_side(client: TestClient, settings, fake_itsm: FakeItsm):
    login(client, "member01")
    cookie = client.cookies.get("itsm_agent_session")
    with sqlite3.connect(settings.db_path) as conn:
        token = conn.execute("SELECT itsm_token FROM sessions").fetchone()[0]
    assert fake_itsm.tokens[token] == "member01"
    assert token not in cookie and token not in client.get("/api/session").text


def test_switch_between_users_invalidates_old_session(client: TestClient):
    login(client, "member01")
    old_cookie = client.cookies.get("itsm_agent_session")
    login(client, "desk01")
    other = TestClient(client.app)
    other.cookies.set("itsm_agent_session", old_cookie)
    assert other.get("/api/session").status_code == 401


def test_switch_to_unknown_or_inactive_user(client: TestClient):
    res = client.post("/api/session", json={"user_id": 999})
    assert res.status_code == 404 and res.json()["detail"]["code"] == "user_not_found"
    inactive = client.post("/api/session", json={"user_id": 17})
    assert inactive.status_code == 409 and inactive.json()["detail"]["code"] == "user_inactive"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/session", None),
        ("POST", "/api/agent/chat", {"message": "안녕"}),
        ("GET", "/api/agent/messages", None),
        ("DELETE", "/api/agent/messages", None),
    ],
)
def test_protected_endpoints_require_session(client: TestClient, method, path, body):
    res = client.request(method, path, json=body)
    assert res.status_code == 401 and res.json()["detail"]["code"] == "no_session"


def test_forged_session_cookie_is_rejected(client: TestClient):
    client.cookies.set("itsm_agent_session", "forged-session-id")
    assert client.get("/api/session").status_code == 401


def test_end_session(client: TestClient):
    login(client, "member01")
    assert client.delete("/api/session").status_code == 204
    assert client.get("/api/session").status_code == 401


@pytest.mark.anyio
async def test_itsm_token_for_refreshes_expiring_token(tmp_path):
    from fastapi import Request

    from app.db import open_db
    from app.session import get_current_user, itsm_token_for

    fake = FakeItsm(token_ttl=timedelta(seconds=30))  # 갱신 기준(60초)보다 짧은 토큰
    settings = make_settings(tmp_path)
    app = create_app(settings, itsm_client=fake.client())
    with TestClient(app) as c:
        login(c, "member01")
        session_id = c.cookies.get("itsm_agent_session")
        request = Request({"type": "http", "app": app, "headers": [(b"cookie", f"itsm_agent_session={session_id}".encode())]})
        with open_db(settings.db_path) as conn:
            user = get_current_user(request, conn)
        new_token = await itsm_token_for(request, user)
    assert new_token != user.itsm_token and fake.tokens[new_token] == "member01"
    with open_db(settings.db_path) as conn:
        assert conn.execute("SELECT itsm_token FROM sessions").fetchone()[0] == new_token


def _unreachable_client() -> ItsmAuthClient:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return ItsmAuthClient("http://itsm.test", "itsm-agent", "x", transport=httpx.MockTransport(fail))


def test_itsm_unreachable(tmp_path):
    with TestClient(create_app(make_settings(tmp_path), itsm_client=_unreachable_client())) as c:
        health = c.get("/api/health").json()
        assert health["status"] == "ok" and health["itsm"]["reachable"] is False
        res = c.get("/api/users")
        assert res.status_code == 503 and res.json()["detail"]["code"] == "itsm_unavailable"


def test_wrong_client_secret_is_reported(tmp_path):
    bad = ItsmAuthClient("http://itsm.test", "itsm-agent", "wrong", transport=httpx.MockTransport(FakeItsm().handler))
    with TestClient(create_app(make_settings(tmp_path), itsm_client=bad)) as c:
        res = c.get("/api/users")
    assert res.status_code == 502 and res.json()["detail"]["code"] == "itsm_auth_failed"


def test_health_and_ui(client: TestClient):
    health = client.get("/api/health").json()
    assert health["itsm"] == {"base_url": "http://itsm.test", "reachable": True, "version": "2.4.0"}
    assert health["agent"] == {"configured": False, "model": "gpt-6-luna", "reasoning_effort": "high"}
    page = client.get("/")
    assert page.status_code == 200 and "<title>ITSM Assistant</title>" in page.text and 'id="chat-form"' in page.text
    assert client.get("/static/app.js").status_code == 200
