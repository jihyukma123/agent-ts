"""Closed, non-secret data contracts for repository release assurance."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SHA256 = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceSnapshot(ClosedModel):
    revision: str = Field(pattern=r"^workspace-source-v1:[0-9a-f]{64}$")
    head_revision: str
    paths_digest: str
    path_count: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_digest(self) -> "SourceSnapshot":
        if not SHA256.fullmatch(self.paths_digest):
            raise ValueError("paths_digest must be a lowercase SHA-256")
        return self


class EnvironmentProfile(ClosedModel):
    python: str
    executable: str
    pytest: str
    coverage: str
    dependency_lock_sha256: str
    os: str
    architecture: str
    locale: str
    timezone: str
    configuration_digest: str

    @model_validator(mode="after")
    def validate_digests(self) -> "EnvironmentProfile":
        for value in (self.dependency_lock_sha256, self.configuration_digest):
            if not SHA256.fullmatch(value):
                raise ValueError("environment digests must be lowercase SHA-256")
        return self


class RegressionExecutionEvidence(ClosedModel):
    regression_evidence_id: str
    phase: Literal["baseline", "final"]
    source_revision: str
    test_command: str
    command_sha256: str
    environment_profile: EnvironmentProfile
    total_count: Annotated[int, Field(ge=0)]
    passed_count: Annotated[int, Field(ge=0)]
    failed_count: Annotated[int, Field(ge=0)]
    skipped_count: Annotated[int, Field(ge=0)]
    exit_status: int
    output_digest: str
    executed_at: datetime

    @model_validator(mode="after")
    def validate_counts_and_digests(self) -> "RegressionExecutionEvidence":
        if self.total_count != self.passed_count + self.failed_count + self.skipped_count:
            raise ValueError("regression result counts do not equal total_count")
        if not SHA256.fullmatch(self.command_sha256) or not SHA256.fullmatch(self.output_digest):
            raise ValueError("regression digests must be lowercase SHA-256")
        if self.phase == "final" and (
            self.exit_status != 0 or self.failed_count or self.skipped_count
        ):
            raise ValueError("final regression must pass without failures or skips")
        return self


class BaselineReceipt(ClosedModel):
    baseline_receipt_id: str
    source_snapshot: SourceSnapshot
    regression: RegressionExecutionEvidence
    finalized_at: datetime
    locator: str
    receipt_digest: str

    @model_validator(mode="after")
    def validate_baseline(self) -> "BaselineReceipt":
        if self.regression.phase != "baseline":
            raise ValueError("baseline receipt requires baseline regression evidence")
        if self.regression.source_revision != self.source_snapshot.revision:
            raise ValueError("baseline regression revision does not match its snapshot")
        if not SHA256.fullmatch(self.receipt_digest):
            raise ValueError("receipt_digest must be a lowercase SHA-256")
        return self


class ReleaseAssuranceRun(ClosedModel):
    assurance_run_id: str
    source_snapshot: SourceSnapshot
    started_at: datetime
    status: Literal[
        "draft",
        "preflight_ready",
        "automated_verified",
        "documentation_verified",
        "release_ready",
        "blocked",
    ] = "draft"
    environment_profile: EnvironmentProfile
    baseline_locator: str
    baseline_digest: str
    predecessor_locator: str | None = None
    predecessor_digest: str | None = None
    completed_at: datetime | None = None
    blocking_reason: str | None = None

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> "ReleaseAssuranceRun":
        if not SHA256.fullmatch(self.baseline_digest):
            raise ValueError("baseline_digest must be a lowercase SHA-256")
        if (self.predecessor_locator is None) != (self.predecessor_digest is None):
            raise ValueError("predecessor locator and digest must be provided together")
        if self.predecessor_digest and not SHA256.fullmatch(self.predecessor_digest):
            raise ValueError("predecessor_digest must be a lowercase SHA-256")
        if self.status in {"release_ready", "blocked"} and self.completed_at is None:
            raise ValueError("terminal run requires completed_at")
        if self.status == "blocked" and not self.blocking_reason:
            raise ValueError("blocked run requires a safe blocking_reason")
        return self


class VerificationEvidence(ClosedModel):
    evidence_id: str
    assurance_run_id: str
    source_revision: str
    check_id: str
    category: Literal[
        "assembly", "lifecycle", "security", "network", "contract",
        "regression", "coverage", "accessibility", "documentation"
    ]
    result: Literal["passed", "failed", "not_run"]
    command_or_procedure: str
    observed_counts: dict[str, int] = Field(default_factory=dict)
    evidence_digest: str
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_digest(self) -> "VerificationEvidence":
        if not SHA256.fullmatch(self.evidence_digest):
            raise ValueError("evidence_digest must be a lowercase SHA-256")
        return self


class CoverageAssessment(ClosedModel):
    coverage_assessment_id: str
    assurance_run_id: str
    source_revision: str
    source_manifest_digest: str
    source_manifest_paths: tuple[str, ...]
    covered_lines: Annotated[int, Field(ge=0)]
    executable_lines: Annotated[int, Field(ge=1)]
    percentage: Annotated[float, Field(ge=0, le=100)]
    threshold: Annotated[float, Field(ge=80, le=100)] = 80.0
    result: Literal["passed", "failed"]

    @model_validator(mode="after")
    def validate_coverage(self) -> "CoverageAssessment":
        if not SHA256.fullmatch(self.source_manifest_digest):
            raise ValueError("source_manifest_digest must be a lowercase SHA-256")
        if tuple(sorted(set(self.source_manifest_paths))) != self.source_manifest_paths:
            raise ValueError("source manifest paths must be a sorted unique tuple")
        expected = self.covered_lines * 100 / self.executable_lines
        if abs(expected - self.percentage) > 0.005:
            raise ValueError("coverage percentage does not match line counts")
        if (self.percentage >= self.threshold) != (self.result == "passed"):
            raise ValueError("coverage result does not match threshold")
        return self


class DefectRoute(ClosedModel):
    defect_route_id: str
    assurance_run_id: str
    failed_check_id: str
    owning_unit: Literal[
        "u1-ticket-read-skeleton", "u2-ticket-write", "u3-ticket-update", "u4-release-assurance"
    ]
    contract_reference: str
    severity: Literal["blocking", "non_blocking"]
    status: Literal["open", "repaired", "reverified"]
    repair_evidence_id: str | None = None


class DocumentationAssurance(ClosedModel):
    documentation_assurance_id: str
    assurance_run_id: str
    readme_topics: frozenset[Literal[
        "feature", "configuration", "run", "security_boundary", "tests", "coverage",
        "smoke_separation"
    ]]
    smoke_prerequisites_documented: bool
    explicit_credentials_required: bool
    default_automation_uses_real_services: bool
    result: Literal["passed", "failed"]

    @model_validator(mode="after")
    def validate_result(self) -> "DocumentationAssurance":
        complete = len(self.readme_topics) == 7
        expected = (
            complete
            and self.smoke_prerequisites_documented
            and self.explicit_credentials_required
            and not self.default_automation_uses_real_services
        )
        if expected != (self.result == "passed"):
            raise ValueError("documentation result does not match its evidence")
        return self


class PerformanceMeasurementSession(ClosedModel):
    measurement_session_id: str
    source_revision: str
    deployment_id: str
    observation_time: datetime
    anonymized_role: str
    browser: str
    device_class: str
    os: str
    viewport: str
    network_profile: str
    cache_state: str
    status: Literal["planned", "measuring", "complete", "invalid"] = "planned"
    invalid_reason: str | None = None


class PerformanceTrial(ClosedModel):
    trial_id: str
    measurement_session_id: str
    method: Literal["assistant", "existing_ui"]
    scenario: Literal["read", "create", "update"]
    trial_number: Annotated[int, Field(ge=1, le=3)]
    generated_data_reference: str
    measured_at: datetime
    monotonic_start: Annotated[float, Field(ge=0)]
    monotonic_end: Annotated[float, Field(ge=0)]
    duration_seconds: Annotated[float, Field(ge=0)]
    completion_event: str
    success: bool
    failure_reason: str | None = None
    external_screen_transitions: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_duration(self) -> "PerformanceTrial":
        if abs((self.monotonic_end - self.monotonic_start) - self.duration_seconds) > 1e-6:
            raise ValueError("duration does not match monotonic timepoints")
        if not self.success and not self.failure_reason:
            raise ValueError("failed trial requires a safe failure_reason")
        return self
