"""ITSM API 인증·토큰·권한·요청 한도·오류 형식."""

from __future__ import annotations

from fastapi.testclient import TestClient

from itsm_api.main import create_app as create_itsm_app
from tests.conftest import NOW, PORTAL_CLIENT, ItsmApi, make_itsm_settings
from itsm_api.timeutil import FixedClock


def _token(api: ItsmApi, **body):
    return api.client.post("/api/v2/auth/token", json=body)


def test_token_exchange_issues_delegated_token(api: ItsmApi):
    res = _token(api, grant_type="token_exchange", client_id="portal", client_secret="portal-dev-secret", subject="desk01")
    assert res.status_code == 200
    body = res.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 900 and body["subject"] == "desk01"
    me = api.client.get("/api/v2/me", headers={"Authorization": f"Bearer {body['access_token']}"}).json()
    assert me["username"] == "desk01" and me["role"] == "agent_l1" and me["team"]["slug"] == "service-desk"
    assert "ticket:assign" in me["permissions"] and "ticket:resolve" not in me["permissions"]


def test_invalid_client_and_grants(api: ItsmApi):
    bad = _token(api, grant_type="client_credentials", client_id="portal", client_secret="wrong")
    assert bad.status_code == 401 and bad.json()["code"] == "invalid_client"
    no_delegate = _token(api, grant_type="token_exchange", client_id="status-board",
                         client_secret="status-board-dev-secret", subject="member01")  # fmt: skip
    assert no_delegate.status_code == 403 and no_delegate.json()["code"] == "delegation_not_allowed"
    inactive = _token(api, grant_type="token_exchange", client_id="portal", client_secret="portal-dev-secret", subject="desk03")
    assert inactive.status_code == 400 and inactive.json()["code"] == "invalid_grant"
    missing_subject = _token(api, grant_type="token_exchange", client_id="portal", client_secret="portal-dev-secret")
    assert missing_subject.status_code == 400


def test_missing_invalid_and_expired_tokens(api: ItsmApi, clock: FixedClock):
    assert api.client.get("/api/v2/tickets").json()["code"] == "missing_token"
    res = api.client.get("/api/v2/tickets", headers={"Authorization": "Bearer nope"})
    assert res.status_code == 401 and res.json()["code"] == "invalid_token"
    assert res.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'

    headers = api.headers("member01")
    assert api.client.get("/api/v2/tickets", headers=headers).status_code == 200
    clock.advance(seconds=901)
    expired = api.client.get("/api/v2/tickets", headers=headers)
    assert expired.status_code == 401 and expired.json()["code"] == "token_expired"


def test_service_token_is_limited_to_catalog_and_directory(api: ItsmApi):
    assert api.get("/api/v2/catalog/services").status_code == 200
    assert api.get("/api/v2/users").status_code == 200
    res = api.get("/api/v2/tickets")
    assert res.status_code == 403 and res.json()["code"] == "permission_denied"
    assert res.json()["required_permissions"] == ["ticket:read"]
    assert api.get("/api/v2/me").json()["code"] == "user_token_required"


def test_problem_json_shape(api: ItsmApi):
    res = api.get("/api/v2/tickets/INC-999", "member01", headers={"X-Request-ID": "req-123"})
    assert res.status_code == 404
    assert res.headers["content-type"] == "application/problem+json"
    assert res.headers["X-Request-ID"] == "req-123"
    body = res.json()
    assert body == {
        "type": "https://itsm.internal/problems/ticket_not_found",
        "title": "Not Found",
        "status": 404,
        "code": "ticket_not_found",
        "detail": "티켓 INC-999 을(를) 찾을 수 없습니다.",
        "instance": "/api/v2/tickets/INC-999",
        "request_id": "req-123",
        "ticket_id": "INC-999",
    }


def test_validation_errors_use_problem_json(api: ItsmApi):
    res = api.post("/api/v2/tickets", "member01", json={"title": ""})
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "validation_failed"
    fields = {e["field"] for e in body["errors"]}
    assert {"title", "description", "service_id", "impact_level", "urgency", "impact_note", "occurred_at"} <= fields


def test_unknown_route_is_problem_json(api: ItsmApi):
    res = api.get("/api/v2/nothing", "member01")
    assert res.status_code == 404 and res.json()["code"] == "route_not_found"


def test_rate_limit(tmp_path):
    app = create_itsm_app(make_itsm_settings(tmp_path, rate_limit_per_minute=3), FixedClock(NOW))
    with TestClient(app) as c:
        api = ItsmApi(c)
        headers = api.headers("member01")
        statuses = [c.get("/api/v2/catalog/models", headers=headers) for _ in range(4)]
    assert [r.status_code for r in statuses] == [200, 200, 200, 429]
    assert statuses[0].headers["X-RateLimit-Limit"] == "3"
    limited = statuses[-1]
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["retry_after_seconds"] == int(limited.headers["Retry-After"])


def test_rate_limit_is_per_principal(tmp_path):
    app = create_itsm_app(make_itsm_settings(tmp_path, rate_limit_per_minute=1), FixedClock(NOW))
    with TestClient(app) as c:
        api = ItsmApi(c)
        assert c.get("/api/v2/catalog/models", headers=api.headers("member01")).status_code == 200
        assert c.get("/api/v2/catalog/models", headers=api.headers("member02")).status_code == 200
        assert c.get("/api/v2/catalog/models", headers=api.headers("member01")).status_code == 429


def test_portal_client_credentials_constant():
    assert PORTAL_CLIENT == ("portal", "portal-dev-secret")


def test_agent_client_can_delegate(api: ItsmApi):
    res = _token(api, grant_type="token_exchange", client_id="itsm-agent", client_secret="itsm-agent-dev-secret", subject="member01")
    assert res.status_code == 200
    me = api.client.get("/api/v2/me", headers={"Authorization": f"Bearer {res.json()['access_token']}"}).json()
    assert me["username"] == "member01" and me["role"] == "requester"


def test_api_docs_are_not_public_by_default(api: ItsmApi, tmp_path):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert api.client.get(path).status_code == 404
    app = create_itsm_app(make_itsm_settings(tmp_path / "docs", enable_docs=True), FixedClock(NOW))
    with TestClient(app) as c:
        assert c.get("/openapi.json").status_code == 200 and c.get("/docs").status_code == 200
