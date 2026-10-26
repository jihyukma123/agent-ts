"""ITSM API worklog·병합·문제(Problem)·내보내기·레거시 v1."""

from __future__ import annotations

import csv
import io
import time

from tests.conftest import ItsmApi

# ---------- worklog ----------


def test_add_worklogs(api: ItsmApi):
    public = api.post("/api/v2/tickets/INC-005/worklogs", "desk01", json={"body": "확인 중입니다."})
    assert public.status_code == 201 and public.json()["visibility"] == "public"
    internal = api.post("/api/v2/tickets/INC-005/worklogs", "desk01", json={"body": "CDN 의심", "visibility": "internal"})
    assert internal.status_code == 201
    # 직원의 첫 공개 댓글은 응답 시각으로 기록됩니다.
    ticket = api.get("/api/v2/tickets/INC-005", "member01").json()
    assert ticket["sla"]["responded_at"] == "2026-10-26T01:00:00Z" and ticket["version"] == 3
    assert [w["body"] for w in api.get("/api/v2/tickets/INC-005/worklogs", "member01").json()["items"]] == ["확인 중입니다."]


def test_worklog_permissions(api: ItsmApi):
    internal = api.post("/api/v2/tickets/INC-005/worklogs", "member01", json={"body": "x", "visibility": "internal"})
    assert internal.status_code == 403
    # 요청자는 주요 장애로 공지된 다른 사람의 티켓을 볼 수는 있지만 댓글은 남길 수 없습니다.
    other = api.post("/api/v2/tickets/INC-002/worklogs", "member01", json={"body": "저도요"})
    assert other.status_code == 403 and other.json()["code"] == "comment_not_allowed"
    closed = api.post("/api/v2/tickets/INC-004/worklogs", "member01", json={"body": "x"})
    assert closed.status_code == 409 and closed.json()["code"] == "ticket_closed"


def test_worklog_pagination(api: ItsmApi):
    for i in range(25):
        api.post("/api/v2/tickets/INC-012/worklogs", "codex01", json={"body": f"메모 {i}"})
    first = api.get("/api/v2/tickets/INC-012/worklogs", "codex01").json()
    assert len(first["items"]) == 20 and first["page"]["has_more"]
    rest = api.get("/api/v2/tickets/INC-012/worklogs", "codex01", params={"cursor": first["page"]["next_cursor"]}).json()
    assert [w["body"] for w in rest["items"]] == [f"메모 {i}" for i in range(20, 25)] and not rest["page"]["has_more"]


# ---------- 병합 ----------


def test_merge_duplicate(api: ItsmApi):
    dup = api.post("/api/v2/tickets", "member03", json={
        "title": "Codex 로그인 안 됨", "description": "로그인 후 튕김", "service_id": "codex", "component_id": 1,
        "impact_level": "minor", "urgency": "high", "impact_note": "본인", "occurred_at": "2026-10-26T09:00:00+09:00",
    }).json()  # fmt: skip
    res = api.post(f"/api/v2/tickets/{dup['id']}/merge", "lead02", json={"target_id": "INC-001"}, headers={"If-Match": '"1"'})
    assert res.status_code == 200, res.text
    merged = res.json()
    assert merged["status"] == "closed" and merged["merged_into"] == "INC-001" and merged["resolution"]["code"] == "duplicate"
    target = api.get("/api/v2/tickets/INC-001", "lead02").json()
    assert target["duplicates"] == ["INC-030", dup["id"]] and target["version"] == 2


def test_merge_rules(api: ItsmApi):
    assert api.post("/api/v2/tickets/INC-005/merge", "desk01", json={"target_id": "INC-001"},
                    headers={"If-Match": '"1"'}).status_code == 403  # fmt: skip
    mismatch = api.post("/api/v2/tickets/INC-005/merge", "lead01", json={"target_id": "INC-001"}, headers={"If-Match": '"1"'})
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "merge_service_mismatch"
    self_merge = api.post("/api/v2/tickets/INC-005/merge", "lead01", json={"target_id": "INC-005"}, headers={"If-Match": '"1"'})
    assert self_merge.json()["code"] == "merge_into_self"
    closed_target = api.post("/api/v2/tickets/INC-012/merge", "lead01", json={"target_id": "INC-004"}, headers={"If-Match": '"1"'})
    assert closed_target.status_code == 409 and closed_target.json()["code"] == "merge_target_closed"
    hidden_target = api.post("/api/v2/tickets/INC-032/merge", "lead01", json={"target_id": "INC-027"}, headers={"If-Match": '"1"'})
    assert hidden_target.status_code == 404


# ---------- 문제(Problem) ----------


def test_problems(api: ItsmApi):
    assert api.get("/api/v2/problems", "member01").status_code == 403
    prb = api.get("/api/v2/problems/prb-002", "desk01").json()
    assert prb["status"] == "known_error" and prb["incident_ids"] == ["INC-002", "INC-003"]
    listed = api.get("/api/v2/problems", "desk01", params={"status": "open"}).json()
    assert [p["id"] for p in listed] == ["PRB-001"]

    created = api.post("/api/v2/problems", "api01", json={"title": "o5 긴 응답 절단", "description": "조사", "owner_team_id": 3})
    assert created.status_code == 201 and created.json()["id"] == "PRB-004"
    linked = api.put("/api/v2/tickets/INC-022/problem", "api01", json={"problem_id": "PRB-004"}, headers={"If-Match": '"1"'})
    assert linked.json()["problem"]["id"] == "PRB-004"
    assert api.get("/api/v2/problems/PRB-004", "api01").json()["incident_ids"] == ["INC-022"]
    unknown = api.put("/api/v2/tickets/INC-022/problem", "api01", json={"problem_id": "PRB-999"}, headers={"If-Match": '"2"'})
    assert unknown.json()["code"] == "unknown_problem"
    assert api.put("/api/v2/tickets/INC-022/problem", "desk01", json={"problem_id": None},
                   headers={"If-Match": '"2"'}).status_code == 403  # fmt: skip


# ---------- 내보내기 ----------


def _wait(api: ItsmApi, job_id: str, username: str) -> dict:
    for _ in range(100):
        job = api.get(f"/api/v2/exports/{job_id}", username).json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("export 작업이 끝나지 않음")


def test_export_job(api: ItsmApi):
    res = api.post("/api/v2/exports", "lead01", json={"format": "csv", "filters": {"status": ["pending"]}})
    assert res.status_code == 202
    job = res.json()
    assert job["status"] == "queued" and res.headers["Location"] == f"/api/v2/exports/{job['id']}"
    assert res.headers["Retry-After"] == "2"

    done = _wait(api, job["id"], "lead01")
    assert done["status"] == "succeeded" and done["progress"] == 100 and done["row_count"] == 2
    download = api.get(done["download_url"], "lead01")
    assert download.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(download.text)))
    assert [r["id"] for r in rows] == ["INC-007", "INC-022"]


def test_export_respects_visibility_and_ownership(api: ItsmApi):
    assert api.post("/api/v2/exports", "desk01", json={}).status_code == 403
    job = api.post("/api/v2/exports", "lead01", json={"format": "jsonl"}).json()
    assert api.get(f"/api/v2/exports/{job['id']}", "admin01").status_code == 404  # 다른 사람의 작업
    done = _wait(api, job["id"], "lead01")
    assert done["row_count"] == 33  # 팀장(API 플랫폼팀)은 보안 제한 티켓 3건을 볼 수 없음


def test_export_download_before_ready(api: ItsmApi):
    api.client.app.state.export_runner.step_seconds = 0.5
    job = api.post("/api/v2/exports", "lead01", json={}).json()
    res = api.get(f"/api/v2/exports/{job['id']}/download", "lead01")
    assert res.status_code == 409 and res.json()["code"] == "export_not_ready" and res.headers["Retry-After"] == "2"


# ---------- 레거시 v1 ----------


def test_legacy_list_and_detail(api: ItsmApi):
    res = api.get("/api/v1/tickets", "member01", params={"keyword": "Codex", "state": "IN_PROGRESS"})
    assert res.status_code == 200
    assert res.headers["Deprecation"] == "true" and "successor-version" in res.headers["Link"]
    body = res.json()
    assert body["total"] == 2 and body["page"] == 1
    assert body["list"][1] == {
        "ticket_no": "INC-001", "subject": "Codex 로그인 실패", "state": "IN_PROGRESS",
        "svc": "Codex", "severity": 2, "created": "2026-10-20 09:55:00",
    }  # fmt: skip
    detail = api.get("/api/v1/tickets/INC-001", "member01").json()
    assert detail["owner"] == "idops01" and detail["module"] == "로그인" and detail["reporter"] == "member01"


def test_legacy_pagination_and_errors(api: ItsmApi):
    page2 = api.get("/api/v1/tickets", "admin01", params={"size": 10, "page": 2}).json()
    assert page2["total"] == 36 and len(page2["list"]) == 10
    missing = api.get("/api/v1/tickets/INC-026", "member01")
    assert missing.status_code == 404 and missing.headers["content-type"].startswith("text/plain")
    assert missing.text == "ERROR NOT_FOUND: ticket INC-026 not found"
    bad = api.get("/api/v1/tickets", "member01", params={"state": "open"})
    assert bad.status_code == 400 and bad.text.startswith("ERROR BAD_REQUEST: state")
    assert api.client.get("/api/v1/tickets").text == "ERROR MISSING_TOKEN: Authorization: Bearer <token> 헤더가 필요합니다."


def test_legacy_create(api: ItsmApi):
    res = api.post("/api/v1/tickets", "member01", json={"subject": "이미지 생성 안 됨", "desc": "오류", "svc": "이미지 생성", "severity": 2})
    assert res.status_code == 200 and res.json() == {"result": "OK", "ticket_no": "INC-037"}
    t = api.get("/api/v2/tickets/INC-037", "member01").json()
    assert t["priority"] == "P2" and t["service"]["id"] == "image-gen" and t["impact_note"] == "(v1 연동으로 등록됨)"
    bad = api.post("/api/v1/tickets", "member01", json={"subject": "x", "desc": "y", "svc": "없는서비스", "severity": 3})
    assert bad.status_code == 400 and bad.text == "ERROR BAD_SVC: unknown svc '없는서비스'"
