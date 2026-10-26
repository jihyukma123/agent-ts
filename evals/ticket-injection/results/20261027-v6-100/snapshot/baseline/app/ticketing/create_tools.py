"""OpenAI Agents SDK contribution for the single U2 ``ticket_write`` tool."""

from __future__ import annotations

from typing import Literal

from agents import RunContextWrapper, Tool, function_tool

from app.ticketing.create_models import TicketWriteDraftV1, TicketWriteToolInputV1
from app.ticketing.create_presentation import ticket_create_presentation
from app.ticketing.registry import TicketPluginV1
from app.ticketing.tools import TicketRuntime


def build_ticket_write_tool() -> Tool:
    @function_tool(
        name_override="ticket_write",
        description_override="ITSM 티켓 등록 내용을 준비·검토하고 명시적 확인 뒤 등록합니다.",
        use_docstring_info=False,
    )
    async def ticket_write(
        ctx: RunContextWrapper,
        action: Literal["prepare", "confirm", "revise", "cancel"],
        draft: TicketWriteDraftV1 | None = None,
        pending_ref: str | None = None,
    ) -> str:
        runtime = getattr(ctx.context, "ticket_runtime", None)
        if not isinstance(runtime, TicketRuntime) or runtime.create_operations is None:
            raise RuntimeError("ticket create runtime is unavailable")
        if runtime.current_turn_evidence is None:
            raise RuntimeError("current turn evidence is unavailable")
        value = TicketWriteToolInputV1(action=action, draft=draft, pending_ref=pending_ref)
        result = await runtime.create_operations.ticket_write(
            runtime.capability, value, runtime.current_turn_evidence
        )
        runtime.last_write_result = result
        return result.model_dump_json(exclude_none=True)

    return ticket_write


def ticket_write_plugin() -> TicketPluginV1:
    return TicketPluginV1(
        contract_version=1,
        tool_name="ticket_write",
        function_tool_factory=build_ticket_write_tool,
        presentation_mappers={
            kind: ticket_create_presentation
            for kind in (
                "ticket_input",
                "ticket_review",
                "ticket_success",
                "ticket_auth_error",
                "ticket_rate_limited",
                "ticket_ambiguous",
                "ticket_unavailable",
            )
        },
    )
