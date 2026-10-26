from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.ticketing.create_models import (
    CatalogSelectorV1,
    NormalizedTicketCreateV1,
    OccurrenceTimeInputV1,
    ResolvedCatalogV1,
    SafeMutationResultV1,
    TicketMutationToolResultV1,
    TicketWriteDraftV1,
    TicketWriteToolInputV1,
)


def draft(**overrides):
    values = {
        "title": "로그인 오류",
        "description": "여러 사용자가 로그인하지 못합니다.",
        "service": {"id": "svc-auth"},
        "impact_level": "significant",
        "urgency": "high",
        "impact_note": "업무 시작이 지연됩니다.",
        "occurred_at": {"kind": "now"},
    }
    values.update(overrides)
    return TicketWriteDraftV1.model_validate(values)


@pytest.mark.parametrize(
    "value",
    [{}, {"id": "svc", "name": "서비스"}, {"id": ""}, {"name": "   "}, {"id": "svc", "extra": 1}],
)
def test_catalog_selector_requires_exactly_one_closed_nonempty_value(value):
    with pytest.raises(ValidationError):
        CatalogSelectorV1.model_validate(value)


@pytest.mark.parametrize("field,limit", [("title", 200), ("description", 5000), ("impact_note", 1000)])
def test_draft_enforces_string_limits(field, limit):
    assert len(getattr(draft(**{field: "x" * limit}), field)) == limit
    with pytest.raises(ValidationError):
        draft(**{field: "x" * (limit + 1)})


def test_occurrence_timestamp_requires_timezone_and_now_forbids_value():
    with pytest.raises(ValidationError):
        OccurrenceTimeInputV1(kind="timestamp", value=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        OccurrenceTimeInputV1(kind="now", value=datetime.now(timezone.utc))


def test_normalization_captures_explicit_now_and_materializes_default_once():
    now = datetime(2026, 10, 25, 1, 2, 3, 456789, tzinfo=timezone.utc)
    value = NormalizedTicketCreateV1.from_draft(
        draft(), ResolvedCatalogV1(service_id="svc-auth"), now=now
    )
    assert value.occurred_at == now
    assert value.classification == "standard"


def test_canonical_bytes_are_compact_ordered_utf8_and_omit_absent_optionals():
    value = NormalizedTicketCreateV1.from_draft(
        draft(),
        ResolvedCatalogV1(service_id="svc-auth"),
        now=datetime(2026, 10, 25, 1, 2, 3, tzinfo=timezone.utc),
    )
    canonical = value.canonical_request()
    assert canonical.body_bytes == (
        b'{"title":"\xeb\xa1\x9c\xea\xb7\xb8\xec\x9d\xb8 \xec\x98\xa4\xeb\xa5\x98",'
        b'"description":"\xec\x97\xac\xeb\x9f\xac \xec\x82\xac\xec\x9a\xa9\xec\x9e\x90\xea\xb0\x80 \xeb\xa1\x9c\xea\xb7\xb8\xec\x9d\xb8\xed\x95\x98\xec\xa7\x80 \xeb\xaa\xbb\xed\x95\xa9\xeb\x8b\x88\xeb\x8b\xa4.",'
        b'"service_id":"svc-auth","impact_level":"significant","urgency":"high",'
        b'"impact_note":"\xec\x97\x85\xeb\xac\xb4 \xec\x8b\x9c\xec\x9e\x91\xec\x9d\xb4 \xec\xa7\x80\xec\x97\xb0\xeb\x90\xa9\xeb\x8b\x88\xeb\x8b\xa4.",'
        b'"occurred_at":"2026-10-25T01:02:03Z","classification":"standard"}'
    )
    assert canonical.sha256 == value.payload_hash()
    assert b"component_id" not in canonical.body_bytes
    assert b"model" not in canonical.body_bytes


@pytest.mark.parametrize(
    "value",
    [
        {"action": "prepare"},
        {"action": "prepare", "draft": draft(), "pending_ref": "p"},
        {"action": "revise", "draft": draft()},
        {"action": "confirm", "pending_ref": "p", "draft": draft()},
        {"action": "cancel"},
    ],
)
def test_tool_action_shape_is_closed(value):
    with pytest.raises(ValidationError):
        TicketWriteToolInputV1.model_validate(value)


def test_public_result_contains_no_server_only_secret_fields():
    result = TicketMutationToolResultV1(
        state="succeeded", data=SafeMutationResultV1(ticket_id="generated-test-id", idempotent_replayed=True)
    )
    payload = result.model_dump_json()
    for forbidden in ("idempotency", "canonical", "authorization", "credential", "client_secret"):
        assert forbidden not in payload.casefold()


def test_result_state_cannot_claim_success_without_ticket_evidence():
    with pytest.raises(ValidationError):
        TicketMutationToolResultV1(state="succeeded")


def test_unknown_draft_field_is_rejected():
    with pytest.raises(ValidationError):
        draft(requester="someone")
