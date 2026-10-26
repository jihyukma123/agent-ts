"""ITSM 웹: 사용자 전환·서버 측 세션·ITSM 위임 토큰, 티켓 중계 API, ITSM 장애 처리, 저장·초기화."""

from __future__ import annotations

import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from itsm_api.db import reset_db as reset_itsm_db
from itsm_web.db import reset_db
from itsm_web.itsm_client import ItsmClient
from itsm_web.main import create_app
from tests.conftest import PORTAL_NEW_TICKET, login, make_itsm_client, make_settings

ACTIVE_USERS = 18


def ids(res) -> list[str]:
    assert res.status_code == 200, res.text
    return sorted(item["id"] for item in res.json()["items"])


# ---------- 사용자 전환과 세션 ----------


def test_users_come_from_itsm_directory(client: TestClient):
    users = client.get("/api/users").json()
    assert len(users) == ACTIVE_USERS  # 비활성 사용자(desk03) 제외
    by_name = {u["username"]: u for u in users}
    assert by_name["member01"]["role"] == "requester" and by_name["member01"]["role_label"] == "요청자"
    assert by_name["idops01"]["team"] == "계정·인증팀"
    assert all(u["permissions"] is None for u in users)


@pytest.mark.parametrize(
    "username,role,can_create,can_resolve",
    [("member01", "requester", True, False), ("desk01", "agent_l1", True, False), ("api01", "agent_l2", True, True)],
)
def test_switch_user_sets_server_session(client: TestClient, username, role, can_create, can_resolve):
    user = login(client, username)
    assert user["role"] == role
    assert ("ticket:create" in user["permissions"]) is can_create
    assert ("ticket:resolve" in user["permissions"]) is can_resolve
    assert client.get("/api/session").json()["user"]["username"] == username


def test_session_stores_delegated_token_not_in_cookie(client: TestClient, settings):
    login(client, "member01")
    cookie = client.cookies.get("itsm_session")
    with sqlite3.connect(settings.db_path) as conn:
        token, expires = conn.execute("SELECT itsm_token, itsm_token_expires_at FROM sessions").fetchone()
    assert token and token not in cookie and expires.endswith("Z")
    assert token not in client.get("/api/session").text


def test_switch_between_users_invalidates_old_session(client: TestClient):
    login(client, "member01")
    old_cookie = client.cookies.get("itsm_session")
    login(client, "desk01")
    assert client.get("/api/session").json()["user"]["role"] == "agent_l1"
    other = TestClient(client.app)
    other.cookies.set("itsm_session", old_cookie)
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
        ("GET", "/api/tickets", None),
        ("GET", "/api/tickets/INC-001", None),
        ("GET", "/api/tickets/INC-001/worklogs", None),
        ("POST", "/api/tickets", PORTAL_NEW_TICKET),
        ("GET", "/api/catalog/services", None),
    ],
)
def test_protected_endpoints_require_session(client: TestClient, method, path, body):
    res = client.request(method, path, json=body)
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "no_session"


def test_forged_session_cookie_is_rejected(client: TestClient):
    client.cookies.set("itsm_session", "forged-session-id")
    assert client.get("/api/tickets").status_code == 401


def test_end_session(client: TestClient):
    login(client, "member01")
    assert client.delete("/api/session").status_code == 204
    assert client.get("/api/tickets").status_code == 401


def test_expired_delegated_token_is_refreshed(client: TestClient, settings):
    login(client, "member01")
    with sqlite3.connect(settings.db_path) as conn:
        old = conn.execute("SELECT itsm_token FROM sessions").fetchone()[0]
        conn.execute("UPDATE sessions SET itsm_token_expires_at = '2020-01-01T00:00:00Z'")
    assert client.get("/api/tickets").status_code == 200
    with sqlite3.connect(settings.db_path) as conn:
        new, expires = conn.execute("SELECT itsm_token, itsm_token_expires_at FROM sessions").fetchone()
    assert new != old and expires > "2026"


# ---------- 티켓 중계 ----------


def test_ticket_list_follows_itsm_visibility(client: TestClient):
    login(client, "member01")
    member = client.get("/api/tickets", params={"limit": 100}).json()
    assert len(member["items"]) == 12 and member["page"]["has_more"] is False
    login(client, "desk01")
    page = client.get("/api/tickets").json()
    assert len(page["items"]) == 30 and page["page"]["has_more"]
    rest = client.get("/api/tickets", params={"cursor": page["page"]["next_cursor"]}).json()
    assert len(rest["items"]) == 3


def test_ticket_search_and_status_filter(client: TestClient):
    login(client, "desk01")
    assert ids(client.get("/api/tickets", params={"q": "gpt-6-luna"})) == ["INC-002", "INC-003", "INC-031"]
    assert ids(client.get("/api/tickets", params={"status": ["pending"]})) == ["INC-007", "INC-022"]


def test_ticket_detail_and_worklogs(client: TestClient):
    login(client, "member01")
    t = client.get("/api/tickets/inc-001").json()
    assert t["id"] == "INC-001" and t["priority"] == "P2" and t["sla"]["policy"] == "P2 24x7"
    worklogs = client.get("/api/tickets/INC-001/worklogs").json()["items"]
    assert worklogs and {w["visibility"] for w in worklogs} == {"public"}


def test_itsm_errors_are_passed_through(client: TestClient):
    login(client, "member01")
    hidden = client.get("/api/tickets/INC-026")
    assert hidden.status_code == 404 and hidden.json()["detail"]["code"] == "ticket_not_found"
    invalid = client.post("/api/tickets", json={**PORTAL_NEW_TICKET, "component_id": 1})
    assert invalid.status_code == 422
    detail = invalid.json()["detail"]
    assert detail["code"] == "component_service_mismatch" and detail["errors"][0]["field"] == "component_id"


def test_create_ticket_through_web(client: TestClient):
    me = login(client, "member02")
    res = client.post("/api/tickets", json=PORTAL_NEW_TICKET, headers={"Idempotency-Key": "form-abc"})
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["id"] == "INC-037" and created["requester"]["id"] == me["id"]
    assert created["occurred_at"] == "2026-10-21T01:00:00Z"
    again = client.post("/api/tickets", json=PORTAL_NEW_TICKET, headers={"Idempotency-Key": "form-abc"})
    assert again.json()["id"] == "INC-037"
    assert client.get("/api/tickets/INC-037").json()["title"] == PORTAL_NEW_TICKET["title"]


def test_client_supplied_identity_is_ignored(client: TestClient):
    login(client, "member01")
    res = client.post("/api/tickets", json={**PORTAL_NEW_TICKET, "requester_id": 1})
    assert res.status_code == 422
    res = client.post("/api/tickets", json=PORTAL_NEW_TICKET, params={"user_id": 1}, headers={"X-User-Id": "1"})
    assert res.json()["requester"]["username"] == "member01"


def test_catalog_proxy(client: TestClient):
    login(client, "member01")
    services = client.get("/api/catalog/services").json()
    assert {s["id"] for s in services} == {"codex", "openai-api", "chatgpt", "sora", "image-gen", "sso"}
    assert "gpt-6-luna" in {m["id"] for m in client.get("/api/catalog/models").json()}


# ---------- ITSM 장애 ----------


def _unreachable_client() -> ItsmClient:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return ItsmClient("http://itsm.test", "portal", "portal-dev-secret", transport=httpx.MockTransport(fail))


def test_itsm_unreachable(tmp_path):
    with TestClient(create_app(make_settings(tmp_path), itsm_client=_unreachable_client())) as c:
        health = c.get("/api/health").json()
        assert health["status"] == "ok" and health["itsm"]["reachable"] is False
        res = c.get("/api/users")
        assert res.status_code == 503 and res.json()["detail"]["code"] == "itsm_unavailable"
        assert c.post("/api/session", json={"user_id": 1}).status_code == 503


def test_wrong_client_secret_is_reported(tmp_path):
    itsm = make_itsm_client(tmp_path)
    itsm._client_secret = "wrong"
    with TestClient(create_app(make_settings(tmp_path), itsm_client=itsm)) as c:
        res = c.get("/api/users")
    assert res.status_code == 502 and res.json()["detail"]["code"] == "itsm_auth_failed"


# ---------- 저장 유지와 초기화 ----------


def test_data_persists_across_restart(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, itsm_client=make_itsm_client(tmp_path))) as c:
        login(c, "member01")
        assert c.post("/api/tickets", json=PORTAL_NEW_TICKET).status_code == 201
        cookie = c.cookies.get("itsm_session")

    with TestClient(create_app(settings, itsm_client=make_itsm_client(tmp_path))) as c:  # 같은 DB 파일로 재시작
        c.cookies.set("itsm_session", cookie)  # 서버 측 세션도 유지됩니다.
        assert c.get("/api/session").json()["user"]["username"] == "member01"
        assert c.get("/api/tickets/INC-037").json()["title"] == PORTAL_NEW_TICKET["title"]


def test_reset_restores_identical_seed(tmp_path, settings, client: TestClient):
    login(client, "admin01")
    before = client.get("/api/tickets/INC-001").json()
    assert client.post("/api/tickets", json=PORTAL_NEW_TICKET).status_code == 201

    reset_itsm_db(tmp_path / "itsm.db")
    reset_db(settings.db_path)

    assert client.get("/api/tickets").status_code == 401  # 세션도 초기화됨
    login(client, "admin01")
    assert client.get("/api/tickets/INC-037").status_code == 404
    after = client.get("/api/tickets/INC-001").json()
    assert {k: v for k, v in after.items() if k != "sla"} == {k: v for k, v in before.items() if k != "sla"}
    assert client.post("/api/tickets", json=PORTAL_NEW_TICKET).json()["id"] == "INC-037"


# ---------- 상태·화면 ----------


def test_health(client: TestClient):
    assert client.get("/api/health").json() == {
        "status": "ok",
        "itsm": {"base_url": "http://itsm.test", "reachable": True, "version": "2.4.0"},
    }


def test_static_ui_served(client: TestClient):
    res = client.get("/")
    assert res.status_code == 200 and "<title>ITSM</title>" in res.text and 'id="ticket-form"' in res.text
    assert 'id="assistant-panel"' in res.text and 'id="chat-form"' in res.text
    assert 'id="chat-history"' in res.text and 'id="chat-announcer"' in res.text
    assert 'id="agent-ticket-notice"' in res.text and 'id="agent-ticket-view"' in res.text
    for word in ("모의", "가상", "Mock"):
        assert word not in res.text
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert '$("chat-history").append(message);' in script.text
    assert '$("chat-history").append(pending);' in script.text
    assert '$(announce ? "chat-live" : "chat-history")' not in script.text
    assert "function agentCreatedTicketId(presentation)" in script.text
    assert 'field.key === "new_version"' in script.text
    assert "showAgentTicketNotice(createdTicketId)" in script.text
