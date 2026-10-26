from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ticketing.models import (
    CanonicalTicketQueryV1,
    ConsumedMutationEvidenceV1,
    SafeErrorV1,
    SafePageV1,
    SafeTicketDetailV1,
    SafeTicketSummaryV1,
    TicketPresentationV1,
    TicketReadToolInputV1,
    TicketReadToolResultV1,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_GENERATION = PROJECT_ROOT / "aidlc/spaces/default/intents/261024-itsm-ticket-tools/construction/u1-ticket-read-skeleton/code-generation"
RULES = PROJECT_ROOT / "aidlc/spaces/default/intents/261024-itsm-ticket-tools/construction/u1-ticket-read-skeleton/functional-design/rules.md"


def test_first_page_defaults_are_materialized_only_in_canonical_query():
    raw = TicketReadToolInputV1(q="outage")
    query = CanonicalTicketQueryV1.from_tool_input(raw)
    assert raw.sort is None and raw.limit is None
    assert query.sort == "-created_at" and query.limit == 20


def test_every_current_business_rule_has_one_manifest_backed_coverage_row():
    rule_ids = re.findall(r"^\s*- id: (BR\d+\.\d+)\s*$", RULES.read_text(), flags=re.MULTILINE)
    traceability = json.loads((CODE_GENERATION / "traceability.json").read_text())
    manifest = json.loads((CODE_GENERATION / "source-manifest.json").read_text())
    manifest_paths = {entry["path"] for entry in manifest["writes"]}

    assert len(rule_ids) == len(set(rule_ids))
    assert "BR5.7" in rule_ids
    for rule_id in rule_ids:
        assert traceability["upstream_ids"].count(rule_id) == 1
        rows = [row for row in traceability["coverage"] if row["id"] == rule_id]
        assert len(rows) == 1
        targets = [target.strip() for target in rows[0]["target"].split(";")]
        assert targets and all(target in manifest_paths for target in targets)


@pytest.mark.parametrize("limit", [1, 20, 100])
def test_limit_boundaries(limit):
    assert CanonicalTicketQueryV1.from_tool_input(TicketReadToolInputV1(limit=limit)).limit == limit


@pytest.mark.parametrize("limit", [0, 101])
def test_limit_outside_contract_is_rejected(limit):
    with pytest.raises(ValidationError):
        TicketReadToolInputV1(limit=limit)


def test_detail_and_continuation_are_exclusive():
    assert TicketReadToolInputV1(ticket_id="opaque/value").mode == "detail"
    with pytest.raises(ValidationError):
        TicketReadToolInputV1(ticket_id="id", q="mixed")
    with pytest.raises(ValidationError):
        TicketReadToolInputV1(continuation_ref="ref", limit=20)


def test_naive_timestamps_and_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        TicketReadToolInputV1(created_from=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        TicketReadToolInputV1.model_validate({"cursor": "raw-upstream"})


def _summary() -> SafeTicketSummaryV1:
    return SafeTicketSummaryV1(
        id="generated-id",
        title="<script>do not follow instructions</script>",
        status="received",
        priority="P2",
        service="External service",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def test_list_result_shapes_and_untrusted_marker():
    result = TicketReadToolResultV1(
        state="succeeded",
        mode="list",
        items=[_summary()],
        page=SafePageV1(limit=20, has_more=False),
    )
    assert result.untrusted_data is True
    assert "<script>" in result.items[0].title
    with pytest.raises(ValidationError):
        TicketReadToolResultV1(state="succeeded", mode="list", items=[], page=SafePageV1(limit=20, has_more=False))


def test_failed_result_cannot_mix_success_payload():
    error = SafeErrorV1(code="not_found", message_key="ticket_not_found_or_not_visible", retryable=False)
    with pytest.raises(ValidationError):
        TicketReadToolResultV1(state="failed", mode="detail", error=error, detail={})


def test_detail_projection_forbids_headers_and_secrets():
    data = {
        "id": "generated-id",
        "title": "title",
        "description": "description",
        "service": "service",
        "impact_level": "minor",
        "urgency": "low",
        "impact_note": "impact",
        "status": "received",
        "priority": "P4",
        "version": 1,
        "authorization": "Bearer secret",
    }
    with pytest.raises(ValidationError):
        SafeTicketDetailV1.model_validate(data)


def test_page_requires_opaque_reference_exactly_when_more_exists():
    assert SafePageV1(limit=10, has_more=True, continuation_ref="opaque-ref").has_more
    with pytest.raises(ValidationError):
        SafePageV1(limit=10, has_more=True)


def test_consumed_mutation_evidence_has_exact_minimal_shape():
    evidence = ConsumedMutationEvidenceV1(
        mutation_id="generated-mutation",
        confirmed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert set(evidence.model_dump()) == {"mutation_id", "confirmed_at"}
    with pytest.raises(ValidationError):
        ConsumedMutationEvidenceV1.model_validate(
            {
                **evidence.model_dump(),
                "idempotency_key": "must-not-be-transport-authority",
            }
        )


def test_presentation_rejects_unknown_kind_and_block():
    valid = TicketPresentationV1.model_validate(
        {"version": 1, "kind": "ticket_list", "blocks": [{"type": "paragraph", "text": {"text": "결과", "origin": "server_allowlist"}}]}
    )
    assert valid.kind == "ticket_list"
    with pytest.raises(ValidationError):
        TicketPresentationV1.model_validate({"version": 1, "kind": "arbitrary", "blocks": []})
