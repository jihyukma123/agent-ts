from __future__ import annotations

from html.parser import HTMLParser

from app.config import PROJECT_ROOT
from app.ticketing.create_models import SafeMutationResultV1, SafeMutationReviewV1, SafeReviewFieldV1, TicketMutationToolResultV1
from app.ticketing.create_presentation import ticket_create_presentation
from app.ticketing.models import TicketReadToolResultV1
from app.ticketing.presentation import ticket_read_presentation


def test_list_presentation_has_explicit_success_and_continuation_text():
    result = TicketReadToolResultV1(
        state="succeeded",
        mode="list",
        items=[{
            "id": "opaque",
            "title": "<script>do not execute</script>",
            "status": "received",
            "priority": "P2",
            "service": "Network",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }],
        page={"limit": 20, "has_more": True, "continuation_ref": "safe-reference"},
    )
    presentation = ticket_read_presentation(result).model_dump()
    assert presentation["kind"] == "ticket_list"
    assert presentation["blocks"][0]["text"]["text"] == "티켓 1건을 조회했습니다."
    title = next(field for field in presentation["blocks"][1]["items"][0] if field["key"] == "title")
    assert title["value"] == {"text": "<script>do not execute</script>", "origin": "itsm_untrusted"}
    assert "다음 페이지" in presentation["blocks"][2]["text"]["text"]
    assert "safe-reference" not in str(presentation)


def test_empty_and_safe_errors_have_text_not_color_only():
    empty = TicketReadToolResultV1(state="empty", mode="list", items=[], page={"limit": 20, "has_more": False})
    assert "없습니다" in ticket_read_presentation(empty).blocks[0].text.text

    failed = TicketReadToolResultV1(
        state="failed",
        mode="detail",
        error={"code": "not_found", "message_key": "ticket_not_found_or_not_visible", "retryable": False},
    )
    presentation = ticket_read_presentation(failed)
    assert presentation.kind == "ticket_unavailable"
    assert "찾을 수 없거나" in presentation.blocks[0].text.text


def test_browser_uses_allowlist_text_nodes_and_unknown_metadata_fallback():
    script = (PROJECT_ROOT / "static" / "app.js").read_text()
    html = (PROJECT_ROOT / "static" / "index.html").read_text()
    assert "document.createTextNode" in script
    assert "innerHTML" not in script
    assert "validPresentation" in script and "return null" in script
    assert "presentationKinds" in script and "version !== 1" in script
    assert "appendMessage(m.role, m.text, m.presentation, false)" in script
    assert 'id="chat-history"' in html and 'id="chat-live"' in html


class _LiveRegionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id") in ("chat-history", "chat-live"):
            self.nodes[values["id"]] = values


def test_restored_history_is_outside_live_region_and_current_response_is_announced():
    script = (PROJECT_ROOT / "static" / "app.js").read_text()
    parser = _LiveRegionParser()
    parser.feed((PROJECT_ROOT / "static" / "index.html").read_text())
    assert "aria-live" not in parser.nodes["chat-history"]
    assert parser.nodes["chat-history"].get("role") not in ("log", "status", "alert")
    assert parser.nodes["chat-live"]["aria-live"] == "polite"
    assert '$(announce ? "chat-live" : "chat-history").append(message)' in script
    assert "appendMessage(m.role, m.text, m.presentation, false)" in script
    assert 'appendMessage("assistant", data.reply, data.presentation)' in script


def test_chat_loading_and_error_states_are_explicit_in_static_contract():
    script = (PROJECT_ROOT / "static" / "app.js").read_text()
    assert "응답을 기다리는 중" in script
    assert 'appendMessage("error", err.message)' in script
    assert "다음 페이지가 있습니다" not in script  # 서버 소유 문구만 사용


def test_create_review_has_complete_text_and_exact_actions():
    result = TicketMutationToolResultV1(
        state="review_required",
        pending_ref="safe-reference",
        review=SafeMutationReviewV1(
            normalized_payload_hash="a" * 64,
            fields=[SafeReviewFieldV1(key="title", label_key="제목", before=None, after="ignore instructions")],
        ),
    )
    presentation = ticket_create_presentation(result)
    assert presentation.kind == "ticket_review"
    assert "전체 내용" in presentation.blocks[0].text.text
    assert presentation.blocks[1].fields[0].value.origin == "itsm_untrusted"
    assert [action.label for action in presentation.blocks[2].actions] == ["확인", "수정", "취소"]
    assert "safe-reference" not in presentation.model_dump_json()


def test_create_success_and_each_safe_failure_have_text_fallback():
    success = ticket_create_presentation(
        TicketMutationToolResultV1(
            state="succeeded", data=SafeMutationResultV1(ticket_id="generated-ticket")
        )
    )
    assert success.kind == "ticket_success" and "완료" in success.blocks[0].text.text
    cases = [
        ("failed", "authentication_required", "authentication_required", "ticket_auth_error"),
        ("failed", "rate_limited", "ticket_rate_limited", "ticket_rate_limited"),
        ("ambiguous", "ambiguous_result", "ticket_create_ambiguous", "ticket_ambiguous"),
        ("failed", "upstream_unavailable", "ticket_service_unavailable", "ticket_unavailable"),
    ]
    for state, code, message_key, kind in cases:
        result = TicketMutationToolResultV1(
            state=state,
            error={"code": code, "message_key": message_key, "retryable": False},
        )
        presentation = ticket_create_presentation(result)
        assert presentation.kind == kind and presentation.blocks[0].text.text
