"""우선순위 매트릭스와 업무시간 SLA 계산."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from itsm_api import sla
from itsm_api.timeutil import KST


def kst(*args) -> datetime:
    return datetime(*args, tzinfo=KST)


def test_priority_matrix_is_complete():
    assert len(sla.PRIORITY_MATRIX) == 16
    assert set(sla.PRIORITY_MATRIX.values()) == {"P1", "P2", "P3", "P4"}


@pytest.mark.parametrize(
    "start,seconds,expected",
    [
        (kst(2026, 10, 26, 10, 0), 3600, kst(2026, 10, 26, 11, 0)),  # 월요일 업무시간 안
        (kst(2026, 10, 23, 17, 0), 4 * 3600, kst(2026, 10, 26, 12, 0)),  # 금 17시 → 주말 건너뜀
        (kst(2026, 10, 24, 15, 0), 3600, kst(2026, 10, 26, 10, 0)),  # 토요일 시작
        (kst(2026, 10, 26, 7, 30), 3600, kst(2026, 10, 26, 10, 0)),  # 업무 시작 전
        (kst(2026, 10, 8, 17, 0), 4 * 3600, kst(2026, 10, 12, 12, 0)),  # 10/9 한글날 휴무
        (kst(2026, 10, 26, 10, 0), 27 * 3600, kst(2026, 10, 29, 10, 0)),  # 3 업무일
    ],
)
def test_add_business_seconds(start, seconds, expected):
    assert sla.add_business_seconds(start, seconds) == expected


def test_business_seconds_between():
    assert sla.business_seconds_between(kst(2026, 10, 23, 17, 0), kst(2026, 10, 26, 10, 0)) == 2 * 3600
    assert sla.business_seconds_between(kst(2026, 10, 24, 0, 0), kst(2026, 10, 25, 23, 0)) == 0
    assert sla.business_seconds_between(kst(2026, 10, 26, 12, 0), kst(2026, 10, 26, 11, 0)) == 0


def test_evaluate_pauses_extend_resolution_due():
    base = dict(priority="P2", status="investigating", created_at="2026-10-26T00:00:00Z",
                responded_at="2026-10-26T00:10:00Z", resolved_at=None, closed_at=None)  # fmt: skip
    now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    plain = sla.evaluate(**base, pauses=[], now=now)
    assert plain["resolution_due_at"] == "2026-10-26T08:00:00Z" and not plain["resolution_breached"]
    paused = sla.evaluate(**base, pauses=[("2026-10-26T01:00:00Z", "2026-10-26T02:30:00Z")], now=now)
    assert paused["resolution_due_at"] == "2026-10-26T09:30:00Z"
    open_pause = sla.evaluate(**base, pauses=[("2026-10-26T01:00:00Z", None)], now=now)
    assert open_pause["paused"] and open_pause["resolution_due_at"] is None and not open_pause["resolution_breached"]


def test_evaluate_breaches_and_cancelled():
    late = sla.evaluate(priority="P1", status="received", created_at="2026-10-26T00:00:00Z", responded_at=None,
                        resolved_at=None, closed_at=None, pauses=[],
                        now=datetime(2026, 10, 26, 0, 20, tzinfo=timezone.utc))  # fmt: skip
    assert late["response_breached"] and not late["resolution_breached"]
    cancelled = sla.evaluate(priority="P3", status="cancelled", created_at="2026-10-26T00:00:00Z", responded_at=None,
                             resolved_at=None, closed_at="2026-10-26T00:05:00Z", pauses=[],
                             now=datetime(2026, 11, 30, tzinfo=timezone.utc))  # fmt: skip
    assert cancelled["resolution_due_at"] is None and not cancelled["resolution_breached"]
