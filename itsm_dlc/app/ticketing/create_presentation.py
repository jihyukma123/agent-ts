"""Text-first presentation mapping for safe U2 create results."""

from __future__ import annotations

from app.ticketing.create_models import TicketMutationToolResultV1
from app.ticketing.models import (
    ActionV1,
    ActionsBlockV1,
    FieldV1,
    FieldsBlockV1,
    ParagraphBlockV1,
    TextValueV1,
    TicketPresentationV1,
)


def _server(text: str) -> TextValueV1:
    return TextValueV1(text=text, origin="server_allowlist")


def ticket_create_presentation(result: TicketMutationToolResultV1) -> TicketPresentationV1:
    if result.state in ("review_required", "needs_explicit_confirmation", "revised"):
        assert result.review is not None
        heading = "등록할 티켓 전체 내용을 검토해 주세요."
        if result.state == "revised":
            heading = "수정한 티켓 전체 내용을 다시 검토해 주세요."
        fields = [
            FieldV1(
                key=field.key,
                label=field.label_key,
                value=TextValueV1(text="-" if field.after is None else str(field.after), origin="itsm_untrusted"),
            )
            for field in result.review.fields
        ]
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
        return TicketPresentationV1(
            kind="ticket_success",
            blocks=[
                ParagraphBlockV1(text=_server("티켓 등록이 완료되었습니다.")),
                FieldsBlockV1(
                    fields=[
                        FieldV1(
                            key="ticket_id",
                            label="티켓 ID",
                            value=TextValueV1(text=result.data.ticket_id, origin="itsm_untrusted"),
                        )
                    ]
                ),
            ],
        )
    if result.state == "cancelled":
        return TicketPresentationV1(
            kind="ticket_input",
            blocks=[ParagraphBlockV1(text=_server("티켓 등록을 취소했습니다."))],
        )
    assert result.error is not None
    kind_by_code = {
        "invalid_input": "ticket_input",
        "authentication_required": "ticket_auth_error",
        "rate_limited": "ticket_rate_limited",
        "ambiguous_result": "ticket_ambiguous",
        "conflict": "ticket_input",
    }
    message_by_key = {
        "ticket_input_invalid": "등록할 티켓 내용을 확인해 주세요.",
        "ticket_service_invalid": "서비스를 하나로 확인할 수 없습니다.",
        "ticket_component_invalid": "선택한 컴포넌트가 서비스에 속하지 않습니다.",
        "ticket_model_invalid": "모델을 하나로 확인할 수 없습니다.",
        "authentication_required": "인증을 갱신할 수 없습니다. 다시 로그인해 주세요.",
        "ticket_rate_limited": "요청이 많습니다. 잠시 후 다시 시도해 주세요.",
        "ticket_create_ambiguous": "등록 결과를 확인할 수 없습니다. 중복 등록하지 말고 관리자에게 확인해 주세요.",
        "ticket_review_invalidated": "검토가 만료되었거나 현재 사용자와 일치하지 않습니다. 처음부터 다시 작성해 주세요.",
        "ticket_create_conflict": "다른 등록 작업이 먼저 진행되었습니다. 현재 상태를 다시 확인해 주세요.",
    }
    message = message_by_key.get(result.error.message_key, "티켓 등록을 완료하지 못했습니다.")
    blocks = [ParagraphBlockV1(text=_server(message))]
    if result.error.retry_after_seconds is not None:
        blocks.append(
            FieldsBlockV1(
                fields=[
                    FieldV1(
                        key="retry_after_seconds",
                        label="다시 시도까지(초)",
                        value=TextValueV1(
                            text=str(result.error.retry_after_seconds), origin="server_allowlist"
                        ),
                    )
                ]
            )
        )
    if result.state in ("invalidated", "conflict"):
        blocks.append(ActionsBlockV1(actions=[ActionV1(action="start_over", label="처음부터 작성")]))
    elif result.error.retryable:
        blocks.append(ActionsBlockV1(actions=[ActionV1(action="retry_later", label="나중에 다시 시도")]))
    return TicketPresentationV1(kind=kind_by_code.get(result.error.code, "ticket_unavailable"), blocks=blocks)
