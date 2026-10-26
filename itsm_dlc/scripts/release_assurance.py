#!/usr/bin/env python3
"""Provider-independent CLI for local release-assurance evidence bundles."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.release_assurance.evidence import (  # noqa: E402
    EvidenceRepository,
    collect_environment_profile,
    command_digest,
    create_source_snapshot,
    publish_baseline_receipt,
    safe_output_digest,
)
from app.release_assurance.models import (  # noqa: E402
    PerformanceMeasurementSession,
    PerformanceTrial,
)
from app.release_assurance.performance import evaluate_performance  # noqa: E402


def _reject_placeholders(values: list[str]) -> None:
    if any("<" in value or ">" in value for value in values):
        raise ValueError("unresolved command placeholder is forbidden")


def capture_baseline(root: Path, store: Path) -> int:
    snapshot = create_source_snapshot(root, ("app", "scripts"))
    command = [".venv/bin/python", "-m", "pytest", "-q"]
    result = subprocess.run(command, cwd=root, text=True, capture_output=True)
    output = result.stdout + result.stderr
    summary = output.strip().splitlines()[-1] if output.strip() else ""
    counts = _pytest_counts(summary)
    receipt_id = f"baseline-{snapshot.paths_digest[:12]}"
    value = {
        "baseline_receipt_id": receipt_id,
        "source_snapshot": snapshot.model_dump(mode="json"),
        "source_roots": ["app", "scripts"],
        "test_command": " ".join(command),
        "command_sha256": command_digest(command),
        "environment_profile": collect_environment_profile(root).model_dump(mode="json"),
        "result": {**counts, "exit_status": result.returncode, "output_digest": safe_output_digest(summary)},
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }
    directory = publish_baseline_receipt(store, value)
    digest = __import__("hashlib").sha256((directory / "baseline.json").read_bytes()).hexdigest()
    print(json.dumps({"locator": directory.relative_to(store).as_posix(), "digest": digest}, sort_keys=True))
    return result.returncode


def _pytest_counts(summary: str) -> dict[str, int]:
    import re

    values = {name: 0 for name in ("passed", "failed", "skipped")}
    for count, name in re.findall(r"(\d+)\s+(passed|failed|skipped)", summary):
        values[name] = int(count)
    total = sum(values.values())
    if total == 0:
        raise ValueError("pytest result counts could not be parsed")
    return {
        "total_count": total,
        "passed_count": values["passed"],
        "failed_count": values["failed"],
        "skipped_count": values["skipped"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--store", type=Path, default=Path(".release-assurance"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("baseline")
    create = commands.add_parser("create-run")
    create.add_argument("--run-id", required=True)
    create.add_argument("--baseline-locator", required=True)
    create.add_argument("--baseline-digest", required=True)
    record = commands.add_parser("record")
    record.add_argument("--run-id", required=True)
    record.add_argument("--path", required=True)
    record.add_argument("--json-file", type=Path, required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--run-id", required=True)
    show = commands.add_parser("show")
    show.add_argument("--locator", required=True)
    performance = commands.add_parser("performance-evaluate")
    performance.add_argument("--session", type=Path, required=True)
    performance.add_argument("--trials", type=Path, required=True)
    args = parser.parse_args(argv)
    _reject_placeholders([str(value) for value in vars(args).values() if value is not None])
    root = args.root.resolve()
    store = args.store if args.store.is_absolute() else root / args.store
    repository = EvidenceRepository(store)
    if args.command == "baseline":
        return capture_baseline(root, store)
    if args.command == "create-run":
        snapshot = create_source_snapshot(root)
        repository.create_run(args.run_id, snapshot.revision)
        repository.write_record(args.run_id, "preflight.json", {
            "run_id": args.run_id,
            "source_revision": snapshot.revision,
            "source_snapshot": snapshot.model_dump(mode="json"),
            "environment_profile": collect_environment_profile(root).model_dump(mode="json"),
            "baseline_locator": args.baseline_locator,
            "baseline_digest": args.baseline_digest,
            "source_roots": ["app", "scripts"],
        })
        print(snapshot.revision)
        return 0
    if args.command == "record":
        digest = repository.write_record(args.run_id, args.path, json.loads(args.json_file.read_text()))
        print(digest)
        return 0
    if args.command == "finalize":
        location = repository.finalize(args.run_id)
        print(location.relative_to(store))
        return 0
    if args.command == "show":
        print(json.dumps(repository.load_final(args.locator), sort_keys=True, indent=2))
        return 0
    session = PerformanceMeasurementSession.model_validate_json(args.session.read_text())
    trials = [PerformanceTrial.model_validate(item) for item in json.loads(args.trials.read_text())]
    print(json.dumps(dataclasses.asdict(evaluate_performance(session, trials)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
