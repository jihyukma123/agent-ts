"""ITSM API 티켓 조회: 가시성, 검색·필터, 커서 페이지네이션, 통계, 상세, 카탈로그·디렉터리."""

from __future__ import annotations

import pytest

from itsm_api import seed_data
from tests.conftest import ItsmApi

RESTRICTED = {"INC-026", "INC-027", "INC-028"}
MEMBER01_VISIBLE = {
    "INC-001", "INC-002", "INC-004", "INC-005", "INC-010", "INC-014",
    "INC-017", "INC-023", "INC-024", "INC-027", "INC-032", "INC-035",
}  # fmt: skip


def all_ids(api: ItsmApi, username: str, **params) -> list[str]:
    ids: list[str] = []
    params = {"limit": 100, **params}
    while True:
        res = api.get("/api/v2/tickets", username, params=params)
        assert res.status_code == 200, res.text
        ids += [t["id"] for t in res.json()["items"]]
        if not res.json()["page"]["has_more"]:
            return ids
        params["cursor"] = res.json()["page"]["next_cursor"]


# ---------- 가시성 ----------


def test_requester_sees_own_and_major_tickets(api: ItsmApi):
    assert set(all_ids(api, "member01")) == MEMBER01_VISIBLE
    # 본인이 신고한 보안 제한 티켓은 보이지만 다른 보안 티켓은 보이지 않습니다.
    assert api.get("/api/v2/tickets/INC-027", "member01").status_code == 200
    assert api.get("/api/v2/tickets/INC-026", "member01").status_code == 404


@pytest.mark.parametrize("username", ["desk01", "api01", "lead01"])
def test_staff_see_standard_tickets_only(api: ItsmApi, username):
    ids = set(all_ids(api, username))
    assert len(ids) == len(seed_data.TICKETS) - len(RESTRICTED)
    assert not ids & RESTRICTED


@pytest.mark.parametrize("username", ["sec01", "admin01"])
def test_security_and_admin_see_everything(api: ItsmApi, username):
    assert len(all_ids(api, username)) == len(seed_data.TICKETS)


def test_invisible_ticket_is_indistinguishable_from_missing(api: ItsmApi):
    hidden = api.get("/api/v2/tickets/INC-026", "desk01").json()
    missing = api.get("/api/v2/tickets/INC-999", "desk01").json()
    assert hidden["code"] == missing["code"] == "ticket_not_found"
    for sub in ("worklogs", "transitions"):
        assert api.get(f"/api/v2/tickets/INC-026/{sub}", "desk01").status_code == 404


def test_internal_worklogs_hidden_from_requester(api: ItsmApi):
    requester = api.get("/api/v2/tickets/INC-001/worklogs", "member01").json()["items"]
    staff = api.get("/api/v2/tickets/INC-001/worklogs", "desk01").json()["items"]
    assert {w["visibility"] for w in requester} == {"public"}
    assert {w["visibility"] for w in staff} == {"public", "internal"}
    assert api.get("/api/v2/tickets/INC-001", "member01").json()["worklog_count"] == len(requester)
    assert api.get("/api/v2/tickets/INC-001", "desk01").json()["worklog_count"] == len(staff)
    res = api.get("/api/v2/tickets/INC-001/worklogs", "member01", params={"visibility": "internal"})
    assert res.status_code == 403


# ---------- 검색·필터 ----------


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"q": "Sora"}, {"INC-006", "INC-029"}),
        ({"q": "503"}, {"INC-002", "INC-031"}),
        ({"q": "Codex 로그인"}, {"INC-001", "INC-004", "INC-010", "INC-030"}),  # INC-010: "재로그인"
        ({"q": "inc-012"}, {"INC-012"}),
        ({"q": "없는검색어"}, set()),
        ({"model": "gpt-6-luna"}, {"INC-002", "INC-003", "INC-031"}),
        ({"service_id": "sso"}, {"INC-013", "INC-014"}),
        ({"status": ["pending"]}, {"INC-007", "INC-022"}),
        ({"priority": ["P1"], "status": ["investigating"]}, {"INC-002"}),
        ({"assignee": "none", "status": ["received"]}, {"INC-005", "INC-012", "INC-017", "INC-032"}),
        ({"is_major": True}, {"INC-001", "INC-002", "INC-024"}),
        ({"created_from": "2026-10-24T00:00:00+09:00", "created_to": "2026-10-25T00:00:00+09:00"},
         {"INC-012", "INC-017", "INC-032", "INC-034"}),
    ],
)  # fmt: skip
def test_search_and_filters(api: ItsmApi, params, expected):
    assert set(all_ids(api, "desk01", **params)) == expected


def test_me_filters(api: ItsmApi):
    assert set(all_ids(api, "api01", assignee="me")) == {"INC-002", "INC-009", "INC-018", "INC-021", "INC-036"}
    assert set(all_ids(api, "member01", requester="me")) == MEMBER01_VISIBLE - {"INC-002", "INC-024"}


def test_filter_validation(api: ItsmApi):
    assert api.get("/api/v2/tickets", "desk01", params={"status": "open"}).status_code == 422
    assert api.get("/api/v2/tickets", "desk01", params={"limit": 101}).status_code == 422
    naive = api.get("/api/v2/tickets", "desk01", params={"created_from": "2026-10-24T00:00:00"})
    assert naive.status_code == 400 and naive.json()["errors"][0]["code"] == "timezone_required"
    bad_assignee = api.get("/api/v2/tickets", "desk01", params={"assignee": "한도윤"})
    assert bad_assignee.status_code == 400 and bad_assignee.json()["code"] == "invalid_filter"
    assert api.get("/api/v2/tickets", params={"requester": "me"}).status_code == 403  # 서비스 토큰


# ---------- 페이지네이션·정렬 ----------


@pytest.mark.parametrize("sort", ["-created_at", "-updated_at", "priority"])
def test_cursor_pagination_visits_every_ticket_once(api: ItsmApi, sort):
    ids = all_ids(api, "admin01", sort=sort, limit=7)
    assert len(ids) == len(set(ids)) == len(seed_data.TICKETS)
    if sort == "priority":
        priorities = [api.get(f"/api/v2/tickets/{i}", "admin01").json()["priority"] for i in ids]
        assert priorities == sorted(priorities)


def test_default_page_size_and_cursor_guards(api: ItsmApi):
    first = api.get("/api/v2/tickets", "admin01").json()
    assert len(first["items"]) == 20 and first["page"] == {"limit": 20, "next_cursor": first["page"]["next_cursor"], "has_more": True}
    cursor = first["page"]["next_cursor"]
    changed = api.get("/api/v2/tickets", "admin01", params={"cursor": cursor, "q": "Codex"})
    assert changed.status_code == 400 and changed.json()["code"] == "invalid_cursor"
    garbage = api.get("/api/v2/tickets", "admin01", params={"cursor": "%%%"})
    assert garbage.status_code == 400 and garbage.json()["code"] == "invalid_cursor"


# ---------- 통계 ----------


@pytest.mark.parametrize("group_by", ["status", "priority", "service", "assignment_group"])
@pytest.mark.parametrize("username", ["member01", "desk01", "sec01"])
def test_stats_match_visible_tickets(api: ItsmApi, group_by, username):
    stats = api.get("/api/v2/tickets/stats", username, params={"group_by": group_by}).json()
    assert stats["total"] == len(all_ids(api, username)) == sum(b["count"] for b in stats["buckets"])


def test_stats_with_filters(api: ItsmApi):
    stats = api.get("/api/v2/tickets/stats", "desk01",
                    params={"group_by": "status", "status": ["received", "assigned", "investigating", "pending", "monitoring"]}).json()  # fmt: skip
    counts = {b["key"]: b["count"] for b in stats["buckets"]}
    assert counts["pending"] == 2 and "resolved" not in counts
    labels = {b["key"]: b["label"] for b in stats["buckets"]}
    assert labels["investigating"] == "조사 중"


# ---------- 상세 ----------


def test_ticket_detail(api: ItsmApi):
    res = api.get("/api/v2/tickets/inc-001", "member01")
    assert res.status_code == 200 and res.headers["ETag"] == '"1"'
    t = res.json()
    assert t["id"] == "INC-001" and t["priority"] == "P2" and t["priority_label"] == "높음"
    assert t["service"] == {"id": "codex", "name": "Codex"} and t["component"] == {"id": 1, "name": "로그인"}
    assert t["assignment_group"]["slug"] == "identity" and t["assignee"]["username"] == "idops01"
    assert t["problem"] == {"id": "PRB-001", "title": "인증 세션 처리 반복 장애", "status": "open"}
    assert t["sla"] == {
        "policy": "P2 24x7",
        "business_hours": False,
        "response_due_at": "2026-10-20T01:25:00Z",
        "responded_at": "2026-10-20T01:05:00Z",
        "response_breached": False,
        "resolution_due_at": "2026-10-20T08:55:00Z",
        "resolution_breached": True,
        "paused": False,
    }
    assert t["sla_breached"] is True and t["version"] == 1
    # 병합된 중복 티켓은 직원에게만 보입니다(요청자는 INC-030 을 볼 수 없음).
    assert t["duplicates"] == []
    assert api.get("/api/v2/tickets/INC-001", "desk01").json()["duplicates"] == ["INC-030"]


def test_pending_ticket_sla_is_paused(api: ItsmApi):
    t = api.get("/api/v2/tickets/INC-007", "desk01").json()
    assert t["pending_reason"] == "awaiting_vendor" and t["pending_reason_label"] == "외부 업체 대기"
    assert t["sla"]["paused"] is True and t["sla"]["resolution_due_at"] is None


def test_merged_and_resolved_details(api: ItsmApi):
    merged = api.get("/api/v2/tickets/INC-030", "desk01").json()
    assert merged["status"] == "closed" and merged["merged_into"] == "INC-001"
    assert merged["resolution"]["code"] == "duplicate"
    resolved = api.get("/api/v2/tickets/INC-008", "desk01").json()
    assert resolved["resolution"] == {
        "code": "fixed",
        "code_label": "근본 수정",
        "note": "결과 파일 복제 지연 시 상태를 completed 로 먼저 바꾸던 순서를 수정함.",
        "resolved_at": "2026-10-23T05:00:00Z",
    }


def test_history_requires_audit_permission(api: ItsmApi):
    assert api.get("/api/v2/tickets/INC-001/history", "desk01").status_code == 403
    history = api.get("/api/v2/tickets/INC-001/history", "api01").json()["items"]
    assert history[0]["action"] == "ticket.created" and history[0]["client_id"] == "seed"


# ---------- 카탈로그·디렉터리 ----------


def test_catalog(api: ItsmApi):
    services = api.get("/api/v2/catalog/services").json()
    assert "assistants-api" not in {s["id"] for s in services}
    codex = next(s for s in services if s["id"] == "codex")
    login = next(c for c in codex["components"] if c["name"] == "로그인")
    assert login["owner_team"]["slug"] == "identity"
    assert next(c for c in codex["components"] if c["name"] == "CLI")["owner_team"]["slug"] == "codex"
    deprecated = api.get("/api/v2/catalog/services/assistants-api").json()
    assert deprecated["status"] == "deprecated" and deprecated["replacement_service_id"] == "openai-api"
    assert "gpt-5.5" not in {m["id"] for m in api.get("/api/v2/catalog/models").json()}
    assert api.get("/api/v2/catalog/services/nope").json()["code"] == "service_not_found"


def test_directory(api: ItsmApi):
    users = api.get("/api/v2/users").json()["items"]
    assert len(users) == len(seed_data.USERS) - 1  # 비활성 사용자 제외
    assert [u["username"] for u in api.get("/api/v2/users", params={"q": "한도윤"}).json()["items"]] == ["idops01"]
    assert {u["username"] for u in api.get("/api/v2/users", params={"team_id": 3}).json()["items"]} == {"api01", "api02", "lead01"}
    everyone = {u["username"]: u for u in api.get("/api/v2/users", params={"include_inactive": True}).json()["items"]}
    assert everyone["desk03"]["active"] is False
    page = api.get("/api/v2/users", params={"limit": 5}).json()
    assert len(page["items"]) == 5 and page["page"]["has_more"]
    teams = {t["slug"]: t["member_count"] for t in api.get("/api/v2/teams").json()}
    assert teams["service-desk"] == 4  # desk01, desk02, admin01, lead03 (desk03 비활성 제외)
