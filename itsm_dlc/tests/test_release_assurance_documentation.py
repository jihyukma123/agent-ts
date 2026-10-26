from pathlib import Path

from app.release_assurance.documentation import TOPIC_MARKERS, verify_readme


def valid_readme():
    return """
ticket_read ticket_write ticket_update
ITSM_BASE_URL OPENAI_API_KEY
uvicorn
위임 비밀
pytest FakeModel MockTransport
changed_line_coverage.py 80%
기본 자동화는 mock만 사용합니다.
agent_smoke.py 는 별도 명시 실행이며 target user credential 이 필요합니다.
"""


def test_all_seven_topics_pass_with_explicit_separate_smoke():
    evidence = verify_readme(valid_readme(), "run")
    assert evidence.result == "passed" and len(evidence.readme_topics) == 7


def test_each_missing_topic_fails_closed():
    source = valid_readme()
    for markers in TOPIC_MARKERS.values():
        evidence = verify_readme(source.replace(markers[0], "missing", 1), "run")
        assert evidence.result == "failed"


def test_smoke_requires_target_user_and_credential():
    source = valid_readme().replace(" target", "")
    assert verify_readme(source, "run").result == "failed"


def test_smoke_requires_explicit_separation():
    source = valid_readme().replace("별도 명시", "자동")
    assert verify_readme(source, "run").result == "failed"


def test_repository_readme_satisfies_contract():
    readme = Path(__file__).parents[1].joinpath("README.md").read_text()
    assert verify_readme(readme, "repository").result == "passed"
