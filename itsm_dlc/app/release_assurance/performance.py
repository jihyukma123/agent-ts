"""NFR8 fixed-trial recording and honest outcome evaluation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Iterable

from .models import PerformanceMeasurementSession, PerformanceTrial


METHODS = ("assistant", "existing_ui")
SCENARIOS = ("read", "create", "update")


@dataclass(frozen=True, slots=True)
class PerformanceOutcome:
    status: str
    scenarios: dict[str, str]
    medians: dict[str, dict[str, float]]
    reason: str | None = None


def expected_trial_keys() -> set[tuple[str, str, int]]:
    return {
        (method, scenario, number)
        for method in METHODS for scenario in SCENARIOS for number in range(1, 4)
    }


def evaluate_performance(
    session: PerformanceMeasurementSession, trials: Iterable[PerformanceTrial]
) -> PerformanceOutcome:
    materialized = list(trials)
    if session.status == "invalid":
        return PerformanceOutcome("invalid", {}, {}, session.invalid_reason)
    keys = [(item.method, item.scenario, item.trial_number) for item in materialized]
    if len(keys) != len(set(keys)):
        return PerformanceOutcome("invalid", {}, {}, "duplicate fixed trial")
    if set(keys) != expected_trial_keys():
        return PerformanceOutcome("not_measured", {}, {}, "all 18 fixed trials are required")
    if any(item.measurement_session_id != session.measurement_session_id for item in materialized):
        return PerformanceOutcome("invalid", {}, {}, "trial belongs to another session")

    grouped: dict[tuple[str, str], list[PerformanceTrial]] = defaultdict(list)
    for item in materialized:
        grouped[(item.method, item.scenario)].append(item)
    outcomes: dict[str, str] = {}
    medians: dict[str, dict[str, float]] = {}
    for scenario in SCENARIOS:
        assistant = grouped[("assistant", scenario)]
        existing = grouped[("existing_ui", scenario)]
        if not all(item.success for item in assistant + existing):
            outcomes[scenario] = "not_met"
            continue
        assistant_median = median(item.duration_seconds for item in assistant)
        existing_median = median(item.duration_seconds for item in existing)
        medians[scenario] = {"assistant": assistant_median, "existing_ui": existing_median}
        no_external_transition = all(item.external_screen_transitions == 0 for item in assistant)
        outcomes[scenario] = (
            "met" if assistant_median < existing_median and no_external_transition else "not_met"
        )
    overall = "met" if all(value == "met" for value in outcomes.values()) else "not_met"
    return PerformanceOutcome(overall, outcomes, medians)
