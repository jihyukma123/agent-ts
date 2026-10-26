"""Fail-closed evaluation of current-revision release evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .catalog import AUTOMATION_CASES
from .models import (
    CoverageAssessment,
    DefectRoute,
    DocumentationAssurance,
    RegressionExecutionEvidence,
    ReleaseAssuranceRun,
    VerificationEvidence,
)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    status: str
    reasons: tuple[str, ...]
    automation_passed: int
    automation_required: int = 73


class ReleaseAssuranceEvaluator:
    def evaluate(
        self,
        run: ReleaseAssuranceRun,
        evidence: Iterable[VerificationEvidence],
        regressions: Iterable[RegressionExecutionEvidence],
        coverage: CoverageAssessment | None,
        documentation: DocumentationAssurance | None,
        defects: Iterable[DefectRoute],
    ) -> EvaluationResult:
        reasons: list[str] = []
        evidence_list = list(evidence)
        by_check: dict[str, list[VerificationEvidence]] = {}
        for item in evidence_list:
            if item.assurance_run_id != run.assurance_run_id:
                reasons.append(f"foreign assurance run evidence: {item.check_id}")
                continue
            if item.source_revision != run.source_snapshot.revision:
                reasons.append(f"stale source revision evidence: {item.check_id}")
            by_check.setdefault(item.check_id, []).append(item)

        passed = 0
        for case in AUTOMATION_CASES:
            records = by_check.get(case.check_id, [])
            if len(records) != 1:
                reasons.append(f"{case.check_id} evidence count is {len(records)}, expected 1")
            elif records[0].result != "passed":
                reasons.append(f"{case.check_id} did not pass")
            else:
                passed += 1

        regression_by_phase: dict[str, list[RegressionExecutionEvidence]] = {}
        for item in regressions:
            regression_by_phase.setdefault(item.phase, []).append(item)
        for phase in ("baseline", "final"):
            if len(regression_by_phase.get(phase, [])) != 1:
                reasons.append(f"{phase} regression evidence count must be one")
        if all(len(regression_by_phase.get(phase, [])) == 1 for phase in ("baseline", "final")):
            baseline = regression_by_phase["baseline"][0]
            final = regression_by_phase["final"][0]
            if baseline.test_command != final.test_command:
                reasons.append("baseline and final regression commands differ")
            if baseline.command_sha256 != final.command_sha256:
                reasons.append("baseline and final command digests differ")
            if baseline.environment_profile != final.environment_profile:
                reasons.append("baseline and final environments differ")

        if coverage is None:
            reasons.append("coverage assessment is missing")
        elif coverage.assurance_run_id != run.assurance_run_id:
            reasons.append("coverage belongs to another run")
        elif coverage.source_revision != run.source_snapshot.revision:
            reasons.append("coverage source revision is stale")
        elif coverage.result != "passed":
            reasons.append("coverage threshold was not met")

        if documentation is None or documentation.result != "passed":
            reasons.append("REL.07 documentation assurance did not pass")
        elif documentation.assurance_run_id != run.assurance_run_id:
            reasons.append("documentation evidence belongs to another run")

        open_defects = [
            defect for defect in defects
            if defect.severity == "blocking" and defect.status in {"open", "repaired"}
        ]
        if open_defects:
            reasons.append(f"{len(open_defects)} blocking defect(s) remain")
        return EvaluationResult(
            status="blocked" if reasons else "release_ready",
            reasons=tuple(reasons),
            automation_passed=passed,
        )


def route_defect(
    run_id: str, check_id: str, contract_reference: str, sequence: int = 1
) -> DefectRoute:
    if check_id.startswith("C03"):
        owner = "u1-ticket-read-skeleton"
    elif check_id.startswith("C04"):
        owner = "u2-ticket-write"
    elif check_id.startswith("C05"):
        owner = "u3-ticket-update"
    else:
        owner = "u4-release-assurance"
    return DefectRoute(
        defect_route_id=f"defect-{sequence:04d}",
        assurance_run_id=run_id,
        failed_check_id=check_id,
        owning_unit=owner,
        contract_reference=contract_reference,
        severity="blocking",
        status="open",
    )
