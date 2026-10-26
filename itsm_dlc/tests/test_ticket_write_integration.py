from __future__ import annotations

import json
import re
from dataclasses import replace

import httpx
from agents import Model, ModelResponse, Usage
from fastapi.testclient import TestClient
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from app.agent.service import AgentService
import app.agent.service as service_module
from app.config import RefreshSigningKeyRingConfig
from app.db import open_db
from app.main import create_app
from tests.conftest import FakeItsm, login


class TicketWriteModel(Model):
    def __init__(self):
        self.calls = []
        self.pending_ref = None

    async def get_response(
        self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs
    ):
        self.calls.append({"input": input, "tools": tools})
        number = len(self.calls)
        if number == 1:
            arguments = {
                "action": "prepare",
                "draft": {
                    "title": "Generated login issue",
                    "description": "Several generated users cannot sign in",
                    "service": {"name": "Support"},
                    "component": {"name": "API"},
                    "model": {"name": "Laptop"},
                    "impact_level": "moderate",
                    "urgency": "high",
                    "impact_note": "Generated work is delayed",
                    "occurred_at": {"kind": "now"},
                    "classification": "standard",
                },
                "pending_ref": None,
            }
            return self._tool_call(number, arguments)
        if number == 2:
            match = re.search(r'"pending_ref"\s*:\s*"([^"]+)"', str(input))
            assert match is not None
            self.pending_ref = match.group(1)
            return self._message(number, "등록 내용을 검토해 주세요.")
        if number == 3:
            return self._tool_call(
                number, {"action": "confirm", "draft": None, "pending_ref": self.pending_ref}
            )
        return self._message(number, "티켓 등록이 완료되었습니다.")

    @staticmethod
    def _tool_call(number, arguments):
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    type="function_call",
                    id=f"fc_{number}",
                    call_id=f"call_{number}",
                    name="ticket_write",
                    arguments=json.dumps(arguments),
                    status="completed",
                )
            ],
            usage=Usage(),
            response_id=None,
        )

    @staticmethod
    def _message(number, text):
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"msg_{number}",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
                )
            ],
            usage=Usage(),
            response_id=None,
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def test_review_exact_confirmation_create_presentation_history_and_restart(settings):
    itsm = FakeItsm()
    model = TicketWriteModel()
    service = AgentService(settings, model=model)
    with TestClient(create_app(settings, agent_service=service, itsm_client=itsm.client())) as client:
        login(client, "admin01")
        review = client.post("/api/agent/chat", json={"message": "새 티켓을 준비해줘"})
        assert review.status_code == 200, review.text
        assert review.json()["presentation"]["kind"] == "ticket_review"
        assert itsm.created_tickets == []
        completed = client.post("/api/agent/chat", json={"message": "confirm"})
        assert completed.status_code == 200, completed.text
        assert completed.json()["presentation"]["kind"] == "ticket_success"
        assert len(itsm.created_tickets) == 1
        body, key = itsm.created_tickets[0]
        assert json.loads(body)["title"] == "Generated login issue" and key
        assert itsm.token_prefix not in completed.text
        cookie = client.cookies.get("itsm_agent_session")
        history = client.get("/api/agent/messages").json()["messages"]
        assert history[-1]["presentation"] == completed.json()["presentation"]

    restarted = AgentService(settings, model=TicketWriteModel())
    restarted_itsm = FakeItsm()
    with TestClient(
        create_app(settings, agent_service=restarted, itsm_client=restarted_itsm.client())
    ) as client:
        client.cookies.set("itsm_agent_session", cookie)
        restored = client.get("/api/agent/messages")
        assert restored.status_code == 200
        assert restored.json()["messages"][-1]["presentation"]["kind"] == "ticket_success"


def test_old_user_cannot_confirm_previous_users_pending_review(settings):
    itsm = FakeItsm()
    model = TicketWriteModel()
    service = AgentService(settings, model=model)
    with TestClient(create_app(settings, agent_service=service, itsm_client=itsm.client())) as client:
        login(client, "admin01")
        assert client.post("/api/agent/chat", json={"message": "prepare"}).status_code == 200
        old_ref = model.pending_ref
        login(client, "member01")
        model.calls = [{}, {}]
        model.pending_ref = old_ref
        response = client.post("/api/agent/chat", json={"message": "confirm"})
        assert response.status_code == 200
        assert itsm.created_tickets == []
        assert response.json()["presentation"]["kind"] == "ticket_input"


class TokenExpiredOnceItsm(FakeItsm):
    def __init__(self):
        super().__init__()
        self.ticket_requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v2/tickets" or request.method != "POST":
            return super().handler(request)
        self.requests.append(request)
        who = self._auth(request)
        if isinstance(who, httpx.Response):
            return who
        self.ticket_requests.append(
            (request.content, request.headers.get("idempotency-key", ""))
        )
        if len(self.ticket_requests) == 1:
            return httpx.Response(
                401,
                json={"status": 401, "code": "token_expired", "detail": "expired"},
                headers={"content-type": "application/problem+json"},
            )
        self.created_tickets.append(self.ticket_requests[-1])
        return httpx.Response(201, json={"id": "generated-recovered-ticket"})


def test_production_runtime_fails_closed_when_refresh_key_contract_is_unconfigured(settings):
    itsm = TokenExpiredOnceItsm()
    first_model = TicketWriteModel()
    first_service = AgentService(settings, model=first_model)

    with TestClient(
        create_app(settings, agent_service=first_service, itsm_client=itsm.client())
    ) as client:
        login(client, "admin01")
        review = client.post("/api/agent/chat", json={"message": "새 티켓을 준비해줘"})
        assert review.status_code == 200
        result = client.post("/api/agent/chat", json={"message": "confirm"})
        assert result.status_code == 200
        assert result.json()["presentation"]["kind"] == "ticket_auth_error"

    with open_db(settings.db_path) as conn:
        failed = conn.execute(
            "SELECT * FROM ticket_create_attempts WHERE status='terminal_failure'"
        ).fetchone()
        assert failed is not None
        assert failed["last_outcome"] == "refresh_contract_unavailable"
        assert failed["refresh_reservation_id"] is None
        assert failed["refresh_state"] == "unused"

    refresh_count = sum(
        request.url.path == "/api/v2/auth/token"
        and json.loads(request.content).get("subject") == "admin01"
        for request in itsm.requests
    ) - 1
    assert refresh_count == 0
    assert len(itsm.ticket_requests) == 1


def test_configured_key_ring_refreshes_once_and_retries_same_attempt(settings):
    configured = replace(
        settings,
        refresh_signing_key_ring=RefreshSigningKeyRingConfig(
            "kid-current", {"kid-current": b"k" * 32}, 24 * 60 * 60
        ),
    )
    itsm = TokenExpiredOnceItsm()
    service = AgentService(configured, model=TicketWriteModel())
    with TestClient(create_app(configured, agent_service=service, itsm_client=itsm.client())) as client:
        assert service.ticket_create_operations is not None
        assert (
            service.ticket_create_operations.active_refresh_reservations
            is service._active_refresh_reservations
        )
        login(client, "admin01")
        assert client.post("/api/agent/chat", json={"message": "새 티켓을 준비해줘"}).status_code == 200
        result = client.post("/api/agent/chat", json={"message": "confirm"})
        assert result.status_code == 200
        assert result.json()["presentation"]["kind"] == "ticket_success"

    with open_db(configured.db_path) as conn:
        row = conn.execute("SELECT * FROM ticket_create_attempts").fetchone()
        session = conn.execute("SELECT itsm_credential_generation FROM sessions").fetchone()
    assert row["status"] == "completed" and row["refresh_state"] == "committed"
    assert row["refresh_base_credential_generation"] == 0
    assert row["refresh_committed_credential_generation"] == 1
    assert session[0] == 1
    assert len(itsm.ticket_requests) == 2
    assert itsm.ticket_requests[0] == itsm.ticket_requests[1]


def test_restart_recovers_committed_generation_without_second_exchange(settings, monkeypatch):
    configured = replace(
        settings,
        refresh_signing_key_ring=RefreshSigningKeyRingConfig(
            "kid-current", {"kid-current": b"k" * 32}, 24 * 60 * 60
        ),
    )
    itsm = TokenExpiredOnceItsm()
    first_model = TicketWriteModel()
    service = AgentService(configured, model=first_model)
    original_refresh = service_module.refresh_itsm_credential_for

    async def commit_then_lose_response(request, user):
        await original_refresh(request, user)
        raise RuntimeError("simulated response loss after credential commit")

    with TestClient(create_app(configured, agent_service=service, itsm_client=itsm.client())) as client:
        login(client, "admin01")
        assert client.post("/api/agent/chat", json={"message": "새 티켓을 준비해줘"}).status_code == 200
        pending_ref = first_model.pending_ref
        cookie = client.cookies.get("itsm_agent_session")
        monkeypatch.setattr(service_module, "refresh_itsm_credential_for", commit_then_lose_response)
        lost = client.post("/api/agent/chat", json={"message": "confirm"})
        assert lost.status_code == 200
        assert lost.json()["presentation"]["kind"] == "ticket_auth_error"

    with open_db(configured.db_path) as conn:
        reserved = conn.execute("SELECT * FROM ticket_create_attempts").fetchone()
        assert reserved["refresh_state"] == "reserved"
        assert conn.execute("SELECT itsm_credential_generation FROM sessions").fetchone()[0] == 1

    monkeypatch.setattr(service_module, "refresh_itsm_credential_for", original_refresh)
    restarted_model = TicketWriteModel()
    restarted_model.calls = [{}, {}]
    restarted_model.pending_ref = pending_ref
    restarted = AgentService(configured, model=restarted_model)
    with TestClient(create_app(configured, agent_service=restarted, itsm_client=itsm.client())) as client:
        client.cookies.set("itsm_agent_session", cookie)
        recovered = client.post("/api/agent/chat", json={"message": "confirm"})
        assert recovered.status_code == 200
        assert recovered.json()["presentation"]["kind"] == "ticket_success"

    refresh_count = sum(
        request.url.path == "/api/v2/auth/token"
        and json.loads(request.content).get("subject") == "admin01"
        for request in itsm.requests
    ) - 1
    with open_db(configured.db_path) as conn:
        completed = conn.execute("SELECT * FROM ticket_create_attempts").fetchone()
    assert refresh_count == 1
    assert completed["refresh_state"] == "committed"
    assert completed["status"] == "completed"
    assert len(itsm.ticket_requests) == 2
    assert itsm.ticket_requests[0] == itsm.ticket_requests[1]
