"""Content-addressed snapshots and atomic release-assurance evidence storage."""

from __future__ import annotations

import hashlib
import json
import locale
import os
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import coverage
import pytest

from .models import EnvironmentProfile, SourceSnapshot
from .catalog import selector_results_from_junit


SECRET_PATTERN = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[a-z0-9._~-]+|sk-[a-z0-9_-]{8,}|"
    r"client[_-]?secret\s*[=:]|api[_-]?key\s*[=:]|credential\s*[=:])"
)


@dataclass(frozen=True, slots=True)
class NetworkFenceResult:
    unexpected_attempts: int
    deliberate_bypass_blocked: int

    @property
    def passed(self) -> bool:
        return self.unexpected_attempts == 0 and self.deliberate_bypass_blocked >= 1


def network_fence_result(*, unexpected_attempts: int, deliberate_bypass_blocked: int) -> NetworkFenceResult:
    if unexpected_attempts < 0 or deliberate_bypass_blocked < 0:
        raise ValueError("network counters cannot be negative")
    return NetworkFenceResult(unexpected_attempts, deliberate_bypass_blocked)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_output_digest(output: str) -> str:
    if SECRET_PATTERN.search(output):
        raise ValueError("secret boundary violation: command-output")
    return sha256_bytes(output.encode())


def normalized_command(argv: list[str]) -> str:
    if not argv or any("\x00" in item for item in argv):
        raise ValueError("command argv is invalid")
    return " ".join(argv)


def command_digest(argv: list[str]) -> str:
    return sha256_bytes(normalized_command(argv).encode())


def parse_junit_counts(xml_text: str) -> dict[str, int]:
    """Parse required JUnit counts without retaining testcase output."""
    root = ET.fromstring(xml_text)
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise ValueError("JUnit report contains no testsuite")
    total = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    failed = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    passed = total - failed - errors - skipped
    if total < 1 or min(passed, failed, errors, skipped) < 0:
        raise ValueError("JUnit result counts are invalid")
    return {
        "total_count": total,
        "passed_count": passed,
        "failed_count": failed + errors,
        "skipped_count": skipped,
    }


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def create_source_snapshot(root: Path, roots: tuple[str, ...] = ("app", "scripts")) -> SourceSnapshot:
    rows: list[tuple[str, str, str]] = []
    for name in roots:
        base = root / name
        if not base.exists():
            continue
        for candidate in base.rglob("*.py"):
            if candidate.is_file() and "__pycache__" not in candidate.parts:
                relative = candidate.relative_to(root).as_posix()
                mode = oct(candidate.stat().st_mode & 0o777)
                rows.append((relative, mode, sha256_bytes(candidate.read_bytes())))
    rows.sort()
    if not rows:
        raise ValueError("source snapshot contains no application source")
    head_revision = _git_head(root)
    path_bytes = canonical_bytes(rows)
    paths_digest = sha256_bytes(path_bytes)
    revision_digest = sha256_bytes(canonical_bytes({"head": head_revision, "paths": rows}))
    return SourceSnapshot(
        revision=f"workspace-source-v1:{revision_digest}",
        head_revision=head_revision,
        paths_digest=paths_digest,
        path_count=len(rows),
    )


def collect_environment_profile(root: Path) -> EnvironmentProfile:
    lock_digest = sha256_bytes((root / "requirements.txt").read_bytes())
    config_names = sorted(
        name for name in os.environ if name.startswith(("PYTHON", "PYTEST", "COVERAGE", "TZ"))
    )
    return EnvironmentProfile(
        python=platform.python_version(),
        executable=sys.executable,
        pytest=pytest.__version__,
        coverage=coverage.__version__,
        dependency_lock_sha256=lock_digest,
        os=platform.system(),
        architecture=platform.machine(),
        locale=locale.setlocale(locale.LC_CTYPE),
        timezone=os.environ.get("TZ", "system-default"),
        configuration_digest=sha256_bytes(canonical_bytes(config_names)),
    )


def publish_baseline_receipt(store: Path, receipt: dict[str, Any]) -> Path:
    """Publish one immutable baseline through a locked pending directory."""
    expected = {"baseline_receipt_id", "source_snapshot", "source_roots", "test_command",
                "command_sha256", "environment_profile", "result", "executed_at"}
    if set(receipt) != expected:
        raise ValueError("baseline receipt does not match the closed schema")
    if receipt["source_roots"] != ["app", "scripts"]:
        raise ValueError("baseline source roots must be app and scripts")
    receipt_id = receipt["baseline_receipt_id"]
    revision = receipt["source_snapshot"]["revision"].removeprefix("workspace-source-v1:")
    repository = EvidenceRepository(store)
    target = store / "baselines" / revision / receipt_id
    pending = store / "baseline-pending" / receipt_id
    with repository.lock(f"baseline-{receipt_id}"):
        if target.exists() or pending.exists():
            raise FileExistsError(f"baseline receipt already exists: {receipt_id}")
        pending.mkdir(parents=True, mode=0o700)
        repository._atomic_write(pending / "baseline.json", receipt)
        digest = sha256_bytes((pending / "baseline.json").read_bytes())
        repository._atomic_write(pending / "final.json", {
            "baseline_receipt_id": receipt_id,
            "source_snapshot": receipt["source_snapshot"]["revision"],
            "files": {"baseline.json": digest},
            "terminal_state": "passed" if receipt["result"]["exit_status"] == 0 else "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(pending, target)
        repository._fsync_directory(target.parent)
    return target


def _safe_relative(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError("evidence path must be workspace-relative")
    if candidate.suffix != ".json":
        raise ValueError("evidence records must be JSON")
    return candidate


class EvidenceRepository:
    """Single-writer, atomic repository with immutable finalized bundles."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pending = root / "pending"
        self.runs = root / "runs"
        self.locks = root / "locks"

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        lock_dir = self.locks / f"{run_id}.lock"
        self.locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            lock_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RuntimeError(f"release-assurance writer already active: {run_id}") from exc
        try:
            yield
        finally:
            lock_dir.rmdir()

    def create_run(self, run_id: str, source_revision: str) -> Path:
        if not run_id or "/" in run_id or ".." in run_id:
            raise ValueError("run_id is invalid")
        target = self.pending / run_id
        if target.exists() or any((self.runs / source_revision / run_id).glob("final.json")):
            raise FileExistsError(f"run already exists: {run_id}")
        target.mkdir(parents=True, mode=0o700)
        self._atomic_write(target / "run.json", {"run_id": run_id, "source_revision": source_revision})
        return target

    def write_record(self, run_id: str, relative_path: str, value: Any) -> str:
        target_dir = self.pending / run_id
        if not target_dir.is_dir():
            raise FileNotFoundError(f"pending run not found: {run_id}")
        relative = _safe_relative(relative_path)
        if relative.name == "final.json":
            raise ValueError("final.json is repository-owned")
        destination = target_dir / relative
        if destination.exists():
            raise FileExistsError(f"evidence record already exists: {relative_path}")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = canonical_bytes(value) + b"\n"
        self._atomic_write_bytes(destination, payload)
        return sha256_bytes(payload)

    FINAL_EVIDENCE_PATHS = {
        "run.json", "preflight.json", "evidence/automation.json",
        "evidence/regression.json", "evidence/coverage.json",
        "evidence/security-network.json", "evidence/documentation.json",
        "evidence/defects.json", "evidence/performance.json",
    }

    def finalize(self, run_id: str, *, predecessor_locator: str | None = None) -> Path:
        with self.lock(run_id):
            pending = self.pending / run_id
            if not pending.is_dir():
                raise FileNotFoundError(f"pending run not found: {run_id}")
            run = json.loads((pending / "run.json").read_text())
            self._require_keys(run, {"run_id", "source_revision"}, "run")
            current_snapshot = create_source_snapshot(self.root.parent)
            source_revision = current_snapshot.revision
            if run.get("source_revision") != source_revision:
                raise ValueError("run source revision is not current")
            present = {
                path.relative_to(pending).as_posix()
                for path in pending.rglob("*.json")
                if path.name != "final.json"
            }
            if present != self.FINAL_EVIDENCE_PATHS:
                missing = self.FINAL_EVIDENCE_PATHS - present
                extra = present - self.FINAL_EVIDENCE_PATHS
                raise ValueError(
                    "final evidence set mismatch; missing=" + ",".join(sorted(missing))
                    + "; extra=" + ",".join(sorted(extra))
                )
            terminal_state = self._derive_terminal_state(pending, current_snapshot, run_id)
            digests = {
                path: sha256_bytes((pending / path).read_bytes()) for path in sorted(present)
            }
            manifest = {
                "run_id": run_id,
                "source_revision": source_revision,
                "terminal_state": terminal_state,
                "predecessor_locator": predecessor_locator,
                "files": digests,
                "bundle_digest": sha256_bytes(canonical_bytes(digests)),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            self._atomic_write(pending / "final.json", manifest)
            destination = self.runs / source_revision.removeprefix("workspace-source-v1:") / run_id
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if destination.exists():
                raise FileExistsError(f"final run already exists: {run_id}")
            os.replace(pending, destination)
            self._fsync_directory(destination.parent)
            return destination

    def _derive_terminal_state(
        self, pending: Path, snapshot: SourceSnapshot, run_id: str
    ) -> str:
        is_blocked = False
        preflight = self._read_json(pending / "preflight.json")
        self._require_keys(preflight, {"run_id", "source_revision", "source_snapshot", "environment_profile", "baseline_locator", "baseline_digest", "source_roots"}, "preflight")
        self._require_identity(preflight, run_id, snapshot.revision, "preflight")
        if tuple(preflight["source_roots"]) != ("app", "scripts"):
            raise ValueError("baseline/final source roots differ from the approved roots")
        if preflight["source_snapshot"] != snapshot.model_dump(mode="json"):
            raise ValueError("preflight source snapshot is stale")
        environment = collect_environment_profile(self.root.parent).model_dump(mode="json")
        if preflight["environment_profile"] != environment:
            raise ValueError("preflight environment profile is stale")

        baseline_dir = self.root / preflight["baseline_locator"]
        baseline_path = baseline_dir / "baseline.json"
        if not baseline_path.is_file() or sha256_bytes(baseline_path.read_bytes()) != preflight["baseline_digest"]:
            raise ValueError("baseline locator or digest is invalid")
        baseline = self._read_json(baseline_path)
        if baseline.get("source_roots") != ["app", "scripts"]:
            raise ValueError("baseline source roots do not match final source roots")

        automation = self._read_json(pending / "evidence/automation.json")
        self._require_keys(automation, {"run_id", "source_revision", "junit_xml"}, "automation")
        self._require_identity(automation, run_id, snapshot.revision, "automation")
        try:
            selector_results_from_junit(automation["junit_xml"])
        except ValueError as exc:
            if str(exc).startswith("selector result did not pass"):
                is_blocked = True
            else:
                raise

        regression = self._read_json(pending / "evidence/regression.json")
        self._require_keys(regression, {"run_id", "source_revision", "baseline_locator", "baseline_digest", "baseline", "final"}, "regression")
        self._require_identity(regression, run_id, snapshot.revision, "regression")
        if regression["baseline_locator"] != preflight["baseline_locator"] or regression["baseline_digest"] != preflight["baseline_digest"]:
            raise ValueError("regression baseline reference mismatch")
        baseline_result = regression["baseline"]
        final_result = regression["final"]
        result_keys = {"source_revision", "test_command", "command_sha256", "environment_profile", "total_count", "passed_count", "failed_count", "skipped_count", "exit_status"}
        self._require_keys(baseline_result, result_keys, "baseline regression")
        self._require_keys(final_result, result_keys, "final regression")
        if baseline_result["source_revision"] != baseline["source_snapshot"]["revision"]:
            raise ValueError("baseline regression revision mismatch")
        if baseline_result["test_command"] != baseline["test_command"] or baseline_result["command_sha256"] != baseline["command_sha256"]:
            raise ValueError("baseline regression command does not match receipt")
        if baseline_result["environment_profile"] != baseline["environment_profile"]:
            raise ValueError("baseline regression environment does not match receipt")
        for key in ("total_count", "passed_count", "failed_count", "skipped_count", "exit_status"):
            if baseline_result[key] != baseline["result"][key]:
                raise ValueError("baseline regression result does not match receipt")
        if final_result["source_revision"] != snapshot.revision:
            raise ValueError("final regression revision mismatch")
        for key in ("test_command", "command_sha256", "environment_profile"):
            if baseline_result[key] != final_result[key]:
                raise ValueError(f"baseline/final regression {key} mismatch")
        if final_result["environment_profile"] != environment:
            raise ValueError("final regression environment is stale")
        for result, label in ((baseline_result, "baseline"), (final_result, "final")):
            if result["total_count"] != result["passed_count"] + result["failed_count"] + result["skipped_count"]:
                raise ValueError(f"{label} regression counts are invalid")
            if result["exit_status"] != 0 or result["failed_count"] or result["skipped_count"]:
                is_blocked = True

        coverage_value = self._read_json(pending / "evidence/coverage.json")
        self._require_keys(coverage_value, {"run_id", "source_revision", "covered_lines", "executable_lines", "percentage", "threshold", "input_drift", "missing_sources"}, "coverage")
        self._require_identity(coverage_value, run_id, snapshot.revision, "coverage")
        percentage = coverage_value["covered_lines"] * 100 / coverage_value["executable_lines"]
        if abs(percentage - coverage_value["percentage"]) > 0.01 or coverage_value["threshold"] < 80:
            raise ValueError("coverage evidence is internally inconsistent")
        if percentage < coverage_value["threshold"]:
            is_blocked = True
        if coverage_value["input_drift"] or coverage_value["missing_sources"]:
            raise ValueError("coverage evidence reports drift or missing sources")

        security = self._read_json(pending / "evidence/security-network.json")
        self._require_keys(security, {"run_id", "source_revision", "unexpected_network_attempts", "deliberate_bypass_blocked", "secret_canary_exposures"}, "security-network")
        self._require_identity(security, run_id, snapshot.revision, "security-network")
        counters = (security["unexpected_network_attempts"], security["deliberate_bypass_blocked"], security["secret_canary_exposures"])
        if any(not isinstance(value, int) or value < 0 for value in counters):
            raise ValueError("security/network boundary counters are invalid")
        if security["unexpected_network_attempts"] or security["deliberate_bypass_blocked"] < 1 or security["secret_canary_exposures"]:
            is_blocked = True
        documentation = self._read_json(pending / "evidence/documentation.json")
        self._require_keys(documentation, {"run_id", "source_revision", "check_id", "result", "topics", "default_automation_uses_real_services", "explicit_live_smoke_prerequisites"}, "documentation")
        self._require_identity(documentation, run_id, snapshot.revision, "documentation")
        if documentation["check_id"] != "REL.07" or documentation["result"] not in {"passed", "failed"}:
            raise ValueError("REL.07 documentation evidence is invalid")
        if documentation["result"] != "passed" or len(set(documentation["topics"])) != 7 or documentation["default_automation_uses_real_services"] or not documentation["explicit_live_smoke_prerequisites"]:
            is_blocked = True
        defects = self._read_json(pending / "evidence/defects.json")
        self._require_keys(defects, {"run_id", "source_revision", "blocking"}, "defects")
        self._require_identity(defects, run_id, snapshot.revision, "defects")
        if not isinstance(defects["blocking"], list):
            raise ValueError("blocking defects must be a list")
        if defects["blocking"]:
            is_blocked = True
        performance = self._read_json(pending / "evidence/performance.json")
        self._require_keys(performance, {"run_id", "source_revision", "status"}, "performance")
        self._require_identity(performance, run_id, snapshot.revision, "performance")
        if performance["status"] not in {"not_measured", "met", "not_met", "invalid"}:
            raise ValueError("performance status is invalid")
        return "blocked" if is_blocked else "release_ready"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError(f"evidence must be an object: {path.name}")
        return value

    @staticmethod
    def _require_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
        if set(value) != expected:
            raise ValueError(f"{label} evidence does not match the closed schema")

    @staticmethod
    def _require_identity(
        value: dict[str, Any], run_id: str, source_revision: str, label: str
    ) -> None:
        if value["run_id"] != run_id:
            raise ValueError(f"{label} evidence belongs to another run")
        if value["source_revision"] != source_revision:
            raise ValueError(f"{label} evidence source revision is stale")

    def load_final(self, locator: str) -> dict[str, Any]:
        relative = Path(locator)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("final locator is invalid")
        directory = self.root / relative
        manifest = json.loads((directory / "final.json").read_text())
        for name, expected in manifest["files"].items():
            if sha256_bytes((directory / name).read_bytes()) != expected:
                raise ValueError(f"final evidence digest mismatch: {name}")
        return manifest

    def retention_eligible(self, path: Path, now: datetime) -> bool:
        age = now - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return age >= (timedelta(days=90) if (path / "final.json").exists() else timedelta(days=7))

    @staticmethod
    def _atomic_write(path: Path, value: Any) -> None:
        EvidenceRepository._atomic_write_bytes(path, canonical_bytes(value) + b"\n")

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        EvidenceRepository._fsync_directory(path.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
