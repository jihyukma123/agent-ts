from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.release_assurance.models import (
    CoverageAssessment,
    DocumentationAssurance,
    EnvironmentProfile,
    PerformanceTrial,
    RegressionExecutionEvidence,
    SourceSnapshot,
)


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
DIGEST = "a" * 64


def environment():
    return EnvironmentProfile(
        python="3.13.3", executable=".venv/bin/python", pytest="9.1.1", coverage="7.17.0",
        dependency_lock_sha256=DIGEST, os="Darwin", architecture="arm64", locale="C.UTF-8",
        timezone="UTC", configuration_digest="b" * 64,
    )


def test_closed_models_reject_unknown_and_secret_shaped_fields():
    with pytest.raises(ValidationError):
        SourceSnapshot(revision=f"workspace-source-v1:{DIGEST}", head_revision="head",
                       paths_digest=DIGEST, path_count=1, authorization="Bearer hidden")


def test_source_snapshot_requires_content_addressed_revision():
    with pytest.raises(ValidationError):
        SourceSnapshot(revision="HEAD", head_revision="head", paths_digest=DIGEST, path_count=1)


def test_regression_counts_must_balance():
    with pytest.raises(ValidationError, match="counts"):
        RegressionExecutionEvidence(
            regression_evidence_id="reg-1", phase="baseline", source_revision="rev",
            test_command="pytest", command_sha256=DIGEST, environment_profile=environment(),
            total_count=2, passed_count=1, failed_count=0, skipped_count=0, exit_status=0,
            output_digest=DIGEST, executed_at=NOW,
        )


def test_final_regression_rejects_skip_or_failure():
    with pytest.raises(ValidationError, match="without failures or skips"):
        RegressionExecutionEvidence(
            regression_evidence_id="reg-1", phase="final", source_revision="rev",
            test_command="pytest", command_sha256=DIGEST, environment_profile=environment(),
            total_count=1, passed_count=0, failed_count=0, skipped_count=1, exit_status=0,
            output_digest=DIGEST, executed_at=NOW,
        )


def test_coverage_percentage_and_sorted_paths_are_enforced():
    with pytest.raises(ValidationError, match="sorted unique"):
        CoverageAssessment(
            coverage_assessment_id="cov", assurance_run_id="run", source_revision="rev",
            source_manifest_digest=DIGEST, source_manifest_paths=("app/z.py", "app/a.py"),
            covered_lines=8, executable_lines=10, percentage=80.0, result="passed",
        )


def test_documentation_result_is_derived_from_all_seven_topics():
    with pytest.raises(ValidationError, match="does not match"):
        DocumentationAssurance(
            documentation_assurance_id="docs", assurance_run_id="run",
            readme_topics=frozenset({"feature"}), smoke_prerequisites_documented=True,
            explicit_credentials_required=True, default_automation_uses_real_services=False,
            result="passed",
        )


def test_performance_trial_duration_and_failure_reason_are_enforced():
    with pytest.raises(ValidationError, match="duration"):
        PerformanceTrial(
            trial_id="trial", measurement_session_id="session", method="assistant", scenario="read",
            trial_number=1, generated_data_reference="generated-1", measured_at=NOW,
            monotonic_start=1.0, monotonic_end=3.0, duration_seconds=1.0,
            completion_event="done", success=True, external_screen_transitions=0,
        )
