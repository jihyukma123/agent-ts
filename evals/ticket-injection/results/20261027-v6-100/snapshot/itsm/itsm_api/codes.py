"""티켓 코드값과 한글 표시명.

API 는 안정적인 코드값으로 저장·필터링하고, 응답에 한글 표시명(`*_label`)을 함께 담습니다.
"""

from __future__ import annotations

from typing import Literal

TicketStatus = Literal["received", "assigned", "investigating", "pending", "monitoring", "resolved", "closed", "cancelled"]
STATUS_LABELS: dict[str, str] = {
    "received": "접수",
    "assigned": "배정됨",
    "investigating": "조사 중",
    "pending": "보류",
    "monitoring": "모니터링 중",
    "resolved": "해결",
    "closed": "종료",
    "cancelled": "취소",
}
OPEN_STATUSES = ("received", "assigned", "investigating", "pending", "monitoring")
FINAL_STATUSES = ("closed", "cancelled")

ImpactLevel = Literal["extensive", "significant", "moderate", "minor"]
IMPACT_LABELS: dict[str, str] = {
    "extensive": "전사·전체 고객",
    "significant": "다수 팀·다수 고객",
    "moderate": "단일 팀·일부 고객",
    "minor": "개인",
}

Urgency = Literal["critical", "high", "medium", "low"]
URGENCY_LABELS: dict[str, str] = {
    "critical": "즉시 (업무 중단)",
    "high": "높음 (주요 기능 저하)",
    "medium": "보통 (우회 가능)",
    "low": "낮음",
}

Priority = Literal["P1", "P2", "P3", "P4"]
PRIORITY_LABELS: dict[str, str] = {"P1": "긴급", "P2": "높음", "P3": "보통", "P4": "낮음"}

ResolutionCode = Literal["fixed", "workaround", "vendor_fixed", "no_fault_found", "duplicate"]
RESOLUTION_LABELS: dict[str, str] = {
    "fixed": "근본 수정",
    "workaround": "우회 조치",
    "vendor_fixed": "외부 업체 수정",
    "no_fault_found": "문제 재현 안 됨",
    "duplicate": "중복 티켓",
}

PendingReason = Literal["awaiting_requester", "awaiting_vendor", "awaiting_change"]
PENDING_LABELS: dict[str, str] = {
    "awaiting_requester": "요청자 응답 대기",
    "awaiting_vendor": "외부 업체 대기",
    "awaiting_change": "변경 작업 일정 대기",
}

Classification = Literal["standard", "restricted"]
CLASSIFICATION_LABELS: dict[str, str] = {"standard": "일반", "restricted": "보안 제한"}

WorklogVisibility = Literal["public", "internal"]
WorklogKind = Literal["comment", "status_change", "system"]

ProblemStatus = Literal["open", "known_error", "resolved"]
PROBLEM_STATUS_LABELS: dict[str, str] = {"open": "분석 중", "known_error": "알려진 오류", "resolved": "해결"}
