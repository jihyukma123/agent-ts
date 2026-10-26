from datetime import datetime, timezone

import pytest

from app.release_assurance.models import PerformanceMeasurementSession, PerformanceTrial
from app.release_assurance.performance import evaluate_performance, expected_trial_keys


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def session(status="measuring", invalid_reason=None):
    return PerformanceMeasurementSession(
        measurement_session_id="session", source_revision="workspace-source-v1:" + "a" * 64,
        deployment_id="deployment", observation_time=NOW, anonymized_role="support-agent",
        browser="browser/1", device_class="desktop", os="Darwin", viewport="1440x900",
        network_profile="wired", cache_state="warm", status=status, invalid_reason=invalid_reason,
    )


def trials(assistant=1.0, existing=2.0):
    result = []
    for method, scenario, number in sorted(expected_trial_keys()):
        duration = assistant if method == "assistant" else existing
        result.append(PerformanceTrial(
            trial_id=f"{method}-{scenario}-{number}", measurement_session_id="session",
            method=method, scenario=scenario, trial_number=number,
            generated_data_reference=f"generated-{scenario}-{number}", measured_at=NOW,
            monotonic_start=10.0, monotonic_end=10.0 + duration, duration_seconds=duration,
            completion_event="ticket-state-visible", success=True, external_screen_transitions=0,
        ))
    return result


def test_exactly_eighteen_fixed_trials_are_required():
    assert len(expected_trial_keys()) == 18
    assert evaluate_performance(session(), trials()[:-1]).status == "not_measured"


def test_assistant_faster_with_zero_transitions_meets_all_scenarios():
    outcome = evaluate_performance(session(), trials())
    assert outcome.status == "met"
    assert outcome.scenarios == {"read": "met", "create": "met", "update": "met"}


def test_failed_trial_is_not_replaced_or_excluded_from_median():
    values = trials()
    values[0] = values[0].model_copy(update={"success": False, "failure_reason": "normal failure"})
    assert evaluate_performance(session(), values).status == "not_met"


def test_external_transition_prevents_assistant_success():
    values = trials()
    index = next(i for i, item in enumerate(values) if item.method == "assistant" and item.scenario == "read")
    values[index] = values[index].model_copy(update={"external_screen_transitions": 1})
    outcome = evaluate_performance(session(), values)
    assert outcome.scenarios["read"] == "not_met"


def test_slower_assistant_is_not_met():
    assert evaluate_performance(session(), trials(assistant=3.0, existing=2.0)).status == "not_met"


def test_invalid_session_remains_invalid_without_fabricating_results():
    outcome = evaluate_performance(session("invalid", "browser fingerprint changed"), [])
    assert outcome.status == "invalid" and outcome.reason == "browser fingerprint changed"


def test_duplicate_trial_invalidates_session_result():
    values = trials()
    assert evaluate_performance(session(), [*values, values[0]]).status == "invalid"


def test_foreign_session_trial_invalidates_result():
    values = trials()
    values[0] = values[0].model_copy(update={"measurement_session_id": "foreign"})
    assert evaluate_performance(session(), values).status == "invalid"
