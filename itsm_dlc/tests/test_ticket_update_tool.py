from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from agents.tool_context import ToolContext

from app.ticketing.capability import DelegatedCredential, TicketRequestCapability
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.tools import TicketRuntime
from app.ticketing.update_models import (
    SafeUpdateReviewFieldV1,
    SafeUpdateReviewV1,
    TicketUpdateToolResultV1,
)
from app.ticketing.update_tools import build_ticket_update_tool, ticket_update_plugin


class _UpdateOperations:
    def __init__(self):
        self.calls = []

    async def ticket_update(self, capability, value, evidence):
        self.calls.append((capability, value, evidence))
        return TicketUpdateToolResultV1(
            state="review_required",
            pending_ref="opaque-reference",
            review=SafeUpdateReviewV1(
                ticket_id="T-1",
                base_version=4,
                normalized_payload_hash="a" * 64,
                fields=[
                    SafeUpdateReviewFieldV1(
                        key="title", label="제목", before="old", after="new"
                    )
                ],
            ),
        )


@dataclass
class _Context:
    ticket_runtime: TicketRuntime


@pytest.mark.anyio
async def test_ticket_update_tool_uses_server_authority_and_returns_safe_result_only():
    async def credential():
        return DelegatedCredential(
            "delegated-secret-canary", datetime.now(timezone.utc) + timedelta(minutes=5)
        )

    operations = _UpdateOperations()
    runtime = TicketRuntime(
        capability=TicketRequestCapability(7, "server-session", "server-conversation", credential, credential),
        operations=object(),
        update_operations=operations,
        current_turn_evidence=CurrentTurnEvidenceV1.capture(
            user_id=7,
            session_id="server-session",
            conversation_id="server-conversation",
            raw_message="변경 준비",
        ),
    )
    tool = build_ticket_update_tool()
    context = ToolContext(
        context=_Context(runtime),
        tool_name="ticket_update",
        tool_call_id="call-update",
        tool_arguments="{}",
    )
    output = await tool.on_invoke_tool(
        context,
        json.dumps(
            {
                "action": "prepare",
                "ticket_id": "T-1",
                "patch": {"title": "new"},
                "pending_ref": None,
            }
        ),
    )
    assert json.loads(output)["pending_ref"] == "opaque-reference"
    assert operations.calls[0][1].patch.title == "new"
    assert runtime.last_update_result is not None
    assert all(value not in output for value in ("delegated-secret-canary", "server-session"))


def test_ticket_update_schema_is_closed_and_business_only():
    tool = build_ticket_update_tool()
    schema = tool.params_json_schema
    assert tool.name == "ticket_update"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"action", "ticket_id", "patch", "pending_ref"}
    serialized = json.dumps(schema).casefold()
    for forbidden in ("credential", "authorization", "if_match", "canonical_patch", "user_id", "session_id"):
        assert forbidden not in serialized


def test_update_plugin_contributes_one_v1_tool():
    plugin = ticket_update_plugin()
    assert plugin.contract_version == 1
    assert plugin.tool_name == "ticket_update"
    assert plugin.presentation_mappers == {}
