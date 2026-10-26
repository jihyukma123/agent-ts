"""OpenAI Agents SDK 경계에 노출되는 ticket_read function tool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from agents import RunContextWrapper, Tool, function_tool
from pydantic import Field

from app.ticketing.capability import TicketRequestCapability
from app.ticketing.create_models import TicketMutationToolResultV1
from app.ticketing.create_operations import TicketCreateOperations
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import TicketPriority, TicketReadToolInputV1, TicketReadToolResultV1, TicketStatus
from app.ticketing.operations import TicketOperations
from app.ticketing.update_models import TicketUpdateToolResultV1
from app.ticketing.update_operations import TicketUpdateOperations


@dataclass
class TicketRuntime:
    capability: TicketRequestCapability
    operations: TicketOperations
    create_operations: TicketCreateOperations | None = None
    update_operations: TicketUpdateOperations | None = None
    current_turn_evidence: CurrentTurnEvidenceV1 | None = None
    last_read_result: TicketReadToolResultV1 | None = None
    last_write_result: TicketMutationToolResultV1 | None = None
    last_update_result: TicketUpdateToolResultV1 | None = None


def build_ticket_read_tool() -> Tool:
    @function_tool(
        name_override="ticket_read",
        description_override="사용자가 볼 수 있는 ITSM 티켓 목록 또는 상세를 조회합니다.",
        use_docstring_info=False,
    )
    async def ticket_read(
        ctx: RunContextWrapper,
        ticket_id: str | None = None,
        q: Annotated[str | None, Field(max_length=200)] = None,
        status: list[TicketStatus] | None = None,
        priority: list[TicketPriority] | None = None,
        service_id: str | None = None,
        component_id: int | None = None,
        model: str | None = None,
        assignment_group_id: int | None = None,
        assignee: str | None = None,
        requester: str | None = None,
        classification: Literal["standard", "restricted"] | None = None,
        is_major: bool | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        updated_from: datetime | None = None,
        sort: Literal["-created_at", "-updated_at", "priority"] | None = None,
        limit: Annotated[int | None, Field(ge=1, le=100)] = None,
        continuation_ref: str | None = None,
    ) -> str:
        runtime = getattr(ctx.context, "ticket_runtime", None)
        if not isinstance(runtime, TicketRuntime):
            raise RuntimeError("ticket runtime is unavailable")
        value = TicketReadToolInputV1(
            ticket_id=ticket_id,
            q=q,
            status=status,
            priority=priority,
            service_id=service_id,
            component_id=component_id,
            model=model,
            assignment_group_id=assignment_group_id,
            assignee=assignee,
            requester=requester,
            classification=classification,
            is_major=is_major,
            created_from=created_from,
            created_to=created_to,
            updated_from=updated_from,
            sort=sort,
            limit=limit,
            continuation_ref=continuation_ref,
        )
        result = await runtime.operations.ticket_read(runtime.capability, value)
        runtime.last_read_result = result
        return result.model_dump_json(exclude_none=True)

    return ticket_read
