import socket

import pytest

from app.release_assurance.evidence import SECRET_PATTERN, safe_output_digest
from app.release_assurance.models import VerificationEvidence


@pytest.mark.parametrize(
    "boundary,value",
    [
        ("model", "Authorization: model-secret"),
        ("tool", "Bearer tool-secret"),
        ("result", "sk-resultsecret123"),
        ("log", "api_key=log-secret"),
        ("error", "client_secret=error-secret"),
        ("assertion", "Authorization: assertion-secret"),
        ("traceback", "Bearer traceback-secret"),
        ("stdout", "sk-stdoutsecret123"),
        ("stderr", "api-key: stderr-secret"),
        ("runner-report", "client-secret=runner-secret"),
    ],
)
def test_secret_canary_boundary_fails_with_safe_identifier(boundary, value):
    with pytest.raises(ValueError) as caught:
        safe_output_digest(value)
    assert str(caught.value) == "secret boundary violation: command-output"
    assert value not in str(caught.value)


def test_safe_counts_and_digest_are_allowed_without_payload():
    assert len(safe_output_digest("passed=73 network_attempts=0 exposures=0")) == 64


def test_evidence_closed_schema_rejects_raw_output_and_authorization_fields():
    with pytest.raises(Exception):
        VerificationEvidence(
            evidence_id="e", assurance_run_id="run", source_revision="rev", check_id="REL.05",
            category="security", result="passed", command_or_procedure="scan",
            evidence_digest="a" * 64, recorded_at="2026-10-25T00:00:00Z", raw_output="secret",
        )


def test_secret_pattern_does_not_flag_safe_boundary_id():
    assert SECRET_PATTERN.search("secret boundary violation: model") is None
