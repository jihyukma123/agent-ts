"""OpenAI Agents SDK Agent 정의와 U1 도구 registry 조립."""

from __future__ import annotations

from agents import Agent, Model, ModelSettings, Tool
from openai.types.shared import Reasoning

from app.agent.context import AgentUserContext
from app.config import AGENT_REASONING_EFFORT
from app.ticketing.presentation import ticket_read_presentation
from app.ticketing.registry import RegistryV1, TicketPluginV1
from app.ticketing.tools import build_ticket_read_tool

def build_u1_registry() -> RegistryV1:
    """Return the independently reviewable U1 assembly: exactly ticket_read."""
    registry = RegistryV1()
    registry.register_plugin(TicketPluginV1(
        contract_version=1,
        tool_name="ticket_read",
        function_tool_factory=build_ticket_read_tool,
        presentation_mappers={
            kind: ticket_read_presentation
            for kind in (
                "ticket_list",
                "ticket_detail",
                "ticket_conflict",
            )
        },
    ))
    return registry


def build_runtime_registry(
    *, include_ticket_write: bool = True, include_ticket_update: bool = True
) -> RegistryV1:
    """Compose later-Unit plugins without changing the U1 checkpoint contract."""
    registry = build_u1_registry()
    if include_ticket_write:
        from app.ticketing.create_tools import ticket_write_plugin

        registry.register_plugin(ticket_write_plugin())
    if include_ticket_update:
        from app.ticketing.update_tools import ticket_update_plugin

        registry.register_plugin(ticket_update_plugin())
    return registry


U1_REGISTRY = build_u1_registry()
U1_TOOLS: list[Tool] = list(U1_REGISTRY.build_tools())
REGISTRY = build_runtime_registry()
TOOLS: list[Tool] = list(REGISTRY.build_tools())

INSTRUCTIONS = "당신은 사내 IT 서비스 관리(ITSM) 시스템을 쓰는 동료들을 돕는 친절한 도우미 친구입니다. 한국어로 간결하게 답합니다."


def _agent_with_tools(model: str | Model, tools: list[Tool]) -> Agent[AgentUserContext]:
    return Agent[AgentUserContext](
        name="ITSM Assistant",
        instructions=INSTRUCTIONS,
        model=model,
        model_settings=ModelSettings(reasoning=Reasoning(effort=AGENT_REASONING_EFFORT)),
        tools=list(tools),
    )


def build_u1_agent(model: str | Model) -> Agent[AgentUserContext]:
    """Build the U1 checkpoint Agent without any later-Unit plugin."""
    return _agent_with_tools(model, U1_TOOLS)


def build_agent(model: str | Model) -> Agent[AgentUserContext]:
    """Build the current cumulative runtime, including installed later Units."""
    return _agent_with_tools(model, TOOLS)
