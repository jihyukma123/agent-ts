import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "changed_line_coverage.py"
SPEC = importlib.util.spec_from_file_location("changed_line_coverage", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def report(files):
    return {"files": files}


def test_parse_added_and_modified_hunks():
    diff = """diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -1,2 +1,3 @@
diff --git a/app/b.py b/app/b.py
--- /dev/null
+++ b/app/b.py
@@ -0,0 +1,2 @@
"""
    assert MODULE.parse_changed_lines(diff) == {"app/a.py": {1, 2, 3}, "app/b.py": {1, 2}}


def test_deleted_file_has_no_new_changed_lines():
    diff = """diff --git a/app/deleted.py b/app/deleted.py
--- a/app/deleted.py
+++ /dev/null
@@ -1,2 +0,0 @@
"""
    assert MODULE.parse_changed_lines(diff) == {}


def test_rename_uses_destination_path_and_new_hunks():
    diff = """diff --git a/app/old.py b/app/new.py
similarity index 80%
rename from app/old.py
rename to app/new.py
--- a/app/old.py
+++ b/app/new.py
@@ -2 +2,2 @@
"""
    assert MODULE.parse_changed_lines(diff) == {"app/new.py": {2, 3}}


def test_calculate_ignores_non_executable_changed_lines():
    changed = {"app/a.py": {1, 2, 3, 4}}
    coverage = report({"app/a.py": {"executed_lines": [2], "missing_lines": [4]}})
    files, covered, total = MODULE.calculate(changed, coverage, include="app/**/*.py")
    assert files == {"app/a.py": (1, 2)} and (covered, total) == (1, 2)


def test_exact_eighty_percent_meets_threshold_math():
    changed = {"app/pkg/a.py": set(range(1, 11))}
    coverage = report(
        {"app/pkg/a.py": {"executed_lines": list(range(1, 9)), "missing_lines": [9, 10]}}
    )
    _, covered, total = MODULE.calculate(changed, coverage, include="app/**/*.py")
    assert covered * 100 / total == 80


def test_empty_denominator_is_reported_as_zero_not_success():
    files, covered, total = MODULE.calculate(
        {"docs/readme.md": {1}}, report({}), include="app/**/*.py"
    )
    assert files == {} and covered == total == 0


def test_windows_paths_are_normalized_from_coverage_json():
    normalized = MODULE.coverage_lines(
        report({"app\\pkg\\a.py": {"executed_lines": [1], "missing_lines": [2]}})
    )
    assert normalized == {"app/pkg/a.py": ({1}, {1, 2})}


def test_workspace_changes_keeps_deleted_and_coverage_less_untracked_paths(monkeypatch):
    def fake_git_output(*args):
        if args[:2] == ("diff", "--no-ext-diff"):
            return ""
        if args[:2] == ("diff", "--name-only"):
            return "app/deleted.py\n"
        if args[:2] == ("ls-files", "--others"):
            return "app/untracked.py\n"
        raise AssertionError(args)

    monkeypatch.setattr(MODULE, "git_output", fake_git_output)
    assert MODULE.workspace_changes("base", report({}), "app/**/*.py") == {
        "app/deleted.py": set(),
        "app/untracked.py": set(),
    }


def test_manifest_and_approved_plan_define_the_unit_application_scope():
    manifest = {
        "stage": "code-generation",
        "unit": "u2-ticket-write",
        "version": 1,
        "writes": [
            {"path": "app/ticketing/create_operations.py"},
            {"path": "tests/test_ticket_create_operations.py"},
        ],
    }
    plan = """## Planned Source Set

| Path | Action | Responsibility |
|---|---|---|
| `app/ticketing/create_operations.py` | create | policy |
| `app/ticketing/capability.py` | modify | port |
| `tests/test_ticket_create_operations.py` | create | tests |

## Implementation Plan
"""
    assert MODULE.manifest_paths(manifest, include="app/**/*.py") == {
        "app/ticketing/create_operations.py"
    }
    assert MODULE.approved_plan_paths(plan, include="app/**/*.py") == {
        "app/ticketing/create_operations.py",
        "app/ticketing/capability.py",
    }


def test_source_baseline_parses_root_repository_and_ignores_sibling_repository():
    digest = "a" * 64
    text = f"\tapp/a.py\t100644\t{digest}\nother\tapp/b.py\t100644\t{'b' * 64}\n"
    assert MODULE.source_baseline(text) == {"app/a.py": digest}


def test_changed_since_baseline_uses_content_hash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "app" / "a.py"
    path.parent.mkdir()
    path.write_text("before\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert MODULE.changed_since_baseline("app/a.py", {"app/a.py": digest}) is False
    path.write_text("after\n")
    assert MODULE.changed_since_baseline("app/a.py", {"app/a.py": digest}) is True


def _coverage_binding(tmp_path, unit="u4-release-assurance"):
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    source = app / "a.py"
    source.write_text("value = 1\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"stage": "code-generation", "unit": unit, "version": 1,
                                    "writes": [{"path": "app/a.py"}]}))
    plan = tmp_path / "plan.md"
    plan.write_text("## Planned Source Set\n\n| Path | Action | Responsibility |\n|---|---|---|\n| `app/a.py` | create | app |\n\n## Implementation Plan\n")
    baseline = tmp_path / "baseline.tsv"
    baseline.write_text(f"\tapp/a.py\t100644\t{'0' * 64}\n")
    return source, manifest, plan, baseline


def test_freeze_inputs_binds_manifest_plan_baseline_and_union(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _source, manifest, plan, baseline = _coverage_binding(tmp_path)
    output = tmp_path / "run" / "coverage-inputs.json"
    frozen = MODULE.freeze_inputs(output, [manifest], [plan], [baseline], ("app/**/*.py",))
    assert frozen["union_paths"] == ["app/a.py"]
    assert MODULE.verify_frozen_inputs(output, ("app/**/*.py",))["bindings"][0]["unit"] == "u4-release-assurance"


def test_freeze_rejects_binding_count_mismatch(tmp_path):
    _source, manifest, plan, baseline = _coverage_binding(tmp_path)
    with pytest.raises(ValueError, match="binding counts differ"):
        MODULE.freeze_inputs(tmp_path / "frozen.json", [manifest], [plan], [], ("app/**/*.py",))


def test_frozen_input_detects_plan_or_manifest_drift(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _source, manifest, plan, baseline = _coverage_binding(tmp_path)
    output = tmp_path / "frozen.json"
    MODULE.freeze_inputs(output, [manifest], [plan], [baseline], ("app/**/*.py",))
    plan.write_text(plan.read_text() + "\nchanged\n")
    with pytest.raises(ValueError, match="binding drift"):
        MODULE.verify_frozen_inputs(output, ("app/**/*.py",))


def test_repeatable_include_accepts_app_and_scripts(tmp_path):
    manifest = {"stage": "code-generation", "unit": "u4", "version": 1,
                "writes": [{"path": "app/a.py"}, {"path": "scripts/a.py"}, {"path": "tests/a.py"}]}
    assert MODULE.manifest_paths(manifest, include=("app/**/*.py", "scripts/**/*.py")) == {"app/a.py", "scripts/a.py"}


def test_calculate_requires_coverage_source_to_be_present_at_gate():
    measured = MODULE.coverage_lines(report({"app/a.py": {"executed_lines": [1], "missing_lines": []}}))
    claims = {"app/a.py", "app/missing.py"}
    assert claims - set(measured) == {"app/missing.py"}


def test_u4_union_requires_exact_unit_bindings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = []
    for number, unit in enumerate((
        "u1-ticket-read-skeleton", "u2-ticket-write", "u3-ticket-update", "u4-release-assurance"
    ), start=1):
        directory = tmp_path / f"u{number}"
        directory.mkdir()
        app = tmp_path / "app"
        app.mkdir(exist_ok=True)
        source = app / f"u{number}.py"
        source.write_text(f"value = {number}\n")
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps({"stage": "code-generation", "unit": unit, "version": 1,
                                        "writes": [{"path": f"app/u{number}.py"}]}))
        plan = directory / "plan.md"
        plan.write_text(f"## Planned Source Set\n\n| Path | Action | Responsibility |\n|---|---|---|\n| `app/u{number}.py` | create | app |\n\n## Implementation Plan\n")
        baseline = directory / "baseline.tsv"
        baseline.write_text(f"\tapp/u{number}.py\t100644\t{'0' * 64}\n")
        paths.append((manifest, plan, baseline))
    manifests, plans, baselines = map(list, zip(*paths, strict=True))
    frozen = MODULE.freeze_inputs(
        tmp_path / "frozen.json", manifests, plans, baselines, ("app/**/*.py",), u4_union=True
    )
    assert frozen["mode"] == "u4-union" and len(frozen["bindings"]) == 4
    with pytest.raises(ValueError, match="exactly U1/U2/U3/U4"):
        MODULE.freeze_inputs(
            tmp_path / "missing.json", manifests[:-1], plans[:-1], baselines[:-1],
            ("app/**/*.py",), u4_union=True,
        )


def test_u4_union_uses_each_units_own_baseline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = tmp_path / "app"; app.mkdir()
    (app / "a.py").write_text("new\n")
    old = hashlib.sha256(b"old\n").hexdigest()
    current = hashlib.sha256(b"new\n").hexdigest()
    baseline_old = tmp_path / "old.tsv"; baseline_old.write_text(f"\tapp/a.py\t100644\t{old}\n")
    baseline_current = tmp_path / "current.tsv"; baseline_current.write_text(f"\tapp/a.py\t100644\t{current}\n")
    frozen = {"bindings": [
        {"claims": ["app/a.py"], "source_baseline_path": str(baseline_old)},
        {"claims": ["app/a.py"], "source_baseline_path": str(baseline_current)},
    ]}
    assert MODULE.u4_union_changed_lines({"app/a.py": {1}}, frozen) == {"app/a.py": {1}}


def test_manifest_bound_scope_ignores_prior_unit_changes_and_rejects_unclaimed_approved_path(
    monkeypatch,
):
    changed = {
        "app/ticketing/create_operations.py": {1},
        "app/ticketing/capability.py": {2},
        "app/ticketing/read_operations.py": {3},
    }
    monkeypatch.setattr(MODULE, "changed_since_baseline", lambda _path, _baseline: True)
    scoped, out_of_plan, unclaimed, other_unit, unapproved_stage = MODULE.manifest_bound_changes(
        changed,
        claims={"app/ticketing/create_operations.py"},
        approved={
            "app/ticketing/create_operations.py",
            "app/ticketing/capability.py",
        },
        prior_claims={"app/ticketing/read_operations.py"},
        baseline={},
        include="app/**/*.py",
    )
    assert scoped == {"app/ticketing/create_operations.py": {1}}
    assert out_of_plan == set()
    assert unclaimed == {"app/ticketing/capability.py"}
    assert other_unit == {"app/ticketing/read_operations.py"}
    assert unapproved_stage == set()


def test_manifest_bound_scope_rejects_claim_and_stage_change_outside_approved_source_set(
    monkeypatch,
):
    monkeypatch.setattr(MODULE, "changed_since_baseline", lambda _path, _baseline: True)
    _, out_of_plan, _, _, unapproved_stage = MODULE.manifest_bound_changes(
        {"app/agent/routes.py": {1}},
        claims={"app/agent/routes.py"},
        approved={"app/ticketing/create_operations.py"},
        prior_claims=set(),
        baseline={},
        include="app/**/*.py",
    )
    assert out_of_plan == {"app/agent/routes.py"}
    assert unapproved_stage == {"app/agent/routes.py"}


def test_manifest_bound_scope_ignores_pre_stage_existing_change(monkeypatch):
    monkeypatch.setattr(MODULE, "changed_since_baseline", lambda _path, _baseline: False)
    scoped, _, _, existing, unapproved = MODULE.manifest_bound_changes(
        {"app/legacy.py": {1}},
        claims=set(),
        approved={"app/ticketing/create_operations.py"},
        prior_claims=set(),
        baseline={},
        include="app/**/*.py",
    )
    assert scoped == {}
    assert existing == {"app/legacy.py"}
    assert unapproved == set()
