"""Text-first, allowlisted presentation mapping for U3 update results."""

from __future__ import annotations

from app.ticketing.models import (
    ActionV1,
    ActionsBlockV1,
    FieldV1,
    FieldsBlockV1,
    ParagraphBlockV1,
    TextValueV1,
    TicketPresentationV1,
)
from app.ticketing.update_models import TicketUpdateToolResultV1


def _server(text: str) -> TextValueV1:
    return TextValueV1(text=text, origin="server_allowlist")


def _field(key: str, label: str, value: object) -> FieldV1:
    return FieldV1(
        key=key,
        label=label,
        value=TextValueV1(text="-" if value is None else str(value), origin="itsm_untrusted"),
    )


def ticket_update_presentation(result: TicketUpdateToolResultV1) -> TicketPresentationV1:
    if result.state in ("review_required", "needs_explicit_confirmation", "revised"):
        assert result.review is not None
        heading = {
            "review_required": "변경할 티켓 내용을 검토해 주세요.",
            "needs_explicit_confirmation": "변경 내용을 적용하려면 현재 메시지에서 확인해 주세요.",
            "revised": "수정한 티켓 변경 내용을 다시 검토해 주세요.",
        }[result.state]
        fields: list[FieldV1] = [
            _field("ticket_id", "티켓 ID", result.review.ticket_id),
            _field("base_version", "기준 버전", result.review.base_version),
        ]
        for change in result.review.fields:
            fields.append(_field(f"{change.key}_before", f"{change.label} (현재)", change.before))
            fields.append(_field(f"{change.key}_after", f"{change.label} (변경)", change.after))
        return TicketPresentationV1(
            kind="ticket_review",
            blocks=[
                ParagraphBlockV1(text=_server(heading)),
                FieldsBlockV1(fields=fields),
                ActionsBlockV1(
                    actions=[
                        ActionV1(action="confirm", label="확인"),
                        ActionV1(action="revise", label="수정"),
                        ActionV1(action="cancel", label="취소"),
                    ]
                ),
            ],
        )
    if result.state == "succeeded":
        assert result.data is not None
        fields = [
            _field("ticket_id", "티켓 ID", result.data.ticket_id),
            _field("new_version", "새 버전", result.data.new_version),
        ]
        fields.extend(
            _field(change.key, change.label, change.after)
            for change in result.data.applied_changes
        )
        return TicketPresentationV1(
            kind="ticket_success",
            blocks=[
                ParagraphBlockV1(text=_server("티켓 변경이 완료되었습니다.")),
                FieldsBlockV1(fields=fields),
            ],
        )
    if result.state == "cancelled":
        return TicketPresentationV1(
            kind="ticket_input",
            blocks=[ParagraphBlockV1(text=_server("티켓 변경을 취소했습니다."))],
        )

    assert result.error is not None
    kind_by_code = {
        "invalid_input": "ticket_input",
        "authentication_required": "ticket_auth_error",
        "conflict": "ticket_conflict",
        "rate_limited": "ticket_rate_limited",
        "ambiguous_result": "ticket_ambiguous",
    }
    message_by_key = {
        "ticket_update_no_changes": "현재 티켓과 다른 변경 내용을 입력해 주세요.",
        "ticket_update_review_invalidated": "검토가 만료되었거나 현재 사용자와 일치하지 않습니다. 다시 준비해 주세요.",
        "ticket_update_review_conflict": "다른 변경 작업이 먼저 진행되었습니다. 현재 상태를 다시 확인해 주세요.",
        "ticket_update_version_changed": "티켓이 검토 후 변경되었습니다. 최신 상태로 다시 검토해 주세요.",
        "ticket_update_conflict": "티켓이 변경되어 적용하지 않았습니다. 최신 상태로 다시 검토해 주세요.",
        "ticket_update_ambiguous": "변경 결과를 확인할 수 없습니다. 다시 전송하지 말고 현재 티켓을 확인해 주세요.",
        "ticket_update_rate_limited": "요청이 많습니다. 잠시 후 다시 시도해 주세요.",
        "authentication_required": "인증을 갱신할 수 없습니다. 다시 로그인해 주세요.",
        "credential_refresh_failed": "인증을 갱신할 수 없습니다. 다시 로그인해 주세요.",
        "credential_refresh_reservation_invalid": "인증 상태가 변경되었습니다. 다시 로그인해 주세요.",
    }
    message = message_by_key.get(result.error.message_key, "티켓 변경을 완료하지 못했습니다.")
    blocks = [ParagraphBlockV1(text=_server(message))]
    if result.error.retry_after_seconds is not None:
        blocks.append(
            FieldsBlockV1(
                fields=[
                    FieldV1(
                        key="retry_after_seconds",
                        label="다시 시도까지(초)",
                        value=_server(str(result.error.retry_after_seconds)),
                    )
                ]
            )
        )
    if result.state in ("invalidated", "conflict"):
        blocks.append(ActionsBlockV1(actions=[ActionV1(action="start_over", label="다시 검토")]))
    elif result.error.retryable:
        blocks.append(ActionsBlockV1(actions=[ActionV1(action="retry_later", label="나중에 다시 시도")]))
    return TicketPresentationV1(kind=kind_by_code.get(result.error.code, "ticket_unavailable"), blocks=blocks)
