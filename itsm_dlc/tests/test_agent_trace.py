"""Agent 실시간 Trace 로거(app/agent/trace.py) 검증."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import openai
from agents import ModelResponse, Usage
from fastapi.testclient import TestClient
from openai.types.responses import ResponseReasoningItem
from openai.types.responses.response_reasoning_item import Summary

from app.agent.service import AgentService
from app.agent.trace import AgentTraceHooks, AgentTraceWriter, resolve_trace_writer
from app.main import create_app
from tests.conftest import FakeItsm, FakeModel, login
from tests.test_ticket_read_integration import TicketReadModel


def test_trace_records_concise_llm_tool_request_execution_and_output(settings):
    fake_itsm = FakeItsm()
    model = TicketReadModel(arguments={"q": "vpn", "status": None, "limit": None}, reply="VPN 관련 티켓 1건입니다.")
    service = AgentService(settings, model=model)
    with TestClient(create_app(settings, agent_service=service, itsm_client=fake_itsm.client())) as client:
        login(client, "admin01")
        res = client.post("/api/agent/chat", json={"message": "VPN 티켓 조회해줘"})
        assert res.status_code == 200

    trace_path = settings.db_path.parent / "agent_trace.log"
    assert trace_path.exists()
    content = trace_path.read_text(encoding="utf-8")

    assert "[TURN START]" in content
    assert "VPN 티켓 조회해줘" in content
    assert "ticket_read tool 호출을 LLM이 요청" in content
    assert "ticket_read tool을 다음 인자로 실행:" in content
    assert '{ "q": "vpn" }' in content
    assert '"status": null' not in content  # null 인자는 생략됨
    assert "ticket_read tool 실행 결과 (Output → LLM에 전달됨" in content
    assert "LLM 최종 응답" in content
    assert "VPN 관련 티켓 1건입니다." in content
    assert "[TURN END]" in content and "presentation=ticket_list" in content


def test_trace_records_turn_error_and_redacts_secrets(settings):
    secret = "sk-secret-trace-canary-123456"
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    error = openai.BadRequestError(
        f"bad key {secret}",
        response=httpx.Response(400, request=request),
        body={"error": {"message": f"bad key {secret}"}},
    )
    service = AgentService(settings, model=FakeModel(error=error))
    with TestClient(create_app(settings, agent_service=service, itsm_client=FakeItsm().client())) as client:
        login(client, "admin01")
        res = client.post("/api/agent/chat", json={"message": f"키 테스트 {secret}"})
        assert res.status_code == 502

    content = (settings.db_path.parent / "agent_trace.log").read_text(encoding="utf-8")
    assert "[TURN ERROR]" in content
    assert "BadRequestError (HTTP 400)" in content
    assert secret not in content
    assert "sk-***" in content


def test_trace_can_be_disabled_via_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_TRACE", "0")
    writer = resolve_trace_writer(tmp_path)
    assert writer.enabled is False
    writer.prepare_file()
    writer.write_block("should not be written")
    assert not (tmp_path / "agent_trace.log").exists()


def test_trace_hooks_formats_reasoning_summary_and_skips_empty_reasoning(tmp_path: Path):
    trace_file = tmp_path / "custom_trace.log"
    writer = AgentTraceWriter(trace_file, enabled=True)
    hooks = AgentTraceHooks(
        writer,
        user_info="admin01",
        conversation_id="conv-12345678",
        user_message="질문",
        model_name="gpt-6-luna",
    )
    empty_reasoning = ResponseReasoningItem(id="rs_empty", type="reasoning", summary=[])
    asyncio.run(
        hooks.on_llm_end(
            None,
            None,
            ModelResponse(output=[empty_reasoning], usage=Usage(), response_id="resp_0"),
        )  # type: ignore[arg-type]
    )
    assert not trace_file.exists() or trace_file.read_text(encoding="utf-8") == ""

    reasoning_item = ResponseReasoningItem(
        id="rs_1",
        type="reasoning",
        summary=[Summary(type="summary_text", text="티켓 목록 조회가 필요합니다.")],
    )
    response = ModelResponse(
        output=[reasoning_item],
        usage=Usage(input_tokens=100, output_tokens=25),
        response_id="resp_1",
    )
    asyncio.run(hooks.on_llm_end(None, None, response))  # type: ignore[arg-type]
    text = trace_file.read_text(encoding="utf-8")
    assert "[Step 0 | Reasoning 요약]" in text
    assert "티켓 목록 조회가 필요합니다." in text
