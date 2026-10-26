"""The immutable U4 verification catalog and selector completeness checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal
import xml.etree.ElementTree as ET


Contract = Literal["C-03", "C-04", "C-05", "release"]


@dataclass(frozen=True, slots=True)
class RequiredVerificationCase:
    check_id: str
    contract_source: Contract
    procedure_key: str
    category: str
    selector: str
    required_for_release: bool = True


@dataclass(frozen=True, slots=True)
class SelectorExecution:
    check_id: str
    selector: str
    outcome: Literal["passed", "failed", "skipped"]


_PROCEDURES: dict[str, tuple[str, ...]] = {
    "C03": (
        "exact_three_tool_names", "duplicate_registration_fails_closed",
        "capability_is_not_serializable_or_model_visible",
        "lifecycle_order_is_atomic_and_idempotent",
        "create_ledger_survives_revocable_state_cleanup",
        "cursor_filter_identity_and_lifecycle_binding",
        "cursor_issue_resolve_exact_query_round_trip_and_hash_tamper_rejection",
        "pending_create_and_update_variant_repository_round_trip",
        "update_pending_atomic_replacement_preserves_old_on_failure",
        "update_execution_staged_active_retired_round_trip_and_server_only_field_non_exposure",
        "update_execution_activation_crash_recovery_and_tamper_rejection",
        "update_execution_lifecycle_and_orphan_cleanup",
        "concurrent_update_confirm_and_revise_has_one_authorized_winner",
        "presentation_history_restart_restore_and_text_fallback",
        "presentation_correlation_rejects_sequence_or_hash_mismatch",
    ),
    "C04": (
        "validation", "catalog_id_or_name_resolution", "explicit_now_freezing",
        "ledger_first_review_publication", "confirmation_from_server_current_turn",
        "consume_evidence_binding", "crash_before_evidence_binding_requires_reconfirmation",
        "cancellation", "claim_identity_and_deadline", "active_claim_non_takeover",
        "stale_claim_reconciliation", "retryable_after_reconcile", "retryable_no_apply",
        "proactive_expiry_refresh_before_first_POST", "durable_refresh_reservation",
        "refresh_port_atomic_commit", "refresh_generation_recovery_after_commit",
        "refresh_crash_without_second_invocation", "byte_identical_retry",
        "shared_total_attempt_ceiling", "idempotent_replay",
        "second_ambiguous_result_is_terminal", "lifecycle_preserves_historical_actor_binding",
        "retention", "secret_canary",
    ),
    "C05": (
        "allowed_fields", "forbidden_fields", "omit_vs_null", "no_op",
        "confirmation_from_server_current_turn", "confirmation_binding",
        "exact_quoted_if_match_from_validated_etag_and_version", "invalid_etag_forbids_patch",
        "conflict", "ambiguous_result", "total_patch_attempt_ceiling",
        "durable_update_refresh_reservation", "refresh_port_atomic_commit",
        "refresh_generation_recovery_without_second_invocation", "permission_delegation",
        "execution_store_round_trip", "server_only_field_non_exposure",
        "atomic_replacement_rollback", "exact_old_row_invalidation",
        "single_active_execution_per_actor_scope", "concurrent_confirm_revise",
        "staged_orphan_cleanup", "activation_crash_recovery", "lifecycle_terminal_cleanup",
        "response_loss_locator_isolation", "tamper_rejection", "secret_canary",
    ),
    "REL": (
        "baseline_full_suite_evidence", "final_full_suite_evidence",
        "changed_application_source_manifest_and_coverage",
        "unexpected_network_default_deny_and_bypass_probe",
        "all_secret_boundaries_canary_scan",
        "existing_API_UI_history_model_and_accessibility_regression",
        "README_and_live_smoke_separation",
    ),
}

_SELECTORS: dict[str, tuple[str, ...]] = {
    "C03": (
        "tests/test_ticket_update_integration.py::test_agent_has_cumulative_ticket_tools",
        "tests/test_ticket_registry.py::test_registry_rejects_duplicate_version_name_kind_and_factory_mismatch",
        "tests/test_ticket_create_security.py::test_public_tool_schema_has_no_server_authority_or_transport_fields",
        "tests/test_ticket_lifecycle.py::test_conversation_clear_invalidates_state_then_presentation_and_history",
        "tests/test_ticket_lifecycle.py::test_successful_user_switch_preserves_u2_ledger_bytes_and_key",
        "tests/test_ticket_repositories.py::test_cursor_binding_mismatch_fails_closed_without_consuming_owner_reference[capability0]",
        "tests/test_ticket_repositories.py::test_cursor_hash_tamper_and_expiry_fail_closed",
        "tests/test_ticket_repositories.py::test_pending_repository_round_trips_both_variants",
        "tests/test_ticket_repositories.py::test_update_replacement_insert_failure_rolls_back_old_row",
        "tests/test_ticket_update_repository.py::test_stage_and_get_round_trip_exact_server_fields",
        "tests/test_ticket_update_repository.py::test_tampered_row_fails_closed_on_read",
        "tests/test_ticket_repositories.py::test_pending_lifecycle_invalidation_is_idempotent",
        "tests/test_ticket_repositories.py::test_pending_consume_concurrency_returns_exactly_one_minimal_evidence",
        "tests/test_ticket_repositories.py::test_presentation_exact_correlation_round_trip_and_restart",
        "tests/test_ticket_repositories.py::test_presentation_corrupt_or_unknown_metadata_falls_back",
    ),
    "C04": (
        "tests/test_ticket_create_models.py::test_unknown_draft_field_is_rejected",
        "tests/test_ticket_create_operations.py::test_ambiguous_or_retired_catalog_is_rejected_before_ledger[asyncio]",
        "tests/test_ticket_create_models.py::test_normalization_captures_explicit_now_and_materializes_default_once",
        "tests/test_ticket_create_operations.py::test_prepare_commits_ledger_before_review_and_sends_no_post[asyncio]",
        "tests/test_ticket_create_operations.py::test_exact_current_turn_confirmation_creates_once_with_stored_binding[asyncio]",
        "tests/test_ticket_create_repository.py::test_confirmation_binding_is_idempotent_and_extends_retention_monotonically",
        "tests/test_ticket_create_operations.py::test_consume_before_bind_crash_republishes_same_attempt_for_new_confirmation[asyncio]",
        "tests/test_ticket_create_operations.py::test_cancel_terminates_revocable_authority_without_post[asyncio]",
        "tests/test_ticket_create_repository.py::test_concurrent_send_claim_has_one_winner_and_increments_once",
        "tests/test_ticket_create_repository.py::test_active_claim_cannot_be_taken_for_reconciliation_before_deadline",
        "tests/test_ticket_create_repository.py::test_reconciliation_preserves_send_claim_and_stale_result_writes_zero",
        "tests/test_ticket_create_operations.py::test_transport_ambiguity_reconciles_then_replays_same_bytes_and_key_once[asyncio]",
        "tests/test_ticket_create_operations.py::test_token_expired_reserves_then_refreshes_once_and_retries_same_binding[asyncio]",
        "tests/test_ticket_write_integration.py::test_configured_key_ring_refreshes_once_and_retries_same_attempt",
        "tests/test_ticket_create_repository.py::test_refresh_reservation_has_one_durable_owner",
        "tests/test_ticket_create_operations.py::test_create_operations_consumes_the_shared_c01_typed_result_fixture[asyncio]",
        "tests/test_ticket_create_operations.py::test_different_boot_restart_uses_generation_recovery_without_second_refresh[asyncio]",
        "tests/test_ticket_create_operations.py::test_same_boot_observer_does_not_recover_commit_before_registry[asyncio]",
        "tests/test_ticket_create_models.py::test_canonical_bytes_are_compact_ordered_utf8_and_omit_absent_optionals",
        "tests/test_ticket_create_operations.py::test_second_ambiguous_result_is_terminal_and_third_post_is_zero[asyncio]",
        "tests/test_ticket_create_repository.py::test_create_prepared_is_durable_and_idempotent_for_exact_row",
        "tests/test_ticket_create_security.py::test_safe_gateway_failure_does_not_carry_raw_problem_or_exception_data",
        "tests/test_ticket_lifecycle.py::test_logout_cleans_old_authority_before_session_delete",
        "tests/test_ticket_create_repository.py::test_cleanup_keeps_row_before_and_deletes_at_exact_boundary",
        "tests/test_ticket_create_security.py::test_key_and_bytes_canaries_stay_out_of_api_logs_and_deliberate_failure_output",
    ),
    "C05": (
        "tests/test_ticket_update_models.py::test_each_allowed_patch_field_is_accepted[title-changed]",
        "tests/test_ticket_update_models.py::test_patch_rejects_empty_and_unknown_fields",
        "tests/test_ticket_update_models.py::test_patch_preserves_omitted_and_explicit_null",
        "tests/test_ticket_update_models.py::test_actual_diff_rejects_noop_and_keeps_only_changes",
        "tests/test_ticket_update_operations.py::test_confirm_requires_current_turn_explicit_command",
        "tests/test_ticket_update_operations.py::test_confirm_binding_mismatch_never_consumes_or_patches",
        "tests/test_ticket_update_operations.py::test_gateway_sends_exact_patch_bytes_and_if_match_once",
        "tests/test_ticket_update_operations.py::test_gateway_rejects_invalid_if_match_before_network",
        "tests/test_ticket_update_operations.py::test_conflict_reconciles_applied_change_without_patch_replay",
        "tests/test_ticket_update_operations.py::test_ambiguous_partial_result_creates_new_review_not_patch_replay",
        "tests/test_ticket_update_models.py::test_attempt_policy_enforces_patch_and_refresh_ceilings",
        "tests/test_ticket_update_repository.py::test_refresh_reservation_is_durable_and_one_shot",
        "tests/test_ticket_update_operations.py::test_create_refresh_port_accepts_shared_c01_schema_and_typed_results[asyncio]",
        "tests/test_ticket_update_repository.py::test_refresh_reconciliation_never_reissues_reservation[3-committed]",
        "tests/test_ticket_update_operations.py::test_gateway_public_contract_accepts_capability_and_raw_credential_is_confined[asyncio]",
        "tests/test_ticket_update_models.py::test_execution_round_trips_canonical_patch_and_hides_server_material",
        "tests/test_ticket_update_security.py::test_public_schema_excludes_server_transport_authority",
        "tests/test_ticket_update_repository.py::test_revise_atomically_retires_exact_old_and_promotes_new",
        "tests/test_ticket_update_repository.py::test_activation_rejects_wrong_binding_and_preserves_old",
        "tests/test_ticket_update_repository.py::test_prepare_does_not_replace_existing_active_row",
        "tests/test_ticket_update_operations.py::test_confirm_success_consumes_once_and_patches_once",
        "tests/test_ticket_update_repository.py::test_retire_scope_and_cleanup_are_idempotent",
        "tests/test_ticket_update_integration.py::test_next_prepare_recovers_u1_committed_staged_execution_without_restart[asyncio]",
        "tests/test_ticket_update_operations.py::test_cancel_retires_authority_and_never_patches",
        "tests/test_ticket_update_repository.py::test_exact_actor_ticket_locator_is_isolated",
        "tests/test_ticket_update_models.py::test_execution_rejects_hash_etag_and_retired_shape_tampering",
        "tests/test_ticket_update_security.py::test_instruction_shaped_ticket_text_stays_untrusted_data",
    ),
    "REL": (
        "tests/test_release_assurance_evidence.py::test_baseline_publication_is_atomic_and_single_writer",
        "tests/test_release_assurance_evidence.py::test_repository_derives_release_ready_from_closed_evidence",
        "tests/test_changed_line_coverage.py::test_u4_union_requires_exact_unit_bindings",
        "tests/test_network_isolation.py::test_release_evidence_requires_zero_unexpected_and_one_blocked_bypass",
        "tests/test_release_assurance_security.py::test_safe_counts_and_digest_are_allowed_without_payload",
        "tests/test_release_assurance_integration.py::test_bundle_round_trip_and_rollback_locator",
        "tests/test_release_assurance_documentation.py::test_repository_readme_satisfies_contract",
    ),
}


def _category(check_id: str, procedure: str) -> str:
    if check_id == "REL.07":
        return "documentation"
    for marker, category in (
        ("secret", "security"), ("network", "network"), ("coverage", "coverage"),
        ("regression", "regression"), ("lifecycle", "lifecycle"),
        ("presentation", "accessibility"),
    ):
        if marker.lower() in procedure.lower():
            return category
    return "contract" if not check_id.startswith("REL") else "regression"


def _build_catalog() -> tuple[RequiredVerificationCase, ...]:
    cases: list[RequiredVerificationCase] = []
    for prefix, procedures in _PROCEDURES.items():
        contract: Contract = "release" if prefix == "REL" else f"C-{prefix[-2:]}"  # type: ignore[assignment]
        for number, procedure in enumerate(procedures, start=1):
            check_id = f"{prefix}.{number:02d}"
            cases.append(
                RequiredVerificationCase(
                    check_id=check_id,
                    contract_source=contract,
                    procedure_key=procedure,
                    category=_category(check_id, procedure),
                    selector=_SELECTORS[prefix][number - 1],
                )
            )
    return tuple(cases)


CATALOG = _build_catalog()
CATALOG_BY_ID = {case.check_id: case for case in CATALOG}
AUTOMATION_CASES = tuple(case for case in CATALOG if case.check_id != "REL.07")
DOCUMENTATION_CASE = CATALOG_BY_ID["REL.07"]


def validate_catalog(cases: Iterable[RequiredVerificationCase] = CATALOG) -> None:
    materialized = tuple(cases)
    ids = [case.check_id for case in materialized]
    selectors = [case.selector for case in materialized]
    if len(materialized) != 74 or len(set(ids)) != 74:
        raise ValueError("verification catalog must contain exactly 74 unique check IDs")
    if len(set(selectors)) != 74:
        raise ValueError("every verification case must have one unique pytest selector")
    expected = {
        *(f"C03.{number:02d}" for number in range(1, 16)),
        *(f"C04.{number:02d}" for number in range(1, 26)),
        *(f"C05.{number:02d}" for number in range(1, 28)),
        *(f"REL.{number:02d}" for number in range(1, 8)),
    }
    if set(ids) != expected:
        raise ValueError("verification catalog differs from the approved contract")


def validate_collected_selectors(node_ids: Iterable[str]) -> None:
    collected = list(node_ids)
    duplicates = {node for node in collected if collected.count(node) > 1}
    missing = {case.selector for case in AUTOMATION_CASES} - set(collected)
    if duplicates:
        raise ValueError("duplicate collected selector(s): " + ", ".join(sorted(duplicates)))
    if missing:
        raise ValueError("missing catalog selector(s): " + ", ".join(sorted(missing)))


def validate_selector_results(results: Iterable[SelectorExecution]) -> tuple[SelectorExecution, ...]:
    materialized = tuple(results)
    expected = {case.check_id: case.selector for case in AUTOMATION_CASES}
    seen: dict[str, SelectorExecution] = {}
    for result in materialized:
        if result.check_id in seen:
            raise ValueError(f"duplicate selector result: {result.check_id}")
        if result.check_id not in expected:
            raise ValueError(f"unexpected selector result: {result.check_id}")
        if result.selector != expected[result.check_id]:
            raise ValueError(f"selector mismatch: {result.check_id}")
        seen[result.check_id] = result
    missing = set(expected) - set(seen)
    if missing:
        raise ValueError("missing selector result(s): " + ", ".join(sorted(missing)))
    not_passed = [item.check_id for item in materialized if item.outcome != "passed"]
    if not_passed:
        raise ValueError("selector result did not pass: " + ", ".join(sorted(not_passed)))
    return materialized


def selector_results_from_junit(xml_text: str) -> tuple[SelectorExecution, ...]:
    root = ET.fromstring(xml_text)
    by_selector = {case.selector: case.check_id for case in AUTOMATION_CASES}
    results: list[SelectorExecution] = []
    for testcase in root.iter("testcase"):
        classname = testcase.attrib.get("classname", "")
        name = testcase.attrib.get("name", "")
        selector = classname.replace(".", "/") + ".py::" + name
        if selector not in by_selector:
            raise ValueError(f"JUnit contains unexpected selector: {selector}")
        outcome: Literal["passed", "failed", "skipped"] = "passed"
        if testcase.find("skipped") is not None:
            outcome = "skipped"
        elif testcase.find("failure") is not None or testcase.find("error") is not None:
            outcome = "failed"
        results.append(SelectorExecution(by_selector[selector], selector, outcome))
    return validate_selector_results(results)


validate_catalog()
