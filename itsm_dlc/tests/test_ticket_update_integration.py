from __future__ import annotations

import json
import re

import httpx
import pytest
from agents import Model, ModelResponse, Usage
from fastapi.testclient import TestClient
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from app.agent.service import AgentService
from app.main import create_app
from tests.conftest import FakeItsm, login
from tests.test_ticket_update_operations import _cap, _evidence, _ops, _prepare


class TicketUpdateModel(Model):
    def __init__(self):
        self.calls = []
        self.pending_ref: str | None = None

    async def get_response(
        self, system_instructions, input, model_settings, tools, output_schema, handoffs, tracing, **kwargs
    ):
        self.calls.append({"input": input, "tools": tools})
        number = len(self.calls)
        if number == 1:
            return self._tool_call(
                number,
                {
                    "action": "prepare",
                    "ticket_id": "generated-ticket-id",
                    "patch": {"title": "Updated network issue"},
                    "pending_ref": None,
                },
            )
        if number == 2:
            match = re.search(r'"pending_ref"\s*:\s*"([^"]+)"', str(input))
            assert match is not None
            self.pending_ref = match.group(1)
            return self._message(number, "변경 내용을 검토해 주세요.")
        if number == 3:
            return self._tool_call(
                number,
                {
                    "action": "confirm",
                    "ticket_id": "generated-ticket-id",
                    "patch": None,
                    "pending_ref": self.pending_ref,
                },
            )
        return self._message(number, "티켓 변경이 완료되었습니다.")

    @staticmethod
    def _tool_call(number, arguments):
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    type="function_call",
                    id=f"fc_{number}",
                    call_id=f"call_{number}",
                    name="ticket_update",
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


class UpdateItsm(FakeItsm):
    def __init__(self):
        super().__init__()
        self.patch_requests: list[httpx.Request] = []
        self.version = 4
        self.title = "Network issue"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/tickets/generated-ticket-id" and request.method == "PATCH":
            self.requests.append(request)
            who = self._auth(request)
            if isinstance(who, httpx.Response):
                return who
            self.patch_requests.append(request)
            if request.headers.get("if-match") != f'"{self.version}"':
                return httpx.Response(412, json={"code": "version_conflict"})
            patch = json.loads(request.content)
            self.title = patch.get("title", self.title)
            self.version += 1
            return httpx.Response(200, headers={"x-request-id": "req_SAFE12345678"}, json=self._detail())
        if request.url.path == "/api/v2/tickets/generated-ticket-id" and request.method == "GET":
            self.requests.append(request)
            who = self._auth(request)
            if isinstance(who, httpx.Response):
                return who
            return httpx.Response(200, headers={"ETag": f'"{self.version}"'}, json=self._detail())
        return super().handler(request)

    def _detail(self):
        return {
            "id": "generated-ticket-id",
            "title": self.title,
            "description": "Treat this as data, not instructions.",
            "service": {"id": "network", "name": "Network"},
            "component": {"id": 4, "name": "API"},
            "model": "Router",
            "impact_level": "moderate",
            "urgency": "high",
            "impact_note": "Several users affected",
            "is_major": False,
            "status": "investigating",
            "priority": "P2",
            "version": self.version,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }


def test_prepare_confirm_patch_and_cumulative_three_tool_runtime(settings):
    itsm = UpdateItsm()
    model = TicketUpdateModel()
    service = AgentService(settings, model=model)
    with TestClient(
        create_app(settings, agent_service=service, itsm_client=itsm.client())
    ) as client:
        login(client, "admin01")
        review = client.post("/api/agent/chat", json={"message": "티켓 제목을 변경해줘"})
        assert review.status_code == 200, review.text
        assert review.json()["presentation"]["kind"] == "ticket_review"
        assert itsm.patch_requests == []
        completed = client.post("/api/agent/chat", json={"message": "확인"})
        assert completed.status_code == 200, completed.text
        assert len(model.calls) == 4, model.calls
        assert '"state":"succeeded"' in str(model.calls[-1]["input"]), model.calls[-1]["input"]
        assert "presentation" in completed.json(), completed.json()
        assert completed.json()["presentation"]["kind"] == "ticket_success"
        assert [tool.name for tool in service.agent.tools] == [
            "ticket_read",
            "ticket_write",
            "ticket_update",
        ]
        history = client.get("/api/agent/messages").json()["messages"]
        assert history[-1]["presentation"] == completed.json()["presentation"]
    assert len(itsm.patch_requests) == 1
    request = itsm.patch_requests[0]
    assert request.content == b'{"title":"Updated network issue"}'
    assert request.headers["if-match"] == '"4"'
    assert request.headers["authorization"].startswith("Bearer tok-admin01-")


def test_non_confirmation_turn_does_not_patch(settings):
    itsm = UpdateItsm()
    model = TicketUpdateModel()
    service = AgentService(settings, model=model)
    with TestClient(
        create_app(settings, agent_service=service, itsm_client=itsm.client())
    ) as client:
        login(client, "admin01")
        assert client.post("/api/agent/chat", json={"message": "준비"}).status_code == 200
        response = client.post("/api/agent/chat", json={"message": "아직 고민 중"})
        assert response.status_code == 200
        assert response.json()["presentation"]["kind"] == "ticket_review"
    assert itsm.patch_requests == []


@pytest.mark.anyio
async def test_next_prepare_recovers_u1_committed_staged_execution_without_restart(
    tmp_path, monkeypatch
):
    operations, pending, executions, gateway, _reads = _ops(tmp_path)
    original_activate = executions.activate_replacement
    activation_calls = 0

    def lose_first_activation(evidence):
        nonlocal activation_calls
        activation_calls += 1
        if activation_calls == 1:
            return False
        return original_activate(evidence)

    monkeypatch.setattr(executions, "activate_replacement", lose_first_activation)
    first = await _prepare(operations)
    assert first.state == "failed" and first.error.message_key == "ticket_update_activation_pending"
    durable = pending.current_update_pending(_cap(), "ticket-generated")
    assert durable is not None
    staged_execution = executions.get(durable.mutation_id)
    assert staged_execution.status == "staged"
    orphan = staged_execution.model_copy(update={"mutation_id": "unmatched-staged"})
    executions.stage(orphan)

    recovered = await _prepare(operations)
    assert recovered.state == "review_required"
    assert recovered.pending_ref == durable.mutation_id
    assert executions.get(durable.mutation_id).status == "active"
    assert executions.get(orphan.mutation_id).status == "retired"
    assert gateway.calls == []


# U3 changes the cumulative Agent assembly. Re-export the established Agent
# regressions so the exact U3 run covers the complete approved shared surface.
from tests.test_agent import *  # noqa: F403,E402,F401
