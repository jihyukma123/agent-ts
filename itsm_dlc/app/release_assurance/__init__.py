"""Public release-assurance API; isolated from FastAPI startup."""

from .catalog import AUTOMATION_CASES, CATALOG, CATALOG_BY_ID, DOCUMENTATION_CASE
from .documentation import verify_readme
from .evaluator import EvaluationResult, ReleaseAssuranceEvaluator, route_defect
from .evidence import EvidenceRepository, collect_environment_profile, create_source_snapshot
from .performance import PerformanceOutcome, evaluate_performance

__all__ = [
    "AUTOMATION_CASES", "CATALOG", "CATALOG_BY_ID", "DOCUMENTATION_CASE",
    "EvaluationResult", "EvidenceRepository", "PerformanceOutcome",
    "ReleaseAssuranceEvaluator", "collect_environment_profile", "create_source_snapshot",
    "evaluate_performance", "route_defect", "verify_readme",
]
