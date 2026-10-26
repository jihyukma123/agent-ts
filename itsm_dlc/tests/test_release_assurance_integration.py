import json
import subprocess
from pathlib import Path

from app.release_assurance.catalog import AUTOMATION_CASES
from app.release_assurance.documentation import verify_readme
from app.release_assurance.evidence import EvidenceRepository, create_source_snapshot


ROOT = Path(__file__).parents[1]
CLI = ROOT / "scripts" / "release_assurance.py"


def test_public_release_assurance_import_does_not_touch_fastapi_runtime():
    import app.release_assurance as assurance

    assert len(assurance.CATALOG) == 74


def test_cli_help_exposes_provider_independent_subcommands():
    result = subprocess.run([str(ROOT / ".venv/bin/python"), str(CLI), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    for command in ("baseline", "create-run", "record", "finalize", "show", "performance-evaluate"):
        assert command in result.stdout


def test_cli_rejects_unresolved_placeholders(tmp_path):
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(CLI), "--store", str(tmp_path / "store"),
         "create-run", "--run-id", "<RUN_ID>", "--baseline-locator", "base", "--baseline-digest", "a" * 64],
        capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_bundle_round_trip_and_rollback_locator(tmp_path, monkeypatch):
    from tests.test_release_assurance_evidence import _ready_repository

    repo, _snapshot = _ready_repository(tmp_path, monkeypatch)
    final = repo.finalize("run")
    loaded = repo.load_final(final.relative_to(repo.root).as_posix())
    assert loaded["terminal_state"] == "release_ready"
    assert set(loaded["files"]) == repo.FINAL_EVIDENCE_PATHS


def test_cli_does_not_accept_caller_asserted_terminal_state():
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(CLI), "finalize", "--run-id", "missing",
         "--terminal-state", "release_ready"], capture_output=True, text=True,
    )
    assert result.returncode != 0 and "unrecognized arguments" in result.stderr


def test_repository_readme_and_generated_bundle_contract_agree():
    readme = ROOT.joinpath("README.md").read_text()
    assert verify_readme(readme, "integration").result == "passed"
    assert ".release-assurance/" in ROOT.joinpath(".gitignore").read_text()


def test_source_snapshot_covers_app_and_scripts_application_code():
    snapshot = create_source_snapshot(ROOT)
    assert snapshot.path_count > 1 and snapshot.revision.startswith("workspace-source-v1:")
