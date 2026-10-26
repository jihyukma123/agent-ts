"""안전한 티켓 도구 결과를 서버 소유 V1 presentation으로 변환한다."""

from __future__ import annotations

from datetime import datetime

from app.ticketing.models import (
    ActionV1,
    ActionsBlockV1,
    FieldV1,
    FieldsBlockV1,
    ItemsBlockV1,
    ParagraphBlockV1,
    SafeTicketDetailV1,
    TextValueV1,
    TicketPresentationV1,
    TicketReadToolResultV1,
)


def _server(text: str) -> TextValueV1:
    return TextValueV1(text=text, origin="server_allowlist")


def _itsm(value: object | None) -> TextValueV1:
    if isinstance(value, datetime):
        text = value.isoformat()
    elif value is None:
        text = "-"
    else:
        text = str(value)
    return TextValueV1(text=text, origin="itsm_untrusted")


def _field(key: str, label: str, value: object | None) -> FieldV1:
    return FieldV1(key=key, label=label, value=_itsm(value))


def _detail_fields(detail: SafeTicketDetailV1) -> list[FieldV1]:
    return [
        _field("id", "티켓 ID", detail.id),
        _field("title", "제목", detail.title),
        _field("status", "상태", detail.status),
        _field("priority", "우선순위", detail.priority),
        _field("service", "서비스", detail.service),
        _field("impact_level", "영향도", detail.impact_level),
        _field("urgency", "긴급도", detail.urgency),
        _field("version", "버전", detail.version),
    ]


def ticket_read_presentation(result: TicketReadToolResultV1) -> TicketPresentationV1:
    """도구 결과만 소비하며 원문 오류나 자격 증명을 해석하지 않는다."""
    if result.state == "failed":
        assert result.error is not None
        kind_by_code = {
            "authentication_required": "ticket_auth_error",
            "rate_limited": "ticket_rate_limited",
            "conflict": "ticket_conflict",
            "ambiguous_result": "ticket_ambiguous",
            "invalid_input": "ticket_input",
        }
        kind = kind_by_code.get(result.error.code, "ticket_unavailable")
        label_by_key = {
            "sign_in_required": "로그인이 필요합니다.",
            "authentication_required": "인증을 갱신한 뒤 다시 시도해 주세요.",
            "ticket_rate_limited": "요청이 많습니다. 잠시 후 다시 시도해 주세요.",
            "ticket_not_found_or_not_visible": "티켓을 찾을 수 없거나 볼 수 없습니다.",
            "ticket_permission_denied": "이 티켓을 조회할 권한이 없습니다.",
            "ticket_continuation_invalid": "목록이 만료되었습니다. 처음부터 다시 조회해 주세요.",
            "ticket_input_invalid": "조회 조건을 확인해 주세요.",
            "ticket_contract_invalid": "티켓 응답을 안전하게 확인할 수 없습니다.",
            "ticket_service_unavailable": "티켓 서비스를 사용할 수 없습니다.",
        }
        message = label_by_key.get(result.error.message_key, "티켓 조회를 완료하지 못했습니다.")
        blocks = [ParagraphBlockV1(text=_server(message))]
        if result.error.retry_after_seconds is not None:
            blocks.append(
                FieldsBlockV1(fields=[_field("retry_after_seconds", "다시 시도까지(초)", result.error.retry_after_seconds)])
            )
        return TicketPresentationV1(version=1, kind=kind, blocks=blocks)

    if result.mode == "detail":
        assert result.detail is not None
        return TicketPresentationV1(
            version=1,
            kind="ticket_detail",
            blocks=[
                ParagraphBlockV1(text=_server("티켓 상세 조회가 완료되었습니다.")),
                FieldsBlockV1(fields=_detail_fields(result.detail)),
                ParagraphBlockV1(text=TextValueV1(text=result.detail.description, origin="itsm_untrusted")),
            ],
        )

    assert result.items is not None and result.page is not None
    if result.state == "empty":
        return TicketPresentationV1(
            version=1,
            kind="ticket_list",
            blocks=[ParagraphBlockV1(text=_server("조회 조건에 맞는 티켓이 없습니다."))],
        )
    rows = [
        [
            _field("id", "티켓 ID", item.id),
            _field("title", "제목", item.title),
            _field("status", "상태", item.status),
            _field("priority", "우선순위", item.priority),
            _field("service", "서비스", item.service),
            _field("updated_at", "수정 시각", item.updated_at),
        ]
        for item in result.items
    ]
    blocks = [
        ParagraphBlockV1(text=_server(f"티켓 {len(rows)}건을 조회했습니다.")),
        ItemsBlockV1(items=rows),
    ]
    if result.page.has_more:
        blocks.extend(
            [
                ParagraphBlockV1(text=_server("다음 페이지가 있습니다.")),
                ActionsBlockV1(actions=[ActionV1(action="provide_input", label="다음 페이지 보기")]),
            ]
        )
    return TicketPresentationV1(version=1, kind="ticket_list", blocks=blocks)
