import json
from dataclasses import dataclass

import pytest
from agents.tool_context import ToolContext

from app.ticketing.capability import DelegatedCredential, TicketRequestCapability
from app.ticketing.create_models import SafeMutationReviewV1, TicketMutationToolResultV1
from app.ticketing.create_tools import build_ticket_write_tool, ticket_write_plugin
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.tools import TicketRuntime


class _ReadOperations:
    pass


class _CreateOperations:
    def __init__(self):
        self.calls = []

    async def ticket_write(self, capability, value, evidence):
        self.calls.append((capability, value, evidence))
        return TicketMutationToolResultV1(
            state="review_required",
            pending_ref="safe-reference",
            review=SafeMutationReviewV1(normalized_payload_hash="a" * 64, fields=[]),
        )


@dataclass
class _Context:
    ticket_runtime: TicketRuntime


@pytest.mark.anyio
async def test_ticket_write_tool_uses_server_runtime_and_serializes_safe_result_only():
    async def token():
        return DelegatedCredential("delegated-secret-canary")

    create = _CreateOperations()
    runtime = TicketRuntime(
        capability=TicketRequestCapability(7, "server-session", "server-conversation", token, token),
        operations=_ReadOperations(),
        create_operations=create,
        current_turn_evidence=CurrentTurnEvidenceV1.capture(
            user_id=7,
            session_id="server-session",
            conversation_id="server-conversation",
            raw_message="prepare",
        ),
    )
    tool = build_ticket_write_tool()
    context = ToolContext(
        context=_Context(runtime),
        tool_name="ticket_write",
        tool_call_id="call-write",
        tool_arguments="{}",
    )
    arguments = {
        "action": "prepare",
        "draft": {
            "title": "Generated issue",
            "description": "Generated description",
            "service": {"id": "svc"},
            "impact_level": "minor",
            "urgency": "low",
            "impact_note": "Generated impact",
            "occurred_at": {"kind": "now"},
            "classification": "standard",
        },
        "pending_ref": None,
    }
    output = await tool.on_invoke_tool(context, json.dumps(arguments))
    parsed = json.loads(output)
    assert parsed["state"] == "review_required" and parsed["pending_ref"] == "safe-reference"
    assert create.calls[0][1].action == "prepare"
    assert runtime.last_write_result is not None
    assert all(secret not in output for secret in ("delegated-secret-canary", "server-session", "authorization"))


def test_tool_schema_is_closed_and_has_only_business_inputs():
    tool = build_ticket_write_tool()
    schema = tool.params_json_schema
    assert tool.name == "ticket_write"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"action", "draft", "pending_ref"}
    serialized = json.dumps(schema).casefold()
    for forbidden in ("credential", "authorization", "idempotency", "canonical", "user_id", "session_id"):
        assert forbidden not in serialized


def test_plugin_contributes_only_v1_ticket_write_and_allowlisted_mapper_kinds():
    plugin = ticket_write_plugin()
    assert plugin.contract_version == 1 and plugin.tool_name == "ticket_write"
    assert set(plugin.presentation_mappers) == {
        "ticket_input",
        "ticket_review",
        "ticket_success",
        "ticket_auth_error",
        "ticket_rate_limited",
        "ticket_ambiguous",
        "ticket_unavailable",
    }
