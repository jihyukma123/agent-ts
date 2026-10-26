from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.ticketing.update_models import (
    PendingUpdateExecutionV1,
    SafeUpdateReviewFieldV1,
    SafeUpdateReviewV1,
    TicketPatchInV1,
    TicketUpdateToolInputV1,
    TicketUpdateToolResultV1,
    UpdateAttemptPolicyV1,
)


def test_patch_rejects_empty_and_unknown_fields():
    with pytest.raises(ValidationError):
        TicketPatchInV1()
    with pytest.raises(ValidationError):
        TicketPatchInV1.model_validate({"status": "closed"})


@pytest.mark.parametrize("field,value", [
    ("title", "changed"),
    ("description", "changed"),
    ("component_id", 7),
    ("model", "M2"),
    ("impact_level", "significant"),
    ("urgency", "critical"),
    ("impact_note", "changed"),
    ("is_major", True),
])
def test_each_allowed_patch_field_is_accepted(field, value):
    patch = TicketPatchInV1.model_validate({field: value})
    assert patch.selected_payload() == {field: value}


def test_patch_preserves_omitted_and_explicit_null():
    patch = TicketPatchInV1(title=None, model="M2")
    assert patch.selected_payload() == {"title": None, "model": "M2"}
    assert "description" not in patch.selected_payload()


def test_canonical_patch_has_fixed_key_order_compact_bytes_and_hash():
    patch = TicketPatchInV1(is_major=False, title="제목", urgency="low")
    canonical = patch.canonical_patch()
    assert canonical.body_bytes == b'{"title":"\xec\xa0\x9c\xeb\xaa\xa9","urgency":"low","is_major":false}'
    assert canonical.sha256 == hashlib.sha256(canonical.body_bytes).hexdigest()


def test_actual_diff_rejects_noop_and_keeps_only_changes():
    patch = TicketPatchInV1(title="same", urgency="high")
    changed = patch.actual_diff({"title": "same", "urgency": "low"})
    assert changed.selected_payload() == {"urgency": "high"}
    with pytest.raises(ValueError, match="does not change"):
        TicketPatchInV1(title="same").actual_diff({"title": "same"})


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "prepare", "ticket_id": "t"},
        {"action": "prepare", "ticket_id": "t", "patch": {"title": "x"}, "pending_ref": "p"},
        {"action": "revise", "ticket_id": "t", "patch": {"title": "x"}},
        {"action": "confirm", "ticket_id": "t", "patch": {"title": "x"}, "pending_ref": "p"},
        {"action": "cancel", "ticket_id": "t"},
    ],
)
def test_tool_action_shapes_are_closed(payload):
    with pytest.raises(ValidationError):
        TicketUpdateToolInputV1.model_validate(payload)


def _execution(**overrides) -> PendingUpdateExecutionV1:
    patch = TicketPatchInV1(title="changed").canonical_patch()
    now = datetime.now(timezone.utc)
    data = dict(
        mutation_id="mutation-generated",
        user_id=7,
        originating_session_id="session-generated",
        originating_conversation_id="conversation-generated",
        ticket_id="ticket-generated",
        base_version=3,
        canonical_patch_bytes=patch.body_bytes,
        patch_bytes_hash=patch.sha256,
        if_match_version='"3"',
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    data.update(overrides)
    return PendingUpdateExecutionV1(**data)


def test_execution_round_trips_canonical_patch_and_hides_server_material():
    execution = _execution()
    assert execution.patch().selected_payload() == {"title": "changed"}
    public = execution.model_dump(mode="json")
    assert "canonical_patch_bytes" not in public
    assert "if_match_version" not in public


def test_execution_rejects_hash_etag_and_retired_shape_tampering():
    with pytest.raises(ValidationError):
        _execution(patch_bytes_hash="0" * 64)
    with pytest.raises(ValidationError):
        _execution(if_match_version='"4"')
    with pytest.raises(ValidationError):
        _execution(status="retired")


def test_review_result_contains_only_safe_fields():
    review = SafeUpdateReviewV1(
        ticket_id="ticket-generated",
        base_version=3,
        normalized_payload_hash="a" * 64,
        fields=[SafeUpdateReviewFieldV1(key="title", label="제목", before="old", after="new")],
    )
    result = TicketUpdateToolResultV1(
        state="review_required", pending_ref="pending-generated", review=review
    )
    rendered = result.model_dump_json()
    for forbidden in ("credential", "authorization", "canonical_patch_bytes", "if_match_version"):
        assert forbidden not in rendered.casefold()


def test_attempt_policy_enforces_patch_and_refresh_ceilings():
    policy = UpdateAttemptPolicyV1()
    policy.record_patch()
    policy.record_patch()
    policy.record_refresh()
    with pytest.raises(RuntimeError, match="PATCH"):
        policy.record_patch()
    with pytest.raises(RuntimeError, match="refresh"):
        policy.record_refresh()
