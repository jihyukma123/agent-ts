from dataclasses import replace

import pytest

from app.release_assurance.catalog import (
    AUTOMATION_CASES,
    CATALOG,
    CATALOG_BY_ID,
    RequiredVerificationCase,
    SelectorExecution,
    validate_catalog,
    validate_collected_selectors,
    validate_selector_results,
    selector_results_from_junit,
)


@pytest.mark.parametrize("case", CATALOG, ids=lambda case: case.check_id)
def test_required_case_contract(case):
    assert case.required_for_release is True
    assert "test_required_case_contract" not in case.selector
    assert case.selector.startswith("tests/") and "::test_" in case.selector
    assert case.contract_source in {"C-03", "C-04", "C-05", "release"}


def test_catalog_has_exact_contract_partition():
    assert len(CATALOG) == 74
    assert len(AUTOMATION_CASES) == 73
    assert sum(case.check_id.startswith("C03") for case in CATALOG) == 15
    assert sum(case.check_id.startswith("C04") for case in CATALOG) == 25
    assert sum(case.check_id.startswith("C05") for case in CATALOG) == 27
    assert sum(case.check_id.startswith("REL") for case in CATALOG) == 7


def test_catalog_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="74 unique"):
        validate_catalog((*CATALOG[:-1], CATALOG[0]))


def test_catalog_rejects_duplicate_selectors():
    duplicate = replace(CATALOG[-1], selector=CATALOG[0].selector)
    with pytest.raises(ValueError, match="unique pytest selector"):
        validate_catalog((*CATALOG[:-1], duplicate))


def test_selector_completeness_rejects_missing_and_duplicate():
    collected = [case.selector for case in AUTOMATION_CASES]
    validate_collected_selectors(collected)
    with pytest.raises(ValueError, match="missing"):
        validate_collected_selectors(collected[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        validate_collected_selectors([*collected, collected[0]])


def test_selector_results_derive_evidence_and_fail_closed():
    results = [SelectorExecution(case.check_id, case.selector, "passed") for case in AUTOMATION_CASES]
    assert len(validate_selector_results(results)) == 73
    with pytest.raises(ValueError, match="missing"):
        validate_selector_results(results[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        validate_selector_results([*results, results[0]])
    with pytest.raises(ValueError, match="did not pass"):
        validate_selector_results([results[0].__class__(results[0].check_id, results[0].selector, "skipped"), *results[1:]])
    with pytest.raises(ValueError, match="mismatch"):
        validate_selector_results([results[0].__class__(results[0].check_id, "tests/wrong.py::test_wrong", "passed"), *results[1:]])


def test_junit_results_are_bound_to_exact_real_selectors():
    cases = []
    for case in AUTOMATION_CASES:
        path, name = case.selector.split("::", 1)
        classname = path.removesuffix(".py").replace("/", ".")
        cases.append(f'<testcase classname="{classname}" name="{name}" />')
    xml = '<testsuites><testsuite tests="73">' + "".join(cases) + "</testsuite></testsuites>"
    assert len(selector_results_from_junit(xml)) == 73
    with pytest.raises(ValueError, match="unexpected selector"):
        selector_results_from_junit(xml.replace(AUTOMATION_CASES[0].selector.split("::")[1], "test_forged", 1))


def test_rel_07_is_documentation_phase_only():
    assert CATALOG_BY_ID["REL.07"].category == "documentation"
    assert all(case.check_id != "REL.07" for case in AUTOMATION_CASES)
