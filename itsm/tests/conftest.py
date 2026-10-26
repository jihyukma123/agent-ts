from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from itsm_api.config import ItsmApiSettings
from itsm_api.main import create_app as create_itsm_app
from itsm_api.timeutil import Clock, FixedClock
from itsm_web.config import PROJECT_ROOT, Settings
from itsm_web.itsm_client import ItsmClient
from itsm_web.main import create_app

# ITSM API 테스트의 기준 시각: 2026-10-26(월) 10:00 KST
NOW = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)

PORTAL_CLIENT = ("portal", "portal-dev-secret")


# ---------- ITSM API ----------


class ItsmApi:
    """ITSM API 테스트 클라이언트. 사용자별 위임 토큰 헤더를 만들어 줍니다."""

    def __init__(self, client: TestClient):
        self.client = client
        self._tokens: dict[str | None, str] = {}

    def token(self, username: str | None = None, *, fresh: bool = False) -> str:
        if fresh or username not in self._tokens:
            body = {"grant_type": "token_exchange" if username else "client_credentials",
                    "client_id": PORTAL_CLIENT[0], "client_secret": PORTAL_CLIENT[1]}  # fmt: skip
            if username:
                body["subject"] = username
            res = self.client.post("/api/v2/auth/token", json=body)
            assert res.status_code == 200, res.text
            self._tokens[username] = res.json()["access_token"]
        return self._tokens[username]

    def headers(self, username: str | None = None, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(username)}", **extra}

    def request(self, method: str, path: str, username: str | None = None, *, headers: dict | None = None, **kwargs):
        res = self.client.request(method, path, headers={**self.headers(username), **(headers or {})}, **kwargs)
        if res.status_code == 401 and res.headers.get("content-type", "").startswith("application/problem+json") \
                and res.json()["code"] == "token_expired":
            # 테스트 시계를 앞당기면 캐시한 토큰이 만료되므로 한 번 다시 발급받습니다.
            self.token(username, fresh=True)
            res = self.client.request(method, path, headers={**self.headers(username), **(headers or {})}, **kwargs)
        return res

    def get(self, path: str, username: str | None = None, **kwargs):
        return self.request("GET", path, username, **kwargs)

    def post(self, path: str, username: str | None = None, **kwargs):
        return self.request("POST", path, username, **kwargs)

    def patch(self, path: str, username: str | None = None, **kwargs):
        return self.request("PATCH", path, username, **kwargs)

    def put(self, path: str, username: str | None = None, **kwargs):
        return self.request("PUT", path, username, **kwargs)

    def version(self, ticket_id: str, username: str = "admin01") -> str:
        return f'"{self.get(f"/api/v2/tickets/{ticket_id}", username).json()["version"]}"'


def make_itsm_settings(tmp_path: Path, **kwargs: Any) -> ItsmApiSettings:
    return ItsmApiSettings(db_path=tmp_path / "itsm.db", export_step_seconds=0, **kwargs)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def itsm_settings(tmp_path: Path) -> ItsmApiSettings:
    return make_itsm_settings(tmp_path)


@pytest.fixture
def api(itsm_settings: ItsmApiSettings, clock: FixedClock) -> ItsmApi:
    with TestClient(create_itsm_app(itsm_settings, clock)) as c:
        yield ItsmApi(c)


NEW_TICKET = {
    "title": "Sora 대기열 작업 상태가 갱신되지 않음",
    "description": "영상 생성 요청이 대기열에서 오래 머무름.",
    "service_id": "sora",
    "component_id": 15,
    "impact_level": "moderate",
    "urgency": "medium",
    "impact_note": "영상 생성을 요청한 일부 사용자",
    "occurred_at": "2026-10-26T09:30:00+09:00",
}


# ---------- ITSM 웹 ----------


def make_settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "web.db", static_dir=PROJECT_ROOT / "static", itsm_base_url="http://itsm.test")


def make_itsm_client(tmp_path: Path, clock: Clock | None = None) -> ItsmClient:
    """같은 프로세스의 ITSM API 앱에 HTTP 로 연결된 클라이언트 (네트워크 없음)."""
    itsm_app = create_itsm_app(make_itsm_settings(tmp_path), clock or Clock())
    return ItsmClient("http://itsm.test", *PORTAL_CLIENT, transport=httpx.ASGITransport(app=itsm_app))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(settings: Settings, tmp_path: Path) -> TestClient:
    with TestClient(create_app(settings, itsm_client=make_itsm_client(tmp_path))) as c:
        yield c


def login(client: TestClient, username: str) -> dict:
    users = {u["username"]: u for u in client.get("/api/users").json()}
    res = client.post("/api/session", json={"user_id": users[username]["id"]})
    assert res.status_code == 200, res.text
    return res.json()["user"]


PORTAL_NEW_TICKET = {
    "title": "Sora 대기열 작업 상태가 갱신되지 않음",
    "description": "영상 생성 요청이 대기열에서 오래 머무름.",
    "service_id": "sora",
    "component_id": 15,
    "impact_level": "moderate",
    "urgency": "medium",
    "impact_note": "영상 생성을 요청한 일부 사용자",
    "occurred_at": "2026-10-21T10:00:00+09:00",
}
