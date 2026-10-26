import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.release_assurance.evidence import (
    EvidenceRepository,
    collect_environment_profile,
    command_digest,
    create_source_snapshot,
    parse_junit_counts,
    publish_baseline_receipt,
    safe_output_digest,
)
from app.release_assurance.catalog import AUTOMATION_CASES


def test_source_snapshot_is_deterministic_and_changes_with_bytes(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    source = tmp_path / "app" / "a.py"
    source.write_text("value = 1\n")
    monkeypatch.setattr("app.release_assurance.evidence._git_head", lambda _root: "head")
    first = create_source_snapshot(tmp_path, ("app",))
    assert first == create_source_snapshot(tmp_path, ("app",))
    source.write_text("value = 2\n")
    assert first.revision != create_source_snapshot(tmp_path, ("app",)).revision


def test_source_snapshot_revision_binds_head_as_well_as_paths(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("value = 1\n")
    monkeypatch.setattr("app.release_assurance.evidence._git_head", lambda _root: "head-one")
    first = create_source_snapshot(tmp_path, ("app",))
    monkeypatch.setattr("app.release_assurance.evidence._git_head", lambda _root: "head-two")
    second = create_source_snapshot(tmp_path, ("app",))
    assert first.paths_digest == second.paths_digest
    assert first.revision != second.revision


def test_safe_output_digest_rejects_secret_boundaries():
    assert len(safe_output_digest("10 passed")) == 64
    for value in ("Authorization: hidden", "Bearer token-value", "sk-secret123456"):
        with pytest.raises(ValueError, match="secret boundary violation"):
            safe_output_digest(value)


def test_command_digest_binds_actual_argv():
    assert command_digest(["python", "-m", "pytest"]) != command_digest(["python", "pytest"])


def test_junit_collector_returns_safe_counts_without_testcase_output():
    xml = '<testsuites><testsuite tests="5" failures="1" errors="1" skipped="1"><testcase name="secret-name"/></testsuite></testsuites>'
    assert parse_junit_counts(xml) == {
        "total_count": 5, "passed_count": 2, "failed_count": 2, "skipped_count": 1
    }


def test_junit_collector_rejects_missing_or_invalid_suite_counts():
    with pytest.raises(ValueError, match="no testsuite"):
        parse_junit_counts("<root />")
    with pytest.raises(ValueError, match="invalid"):
        parse_junit_counts('<testsuite tests="1" failures="2" errors="0" skipped="0" />')


def _ready_repository(tmp_path, monkeypatch):
    monkeypatch.setattr("app.release_assurance.evidence._git_head", lambda _root: "head")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "app").mkdir(); (tmp_path / "scripts").mkdir()
    (tmp_path / "app" / "a.py").write_text("before = 1\n")
    (tmp_path / "scripts" / "a.py").write_text("script = 1\n")
    (tmp_path / "requirements.txt").write_text("pytest==9.1.1\ncoverage==7.17.0\n")
    environment = collect_environment_profile(tmp_path).model_dump(mode="json")
    baseline_snapshot = create_source_snapshot(tmp_path)
    command = ".venv/bin/python -m pytest -q"
    receipt = {
        "baseline_receipt_id": "baseline-one",
        "source_snapshot": baseline_snapshot.model_dump(mode="json"),
        "source_roots": ["app", "scripts"],
        "test_command": command,
        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "environment_profile": environment,
        "result": {"total_count": 10, "passed_count": 10, "failed_count": 0,
                   "skipped_count": 0, "exit_status": 0, "output_digest": "a" * 64},
        "executed_at": "2026-10-25T00:00:00+00:00",
    }
    store = tmp_path / ".release-assurance"
    baseline_dir = publish_baseline_receipt(store, receipt)
    baseline_digest = hashlib.sha256((baseline_dir / "baseline.json").read_bytes()).hexdigest()
    (tmp_path / "app" / "a.py").write_text("after = 2\n")
    snapshot = create_source_snapshot(tmp_path)
    repo = EvidenceRepository(store)
    repo.create_run("run", snapshot.revision)
    repo.write_record("run", "preflight.json", {
        "run_id": "run", "source_revision": snapshot.revision,
        "source_snapshot": snapshot.model_dump(mode="json"), "environment_profile": environment,
        "baseline_locator": baseline_dir.relative_to(store).as_posix(),
        "baseline_digest": baseline_digest, "source_roots": ["app", "scripts"],
    })
    cases = []
    for case in AUTOMATION_CASES:
        path, name = case.selector.split("::", 1)
        cases.append(f'<testcase classname="{path.removesuffix(".py").replace("/", ".")}" name="{name}" />')
    repo.write_record("run", "evidence/automation.json", {
        "run_id": "run", "source_revision": snapshot.revision,
        "junit_xml": '<testsuites><testsuite tests="73">' + "".join(cases) + "</testsuite></testsuites>",
    })
    result = {"test_command": command, "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
              "environment_profile": environment, "total_count": 10, "passed_count": 10,
              "failed_count": 0, "skipped_count": 0, "exit_status": 0}
    repo.write_record("run", "evidence/regression.json", {
        "run_id": "run", "source_revision": snapshot.revision,
        "baseline_locator": baseline_dir.relative_to(store).as_posix(),
        "baseline_digest": baseline_digest,
        "baseline": {**result, "source_revision": baseline_snapshot.revision},
        "final": {**result, "source_revision": snapshot.revision},
    })
    repo.write_record("run", "evidence/coverage.json", {
        "run_id": "run", "source_revision": snapshot.revision, "covered_lines": 8, "executable_lines": 10,
        "percentage": 80.0, "threshold": 80.0, "input_drift": 0, "missing_sources": 0,
    })
    repo.write_record("run", "evidence/security-network.json", {
        "run_id": "run", "source_revision": snapshot.revision,
        "unexpected_network_attempts": 0, "deliberate_bypass_blocked": 1,
        "secret_canary_exposures": 0,
    })
    repo.write_record("run", "evidence/documentation.json", {
        "run_id": "run", "source_revision": snapshot.revision,
        "check_id": "REL.07", "result": "passed",
        "topics": ["feature", "configuration", "run", "security_boundary", "tests", "coverage", "smoke_separation"],
        "default_automation_uses_real_services": False, "explicit_live_smoke_prerequisites": True,
    })
    repo.write_record("run", "evidence/defects.json", {
        "run_id": "run", "source_revision": snapshot.revision, "blocking": [],
    })
    repo.write_record("run", "evidence/performance.json", {
        "run_id": "run", "source_revision": snapshot.revision, "status": "not_measured",
    })
    return repo, snapshot


def test_repository_derives_release_ready_from_closed_evidence(tmp_path, monkeypatch):
    repo, _snapshot = _ready_repository(tmp_path, monkeypatch)
    final = repo.finalize("run")
    assert json.loads((final / "final.json").read_text())["terminal_state"] == "release_ready"


def test_repository_rejects_partial_stale_and_forged_evidence(tmp_path, monkeypatch):
    repo, _snapshot = _ready_repository(tmp_path, monkeypatch)
    (repo.pending / "run" / "evidence" / "documentation.json").unlink()
    with pytest.raises(ValueError, match="evidence set mismatch"):
        repo.finalize("run")

    repo, _snapshot = _ready_repository(tmp_path / "stale", monkeypatch)
    (tmp_path / "stale" / "app" / "a.py").write_text("newer = 3\n")
    with pytest.raises(ValueError, match="not current"):
        repo.finalize("run")

    repo, _snapshot = _ready_repository(tmp_path / "forged", monkeypatch)
    automation = repo.pending / "run" / "evidence" / "automation.json"
    value = json.loads(automation.read_text()); value["junit_xml"] = value["junit_xml"].replace("test_agent_has_cumulative_ticket_tools", "test_forged", 1)
    automation.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="unexpected selector"):
        repo.finalize("run")

    repo, _snapshot = _ready_repository(tmp_path / "revision", monkeypatch)
    regression = repo.pending / "run" / "evidence" / "regression.json"
    value = json.loads(regression.read_text()); value["final"]["source_revision"] = "workspace-source-v1:" + "f" * 64
    regression.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="final regression revision mismatch"):
        repo.finalize("run")

    repo, _snapshot = _ready_repository(tmp_path / "foreign-run", monkeypatch)
    security = repo.pending / "run" / "evidence" / "security-network.json"
    value = json.loads(security.read_text()); value["run_id"] = "forged-run"
    security.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="belongs to another run"):
        repo.finalize("run")


def test_repository_derives_blocked_and_never_trusts_claimed_ready(tmp_path, monkeypatch):
    repo, _snapshot = _ready_repository(tmp_path, monkeypatch)
    defects = repo.pending / "run" / "evidence" / "defects.json"
    value = json.loads(defects.read_text())
    value["blocking"] = [{"check_id": "C04.01"}]
    defects.write_text(json.dumps(value))
    final = repo.finalize("run")
    assert json.loads((final / "final.json").read_text())["terminal_state"] == "blocked"


def test_repository_single_writer_lock_fails_closed(tmp_path):
    repo = EvidenceRepository(tmp_path / "store")
    with repo.lock("run"):
        with pytest.raises(RuntimeError, match="writer already active"):
            with repo.lock("run"):
                pass


def test_final_bundle_detects_tampering(tmp_path, monkeypatch):
    repo, _snapshot = _ready_repository(tmp_path, monkeypatch)
    final = repo.finalize("run")
    locator = final.relative_to(repo.root).as_posix()
    (final / "run.json").write_text("{}")
    with pytest.raises(ValueError, match="digest mismatch"):
        repo.load_final(locator)


def test_finalized_run_is_immutable_to_repository_writes(tmp_path, monkeypatch):
    repo, _snapshot = _ready_repository(tmp_path, monkeypatch)
    repo.finalize("run")
    with pytest.raises(FileNotFoundError):
        repo.write_record("run", "late.json", {})


def test_retention_eligibility_differs_for_pending_and_final(tmp_path):
    repo = EvidenceRepository(tmp_path / "store")
    pending = tmp_path / "pending"
    final = tmp_path / "final"
    pending.mkdir(); final.mkdir(); (final / "final.json").write_text("{}")
    old = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(pending, (old, old)); os.utime(final, (old, old))
    now = datetime.now(timezone.utc)
    assert repo.retention_eligible(pending, now) is True
    assert repo.retention_eligible(final, now) is False


def test_baseline_publication_is_atomic_and_single_writer(tmp_path, monkeypatch):
    monkeypatch.setattr("app.release_assurance.evidence._git_head", lambda _root: "head")
    (tmp_path / "app").mkdir(); (tmp_path / "scripts").mkdir()
    (tmp_path / "app" / "a.py").write_text("a=1\n"); (tmp_path / "scripts" / "a.py").write_text("a=1\n")
    snapshot = create_source_snapshot(tmp_path)
    receipt = {"baseline_receipt_id": "base", "source_snapshot": snapshot.model_dump(mode="json"),
               "source_roots": ["app", "scripts"], "test_command": "pytest",
               "command_sha256": "a" * 64, "environment_profile": {},
               "result": {"exit_status": 0}, "executed_at": "now"}
    repository = EvidenceRepository(tmp_path / "store")
    with repository.lock("baseline-base"):
        with pytest.raises(RuntimeError, match="writer already active"):
            publish_baseline_receipt(tmp_path / "store", receipt)
    assert not (tmp_path / "store" / "baseline-pending" / "base").exists()
    target = publish_baseline_receipt(tmp_path / "store", receipt)
    assert (target / "final.json").is_file()
    with pytest.raises(FileExistsError):
        publish_baseline_receipt(tmp_path / "store", receipt)


def test_baseline_rejects_source_root_mismatch(tmp_path):
    receipt = {"baseline_receipt_id": "base", "source_snapshot": {"revision": "workspace-source-v1:" + "a" * 64},
               "source_roots": ["app"], "test_command": "pytest", "command_sha256": "a" * 64,
               "environment_profile": {}, "result": {"exit_status": 0}, "executed_at": "now"}
    with pytest.raises(ValueError, match="source roots"):
        publish_baseline_receipt(tmp_path / "store", receipt)


def test_baseline_publish_crash_never_exposes_partial_final(tmp_path, monkeypatch):
    snapshot = {"revision": "workspace-source-v1:" + "a" * 64}
    receipt = {"baseline_receipt_id": "crash", "source_snapshot": snapshot,
               "source_roots": ["app", "scripts"], "test_command": "pytest",
               "command_sha256": "a" * 64, "environment_profile": {},
               "result": {"exit_status": 0}, "executed_at": "now"}
    original = os.replace

    def crash_on_directory(source, destination):
        if Path(source).is_dir():
            raise OSError("crash before publication")
        return original(source, destination)

    monkeypatch.setattr(os, "replace", crash_on_directory)
    with pytest.raises(OSError, match="crash"):
        publish_baseline_receipt(tmp_path / "store", receipt)
    assert not (tmp_path / "store" / "baselines" / ("a" * 64) / "crash").exists()
