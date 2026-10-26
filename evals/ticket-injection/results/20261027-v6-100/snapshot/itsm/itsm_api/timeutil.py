"""시각 처리. 저장·응답은 모두 UTC ISO 8601(`2026-10-20T00:40:00Z`)입니다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Clock:
    """현재 시각 공급자. 테스트에서는 고정 시각 구현으로 바꿉니다."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return to_utc_iso(self.now())


class FixedClock(Clock):
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value = self.value + timedelta(**kwargs)
