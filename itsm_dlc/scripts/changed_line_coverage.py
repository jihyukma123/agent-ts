#!/usr/bin/env python3
"""Fail when changed executable application lines miss a coverage floor."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import os
from pathlib import Path
from typing import Iterable


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
PLAN_PATH = re.compile(r"^\|\s*`([^`]+)`\s*\|")
BACKTICK_PATH = re.compile(r"`([^`]+)`")


def matches(path: str, pattern: str | Iterable[str]) -> bool:
    patterns = (pattern,) if isinstance(pattern, str) else tuple(pattern)
    return any(
        fnmatch.fnmatch(path, item) or fnmatch.fnmatch(path, item.replace("/**/", "/"))
        for item in patterns
    )


def parse_changed_lines(diff: str) -> dict[str, set[int]]:
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            value = line[4:]
            current = None if value == "/dev/null" else value.removeprefix("b/")
            if current is not None:
                changed.setdefault(current, set())
            continue
        match = HUNK.match(line)
        if match and current is not None:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            changed[current].update(range(start, start + count))
    return changed


def coverage_lines(report: dict) -> dict[str, tuple[set[int], set[int]]]:
    result = {}
    for name, data in report.get("files", {}).items():
        normalized = name.replace("\\", "/").removeprefix("./")
        executed = set(data.get("executed_lines", []))
        missing = set(data.get("missing_lines", []))
        result[normalized] = (executed, executed | missing)
    return result


def manifest_paths(manifest: dict, *, include: str | Iterable[str]) -> set[str]:
    if set(manifest) != {"stage", "unit", "version", "writes"}:
        raise ValueError("source manifest does not match the strict schema")
    if manifest["stage"] != "code-generation" or manifest["version"] != 1:
        raise ValueError("source manifest stage or version is invalid")
    if not isinstance(manifest["unit"], str) or not manifest["unit"]:
        raise ValueError("source manifest unit is invalid")
    if not isinstance(manifest["writes"], list):
        raise ValueError("source manifest writes must be a list")
    paths = set()
    seen = set()
    for entry in manifest["writes"]:
        if not isinstance(entry, dict) or set(entry) != {"path"} or not entry.get("path"):
            raise ValueError("source manifest contains an invalid write entry")
        path = str(entry["path"]).replace("\\", "/").removeprefix("./")
        if path in seen:
            raise ValueError(f"source manifest repeats path: {path}")
        seen.add(path)
        if matches(path.rstrip("/"), include):
            paths.add(path)
    return paths


def approved_plan_paths(plan: str, *, include: str | Iterable[str]) -> set[str]:
    in_source_set = False
    paths = set()
    for line in plan.splitlines():
        if line.strip() == "## Planned Source Set":
            in_source_set = True
            continue
        if in_source_set and line.startswith("## "):
            break
        match = PLAN_PATH.match(line) if in_source_set else None
        if match:
            path = match.group(1).replace("\\", "/").removeprefix("./")
            if matches(path.rstrip("/"), include):
                paths.add(path)
    if not paths:
        # Older approved plans used "Planned Production Structure" plus a
        # precise Touches line rather than the later Planned Source Set table.
        # Preserve those immutable approvals without rewriting their record.
        for value in BACKTICK_PATH.findall(plan):
            path = value.replace("\\", "/").removeprefix("./")
            if path.endswith("/*"):
                path = path[:-1]
            if matches(path.rstrip("/"), include) or (
                path.endswith("/") and any(item.startswith(path.split("/")[0] + "/") for item in ((include,) if isinstance(include, str) else include))
            ):
                paths.add(path)
    if not paths:
        raise ValueError("approved plan has no matching source paths")
    return paths


def path_is_claimed(path: str, claims: set[str]) -> bool:
    return any(path == claim or (claim.endswith("/") and path.startswith(claim)) for claim in claims)


def source_baseline(text: str) -> dict[str, str]:
    """Parse the engine's root-repository source-baseline TSV."""
    baseline = {}
    for number, line in enumerate(text.splitlines(), start=1):
        fields = line.split("\t")
        if len(fields) != 4:
            raise ValueError(f"source baseline line {number} is malformed")
        repository, path, _mode, digest = fields
        if repository:
            continue
        normalized = path.replace("\\", "/").removeprefix("./")
        if not normalized or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"source baseline line {number} is invalid")
        if normalized in baseline:
            raise ValueError(f"source baseline repeats path: {normalized}")
        baseline[normalized] = digest
    if not baseline:
        raise ValueError("source baseline has no root-repository paths")
    return baseline


def changed_since_baseline(path: str, baseline: dict[str, str]) -> bool:
    candidate = Path(path)
    if not candidate.is_file():
        return path in baseline
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return baseline.get(path) != digest


def manifest_bound_changes(
    changed: dict[str, set[int]],
    *,
    claims: set[str],
    approved: set[str],
    prior_claims: set[str],
    baseline: dict[str, str],
    include: str | Iterable[str],
) -> tuple[dict[str, set[int]], set[str], set[str], set[str], set[str]]:
    out_of_plan_claims = {
        claim
        for claim in claims
        if not path_is_claimed(claim.rstrip("/"), approved)
    }
    stage_changed = {
        path
        for path in changed
        if matches(path, include) and changed_since_baseline(path, baseline)
    }
    approved_changed = {
        path for path in stage_changed if path_is_claimed(path, approved)
    }
    unclaimed_approved = {
        path for path in approved_changed if not path_is_claimed(path, claims)
    }
    scoped = {
        path: lines
        for path, lines in changed.items()
        if path in stage_changed and path_is_claimed(path, claims)
    }
    other_unit_changes = {
        path
        for path in changed
        if matches(path, include)
        and not path_is_claimed(path, approved)
        and (path_is_claimed(path, prior_claims) or path not in stage_changed)
    }
    unapproved_stage_changes = {
        path
        for path in stage_changed
        if not path_is_claimed(path, approved) and not path_is_claimed(path, prior_claims)
    }
    return (
        scoped,
        out_of_plan_claims,
        unclaimed_approved,
        other_unit_changes,
        unapproved_stage_changes,
    )


def calculate(
    changed: dict[str, set[int]], report: dict, *, include: str | Iterable[str]
) -> tuple[dict[str, tuple[int, int]], int, int]:
    measured = coverage_lines(report)
    by_file: dict[str, tuple[int, int]] = {}
    covered_total = 0
    executable_total = 0
    for path, lines in sorted(changed.items()):
        if not matches(path, include) or path not in measured:
            continue
        executed, executable = measured[path]
        changed_executable = lines & executable
        covered = len(changed_executable & executed)
        total = len(changed_executable)
        if total:
            by_file[path] = (covered, total)
            covered_total += covered
            executable_total += total
    return by_file, covered_total, executable_total


def git_output(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout


def workspace_changes(
    base: str, report: dict, include: str | Iterable[str]
) -> dict[str, set[int]]:
    changed = parse_changed_lines(
        git_output("diff", "--no-ext-diff", "--unified=0", "--find-renames", base, "--")
    )
    for path in git_output("diff", "--name-only", "--find-renames", base, "--").splitlines():
        normalized = path.replace("\\", "/")
        if matches(normalized, include):
            changed.setdefault(normalized, set())
    for path in git_output("ls-files", "--others", "--exclude-standard").splitlines():
        normalized = path.replace("\\", "/")
        if matches(normalized, include):
            file_coverage = coverage_lines(report).get(normalized)
            changed[normalized] = set(file_coverage[1]) if file_coverage is not None else set()
    return changed


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_binding(
    manifest_path: Path,
    plan_path: Path,
    baseline_path: Path,
    includes: tuple[str, ...],
) -> dict:
    manifest_bytes = manifest_path.read_bytes()
    plan_bytes = plan_path.read_bytes()
    baseline_bytes = baseline_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    claims = manifest_paths(manifest, include=includes)
    approved = approved_plan_paths(plan_bytes.decode(), include=includes)
    baseline = source_baseline(baseline_bytes.decode())
    outside = {path for path in claims if not path_is_claimed(path, approved)}
    if outside:
        raise ValueError("source manifest claims path(s) outside the approved source set: " + ", ".join(sorted(outside)))
    changed_approved = {
        path for path in approved if changed_since_baseline(path, baseline)
    }
    unclaimed = {path for path in changed_approved if not path_is_claimed(path, claims)}
    if unclaimed:
        raise ValueError("changed approved source path(s) are not claimed: " + ", ".join(sorted(unclaimed)))
    return {
        "unit": manifest["unit"],
        "manifest_path": manifest_path.as_posix(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "approved_plan_path": plan_path.as_posix(),
        "approved_plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "source_baseline_path": baseline_path.as_posix(),
        "source_baseline_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "claims": sorted(claims),
    }


def freeze_inputs(
    output: Path,
    manifests: list[Path],
    plans: list[Path],
    baselines: list[Path],
    includes: tuple[str, ...],
    *,
    u4_union: bool = False,
) -> dict:
    if not (len(manifests) == len(plans) == len(baselines)):
        raise ValueError("manifest, approved-plan, and source-baseline binding counts differ")
    bindings = [
        build_binding(manifest, plan, baseline, includes)
        for manifest, plan, baseline in zip(manifests, plans, baselines, strict=True)
    ]
    units = [binding["unit"] for binding in bindings]
    if len(units) != len(set(units)):
        raise ValueError("coverage bindings repeat a unit")
    expected_units = {
        "u1-ticket-read-skeleton", "u2-ticket-write", "u3-ticket-update", "u4-release-assurance"
    }
    if u4_union and set(units) != expected_units:
        raise ValueError(
            "U4 union requires exactly U1/U2/U3/U4 bindings; got: " + ", ".join(sorted(units))
        )
    union = sorted({path for binding in bindings for path in binding["claims"]})
    if not union:
        raise ValueError("coverage union contains no application source")
    value = {
        "version": 1,
        "includes": list(includes),
        "bindings": bindings,
        "mode": "u4-union" if u4_union else "legacy",
        "union_paths": union,
        "union_digest": hashlib.sha256("\n".join(union).encode()).hexdigest(),
    }
    value["frozen_digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    atomic_json_write(output, value)
    return value


def verify_frozen_inputs(path: Path, includes: tuple[str, ...], *, u4_union: bool = False) -> dict:
    frozen = json.loads(path.read_text())
    digest = frozen.pop("frozen_digest", None)
    actual = hashlib.sha256(
        json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    frozen["frozen_digest"] = digest
    if digest != actual:
        raise ValueError("frozen coverage input digest mismatch")
    if tuple(frozen.get("includes", ())) != includes:
        raise ValueError("frozen coverage include patterns changed")
    if frozen.get("mode") != ("u4-union" if u4_union else "legacy"):
        raise ValueError("frozen coverage mode mismatch")
    union: set[str] = set()
    for binding in frozen.get("bindings", []):
        for key in ("manifest", "approved_plan", "source_baseline"):
            candidate = Path(binding[f"{key}_path"])
            if file_digest(candidate) != binding[f"{key}_sha256"]:
                raise ValueError(f"frozen coverage binding drift: {candidate}")
        rebuilt = build_binding(
            Path(binding["manifest_path"]),
            Path(binding["approved_plan_path"]),
            Path(binding["source_baseline_path"]),
            includes,
        )
        if rebuilt != binding:
            raise ValueError(f"frozen coverage binding changed: {binding['unit']}")
        union.update(binding["claims"])
    sorted_union = sorted(union)
    if sorted_union != frozen.get("union_paths"):
        raise ValueError("frozen coverage union changed")
    if u4_union:
        expected = {"u1-ticket-read-skeleton", "u2-ticket-write", "u3-ticket-update", "u4-release-assurance"}
        units = {binding["unit"] for binding in frozen["bindings"]}
        if units != expected or len(frozen["bindings"]) != 4:
            raise ValueError("U4 union frozen input does not contain exactly four required units")
    return frozen


def u4_union_changed_lines(changed: dict[str, set[int]], frozen: dict) -> dict[str, set[int]]:
    scoped: dict[str, set[int]] = {}
    for binding in frozen["bindings"]:
        baseline = source_baseline(Path(binding["source_baseline_path"]).read_text())
        for path in binding["claims"]:
            if path in changed and changed_since_baseline(path, baseline):
                scoped[path] = changed[path]
    return scoped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--coverage-json", type=Path)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--prior-source-manifest", type=Path, action="append", default=[])
    parser.add_argument("--approved-plan", type=Path, required=True)
    parser.add_argument("--prior-approved-plan", type=Path, action="append", default=[])
    parser.add_argument("--source-baseline", type=Path, required=True)
    parser.add_argument("--prior-source-baseline", type=Path, action="append", default=[])
    parser.add_argument("--include", action="append", required=True)
    parser.add_argument("--fail-under", type=float)
    parser.add_argument("--freeze-output", type=Path)
    parser.add_argument("--frozen-input", type=Path)
    parser.add_argument("--u4-union", action="store_true")
    args = parser.parse_args(argv)
    includes = tuple(args.include)
    manifests = [args.source_manifest, *args.prior_source_manifest]
    plans = [args.approved_plan, *args.prior_approved_plan]
    baselines = [args.source_baseline, *args.prior_source_baseline]
    try:
        if args.freeze_output:
            freeze_inputs(args.freeze_output, manifests, plans, baselines, includes, u4_union=args.u4_union)
            print(f"frozen coverage inputs: {args.freeze_output}")
            return 0
        if args.base is None or args.coverage_json is None or args.fail_under is None:
            parser.error("gate mode requires --base, --coverage-json, and --fail-under")
        report = json.loads(args.coverage_json.read_text())
        changed = workspace_changes(args.base, report, includes)
        claims = manifest_paths(json.loads(args.source_manifest.read_text()), include=includes)
        prior_claims = set().union(
            *(
                manifest_paths(json.loads(path.read_text()), include=includes)
                for path in args.prior_source_manifest
            )
        )
        approved = approved_plan_paths(args.approved_plan.read_text(), include=includes)
        baseline = source_baseline(args.source_baseline.read_text())
        if args.frozen_input:
            frozen = verify_frozen_inputs(args.frozen_input, includes, u4_union=args.u4_union)
            claims = set(frozen["union_paths"])
            prior_claims = set()
            approved = claims
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    scoped, out_of_plan, unclaimed, other_unit, unapproved_stage = manifest_bound_changes(
        changed,
        claims=claims,
        approved=approved,
        prior_claims=prior_claims,
        baseline=baseline,
        include=includes,
    )
    if args.u4_union:
        if not args.frozen_input:
            print("error: --u4-union requires --frozen-input in gate mode", file=sys.stderr)
            return 2
        scoped = u4_union_changed_lines(changed, frozen)
    measured = coverage_lines(report)
    missing_sources = {path for path in claims if path not in measured}
    if missing_sources:
        print("error: missing coverage source: " + ", ".join(sorted(missing_sources)), file=sys.stderr)
        return 2
    by_file, covered, total = calculate(scoped, report, include=includes)
    percentage = (covered * 100 / total) if total else 0.0
    print(f"base: {args.base}")
    print(f"source manifest: {args.source_manifest}")
    print(f"prior source manifests: {len(args.prior_source_manifest)}")
    print(f"approved plan: {args.approved_plan}")
    print(f"source baseline: {args.source_baseline}")
    print(f"ignored prior-unit/pre-stage changed source paths: {len(other_unit)}")
    for path, counts in by_file.items():
        print(f"{path}: {counts[0]}/{counts[1]}")
    print(f"changed executable lines: {covered}/{total} ({percentage:.2f}%)")
    if out_of_plan:
        print(
            "error: source manifest claims path(s) outside the approved source set: "
            + ", ".join(sorted(out_of_plan)),
            file=sys.stderr,
        )
        return 2
    if unclaimed:
        print(
            "error: changed approved source path(s) are not claimed by the unit manifest: "
            + ", ".join(sorted(unclaimed)),
            file=sys.stderr,
        )
        return 2
    if unapproved_stage:
        print(
            "error: source path(s) changed since the stage baseline are outside the approved source set: "
            + ", ".join(sorted(unapproved_stage)),
            file=sys.stderr,
        )
        return 2
    if total == 0:
        print("error: no changed executable lines matched", file=sys.stderr)
        return 2
    return 0 if percentage >= args.fail_under else 1


if __name__ == "__main__":
    raise SystemExit(main())
