"""우선순위 매트릭스와 SLA 계산.

- 우선순위는 영향도(impact_level) x 긴급도(urgency)로 서버가 계산합니다. 클라이언트가 직접 지정할 수 없습니다.
- P1·P2 는 24x7, P3·P4 는 업무시간(평일 09:00–18:00 KST, 회사 휴무일 제외) 기준입니다.
- 보류(pending) 기간은 해결 SLA 시계에서 제외합니다(sla_pauses 테이블).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from itsm_api.timeutil import KST, parse_iso, to_utc_iso

PRIORITY_MATRIX: dict[tuple[str, str], str] = {
    ("extensive", "critical"): "P1",
    ("extensive", "high"): "P1",
    ("extensive", "medium"): "P2",
    ("extensive", "low"): "P3",
    ("significant", "critical"): "P1",
    ("significant", "high"): "P2",
    ("significant", "medium"): "P3",
    ("significant", "low"): "P3",
    ("moderate", "critical"): "P2",
    ("moderate", "high"): "P3",
    ("moderate", "medium"): "P3",
    ("moderate", "low"): "P4",
    ("minor", "critical"): "P3",
    ("minor", "high"): "P3",
    ("minor", "medium"): "P4",
    ("minor", "low"): "P4",
}


def compute_priority(impact_level: str, urgency: str) -> str:
    return PRIORITY_MATRIX[(impact_level, urgency)]


BUSINESS_START = time(9, 0)
BUSINESS_END = time(18, 0)
BUSINESS_DAY_SECONDS = 9 * 3600

# 회사 휴무일 (2026년, 대체공휴일 포함)
HOLIDAYS: frozenset[date] = frozenset(
    date(2026, m, d)
    for m, d in [
        (1, 1), (2, 16), (2, 17), (2, 18), (3, 2), (5, 5), (5, 25), (6, 3),
        (8, 17), (9, 24), (9, 25), (10, 5), (10, 9), (12, 25),
    ]
)  # fmt: skip


@dataclass(frozen=True)
class SlaPolicy:
    priority: str
    response_seconds: int
    resolution_seconds: int
    business_hours: bool

    @property
    def name(self) -> str:
        return f"{self.priority} {'업무시간' if self.business_hours else '24x7'}"


SLA_POLICIES: dict[str, SlaPolicy] = {
    "P1": SlaPolicy("P1", 15 * 60, 4 * 3600, business_hours=False),
    "P2": SlaPolicy("P2", 30 * 60, 8 * 3600, business_hours=False),
    "P3": SlaPolicy("P3", 4 * 3600, 3 * BUSINESS_DAY_SECONDS, business_hours=True),
    "P4": SlaPolicy("P4", BUSINESS_DAY_SECONDS, 5 * BUSINESS_DAY_SECONDS, business_hours=True),
}


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def _day_window(day: date) -> tuple[datetime, datetime]:
    return datetime.combine(day, BUSINESS_START, KST), datetime.combine(day, BUSINESS_END, KST)


def add_business_seconds(start: datetime, seconds: int) -> datetime:
    current = start.astimezone(KST)
    remaining = seconds
    while True:
        day = current.date()
        open_at, close_at = _day_window(day)
        if not is_business_day(day) or current >= close_at:
            current = datetime.combine(day + timedelta(days=1), time(0, 0), KST)
            continue
        if current < open_at:
            current = open_at
        available = int((close_at - current).total_seconds())
        if remaining <= available:
            return current + timedelta(seconds=remaining)
        remaining -= available
        current = datetime.combine(day + timedelta(days=1), time(0, 0), KST)


def business_seconds_between(start: datetime, end: datetime) -> int:
    if end <= start:
        return 0
    current = start.astimezone(KST)
    end = end.astimezone(KST)
    total = 0
    while current < end:
        day = current.date()
        if is_business_day(day):
            open_at, close_at = _day_window(day)
            lo, hi = max(current, open_at), min(end, close_at)
            if hi > lo:
                total += int((hi - lo).total_seconds())
        current = datetime.combine(day + timedelta(days=1), time(0, 0), KST)
    return total


def elapsed(policy: SlaPolicy, start: datetime, end: datetime) -> int:
    if policy.business_hours:
        return business_seconds_between(start, end)
    return max(0, int((end - start).total_seconds()))


def advance(policy: SlaPolicy, start: datetime, seconds: int) -> datetime:
    if policy.business_hours:
        return add_business_seconds(start, seconds)
    return start + timedelta(seconds=seconds)


def evaluate(
    *,
    priority: str,
    status: str,
    created_at: str,
    responded_at: str | None,
    resolved_at: str | None,
    closed_at: str | None,
    pauses: list[tuple[str, str | None]],
    now: datetime,
) -> dict:
    """티켓 한 건의 SLA 상태를 계산합니다. pauses 는 (시작, 종료 또는 None=진행 중) 목록입니다."""
    policy = SLA_POLICIES[priority]
    start = parse_iso(created_at)

    response_due = advance(policy, start, policy.response_seconds)
    responded = parse_iso(responded_at) if responded_at else None
    response_breached = (responded or now) > response_due

    paused_seconds = 0
    paused_now = False
    for begin, end in pauses:
        if end is None:
            paused_now = True
        paused_seconds += elapsed(policy, parse_iso(begin), parse_iso(end) if end else now)

    resolution_due: datetime | None = None
    resolution_breached = False
    if status == "cancelled":
        pass
    elif paused_now:
        # 보류 중에는 마감 시각이 정해지지 않습니다. 보류 직전까지 초과 여부만 판단합니다.
        last_begin = parse_iso(max(b for b, e in pauses if e is None))
        used = elapsed(policy, start, last_begin) - (paused_seconds - elapsed(policy, last_begin, now))
        resolution_breached = used > policy.resolution_seconds
    else:
        resolution_due = advance(policy, start, policy.resolution_seconds + paused_seconds)
        finished = resolved_at or closed_at
        resolution_breached = (parse_iso(finished) if finished else now) > resolution_due

    return {
        "policy": policy.name,
        "business_hours": policy.business_hours,
        "response_due_at": to_utc_iso(response_due),
        "responded_at": responded_at,
        "response_breached": response_breached,
        "resolution_due_at": to_utc_iso(resolution_due) if resolution_due else None,
        "resolution_breached": resolution_breached,
        "paused": paused_now,
    }
