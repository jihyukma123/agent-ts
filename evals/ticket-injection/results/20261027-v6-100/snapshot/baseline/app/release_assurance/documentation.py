"""README contract verifier for release automation and separate live smoke."""

from __future__ import annotations

import re

from .models import DocumentationAssurance


TOPIC_MARKERS = {
    "feature": ("ticket_read", "ticket_write", "ticket_update"),
    "configuration": ("ITSM_BASE_URL", "OPENAI_API_KEY"),
    "run": ("uvicorn",),
    "security_boundary": ("위임", "비밀"),
    "tests": ("pytest", "FakeModel", "MockTransport"),
    "coverage": ("changed_line_coverage.py", "80%"),
    "smoke_separation": ("agent_smoke.py", "명시", "별도"),
}


def verify_readme(readme: str, run_id: str) -> DocumentationAssurance:
    topics = frozenset(
        topic for topic, markers in TOPIC_MARKERS.items()
        if all(marker in readme for marker in markers)
    )
    smoke_section = readme[readme.find("agent_smoke.py") - 500:readme.find("agent_smoke.py") + 1000]
    prerequisites = all(marker in smoke_section for marker in ("target", "user", "credential"))
    explicit = bool(re.search(r"명시|explicit", smoke_section, re.IGNORECASE))
    automation_lines = [line for line in readme.splitlines() if "기본 자동화" in line]
    default_real = any(
        re.search(r"(실제|real).{0,30}(호출|사용)", line, re.IGNORECASE)
        and not re.search(r"(않|금지|never|not)", line, re.IGNORECASE)
        for line in automation_lines
    )
    passed = len(topics) == 7 and prerequisites and explicit and not default_real
    return DocumentationAssurance(
        documentation_assurance_id=f"docs-{run_id}",
        assurance_run_id=run_id,
        readme_topics=topics,
        smoke_prerequisites_documented=prerequisites,
        explicit_credentials_required=explicit,
        default_automation_uses_real_services=default_real,
        result="passed" if passed else "failed",
    )
