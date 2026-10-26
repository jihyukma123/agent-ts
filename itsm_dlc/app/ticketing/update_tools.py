"""OpenAI Agents SDK contribution for the U3 ``ticket_update`` tool."""

from __future__ import annotations

import logging
from typing import Literal

from agents import RunContextWrapper, Tool, function_tool

from app.ticketing.registry import TicketPluginV1
from app.ticketing.tools import TicketRuntime
from app.ticketing.update_models import TicketPatchInV1, TicketUpdateToolInputV1

logger = logging.getLogger("itsm.ticket_update")


def build_ticket_update_tool() -> Tool:
    @function_tool(
        name_override="ticket_update",
        description_override="기존 ITSM 티켓 변경을 준비·검토하고 명시적 확인 뒤 적용합니다.",
        use_docstring_info=False,
    )
    async def ticket_update(
        ctx: RunContextWrapper,
        action: Literal["prepare", "confirm", "revise", "cancel"],
        ticket_id: str,
        patch: TicketPatchInV1 | None = None,
        pending_ref: str | None = None,
    ) -> str:
        runtime = getattr(ctx.context, "ticket_runtime", None)
        if not isinstance(runtime, TicketRuntime) or runtime.update_operations is None:
            raise RuntimeError("ticket update runtime is unavailable")
        if runtime.current_turn_evidence is None:
            raise RuntimeError("current turn evidence is unavailable")
        value = TicketUpdateToolInputV1(
            action=action,
            ticket_id=ticket_id,
            patch=patch,
            pending_ref=pending_ref,
        )
        try:
            result = await runtime.update_operations.ticket_update(
                runtime.capability, value, runtime.current_turn_evidence
            )
        except Exception as exc:
            logger.warning("ticket update tool failed safely: %s", type(exc).__name__)
            raise
        runtime.last_update_result = result
        return result.model_dump_json(exclude_none=True)

    return ticket_update


def ticket_update_plugin() -> TicketPluginV1:
    return TicketPluginV1(
        contract_version=1,
        tool_name="ticket_update",
        function_tool_factory=build_ticket_update_tool,
        # Presentation kinds are the shared C-06 vocabulary already allowlisted
        # by U1/U2. The service selects this Unit's typed mapper by result type.
        presentation_mappers={},
    )
