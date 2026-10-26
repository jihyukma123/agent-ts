from datetime import datetime, timezone

from app.release_assurance.catalog import AUTOMATION_CASES
from app.release_assurance.evaluator import ReleaseAssuranceEvaluator, route_defect
from app.release_assurance.models import (
    CoverageAssessment, DocumentationAssurance, EnvironmentProfile,
    RegressionExecutionEvidence, ReleaseAssuranceRun, SourceSnapshot, VerificationEvidence,
)


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
DIGEST = "a" * 64
REVISION = "workspace-source-v1:" + DIGEST


def profile():
    return EnvironmentProfile(
        python="3.13", executable="python", pytest="9.1.1", coverage="7.17.0",
        dependency_lock_sha256=DIGEST, os="Darwin", architecture="arm64", locale="C",
        timezone="UTC", configuration_digest="b" * 64,
    )


def run():
    return ReleaseAssuranceRun(
        assurance_run_id="run", source_snapshot=SourceSnapshot(
            revision=REVISION, head_revision="head", paths_digest=DIGEST, path_count=1),
        started_at=NOW, environment_profile=profile(), baseline_locator="baselines/base",
        baseline_digest=DIGEST,
    )


def evidences():
    return [VerificationEvidence(
        evidence_id=f"e-{case.check_id}", assurance_run_id="run", source_revision=REVISION,
        check_id=case.check_id, category=case.category, result="passed",
        command_or_procedure=case.procedure_key, evidence_digest=DIGEST, recorded_at=NOW,
    ) for case in AUTOMATION_CASES]


def regressions():
    values = []
    for phase in ("baseline", "final"):
        values.append(RegressionExecutionEvidence(
            regression_evidence_id=f"reg-{phase}", phase=phase,
            source_revision="baseline-revision" if phase == "baseline" else REVISION,
            test_command=".venv/bin/python -m pytest -q", command_sha256=DIGEST,
            environment_profile=profile(), total_count=10, passed_count=10,
            failed_count=0, skipped_count=0, exit_status=0, output_digest=DIGEST,
            executed_at=NOW,
        ))
    return values


def coverage():
    return CoverageAssessment(
        coverage_assessment_id="cov", assurance_run_id="run", source_revision=REVISION,
        source_manifest_digest=DIGEST, source_manifest_paths=("app/a.py",),
        covered_lines=8, executable_lines=10, percentage=80.0, result="passed",
    )


def documentation():
    return DocumentationAssurance(
        documentation_assurance_id="docs", assurance_run_id="run",
        readme_topics=frozenset({"feature", "configuration", "run", "security_boundary", "tests", "coverage", "smoke_separation"}),
        smoke_prerequisites_documented=True, explicit_credentials_required=True,
        default_automation_uses_real_services=False, result="passed",
    )


def evaluate(**overrides):
    inputs = {"run": run(), "evidence": evidences(), "regressions": regressions(),
              "coverage": coverage(), "documentation": documentation(), "defects": []}
    inputs.update(overrides)
    return ReleaseAssuranceEvaluator().evaluate(**inputs)


def test_all_current_evidence_is_release_ready():
    result = evaluate()
    assert result.status == "release_ready" and result.automation_passed == 73


def test_missing_duplicate_and_failed_evidence_block():
    items = evidences()
    assert evaluate(evidence=items[:-1]).status == "blocked"
    assert evaluate(evidence=[*items, items[0]]).status == "blocked"
    assert evaluate(evidence=[items[0].model_copy(update={"result": "failed"}), *items[1:]]).status == "blocked"


def test_stale_revision_evidence_blocks():
    items = evidences()
    items[0] = items[0].model_copy(update={"source_revision": "workspace-source-v1:" + "c" * 64})
    assert "stale" in " ".join(evaluate(evidence=items).reasons)


def test_regression_command_or_environment_mismatch_blocks():
    items = regressions()
    items[1] = items[1].model_copy(update={"command_sha256": "c" * 64})
    assert evaluate(regressions=items).status == "blocked"


def test_coverage_and_documentation_are_independent_blocking_inputs():
    assert evaluate(coverage=None).status == "blocked"
    assert evaluate(documentation=None).status == "blocked"


def test_open_or_repaired_blocking_defect_blocks_until_reverified():
    defect = route_defect("run", "C04.01", "C-04")
    assert defect.owning_unit == "u2-ticket-write"
    assert evaluate(defects=[defect]).status == "blocked"
    assert evaluate(defects=[defect.model_copy(update={"status": "reverified"})]).status == "release_ready"


def test_defect_router_maps_each_contract_owner():
    assert route_defect("run", "C03.01", "C-03").owning_unit.startswith("u1")
    assert route_defect("run", "C05.01", "C-05").owning_unit.startswith("u3")
    assert route_defect("run", "REL.01", "release").owning_unit.startswith("u4")
