"""티켓 데이터 내보내기 (비동기 작업).

POST 로 작업을 만들면 202 와 작업 ID 를 돌려주고, 백그라운드에서 처리합니다.
클라이언트는 GET /api/v2/exports/{id} 로 상태를 확인하고(진행 중이면 Retry-After 헤더 제공),
succeeded 가 되면 download_url 에서 결과를 내려받습니다.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response, status

from itsm_api.auth import Principal, get_db, require_user, user_permissions
from itsm_api.db import open_db
from itsm_api.errors import ApiError
from itsm_api.schemas import ExportIn, ExportJobOut
from itsm_api.ticket_store import SELECT_SQL, TicketFilters, filter_clause
from itsm_api.timeutil import Clock, parse_iso

logger = logging.getLogger("itsm_api.exports")

router = APIRouter(prefix="/api/v2/exports", tags=["exports"])

CSV_COLUMNS = [
    "id", "title", "status", "priority", "service_id", "component_name", "model", "group_name",
    "assignee_username", "requester_username", "created_at", "updated_at", "resolved_at",
]  # fmt: skip


class ExportRunner:
    def __init__(self, db_path: Path, clock: Clock, step_seconds: float):
        self.db_path = db_path
        self.clock = clock
        self.step_seconds = step_seconds

    def submit(self, job_id: str) -> None:
        threading.Thread(target=self._run, args=(job_id,), name=f"export-{job_id}", daemon=True).start()

    def _set(self, conn: sqlite3.Connection, job_id: str, **values) -> None:
        assignments = ", ".join(f"{k} = ?" for k in values)
        with conn:
            conn.execute(f"UPDATE export_jobs SET {assignments} WHERE id = ?", [*values.values(), job_id])

    def _run(self, job_id: str) -> None:
        with open_db(self.db_path) as conn:
            try:
                time.sleep(self.step_seconds)
                self._set(conn, job_id, status="running", progress=10)
                job = conn.execute("SELECT * FROM export_jobs WHERE id = ?", (job_id,)).fetchone()
                principal = _principal(conn, job["client_id"], job["requested_by"])
                filters = json.loads(job["filters"])
                ticket_filters = TicketFilters(
                    statuses=tuple(filters.get("status") or ()),
                    priorities=tuple(filters.get("priority") or ()),
                    service_id=filters.get("service_id"),
                    created_from=parse_iso(filters["created_from"]) if filters.get("created_from") else None,
                    created_to=parse_iso(filters["created_to"]) if filters.get("created_to") else None,
                )
                where, params = filter_clause(ticket_filters, principal)
                rows = conn.execute(SELECT_SQL + f" WHERE {' AND '.join(where)} ORDER BY t.seq", params).fetchall()
                time.sleep(self.step_seconds)
                self._set(conn, job_id, progress=60)
                result = _render(rows, job["format"])
                time.sleep(self.step_seconds)
                self._set(conn, job_id, status="succeeded", progress=100, row_count=len(rows), result=result,
                          finished_at=self.clock.now_iso())  # fmt: skip
            except Exception:
                logger.exception("export 작업 실패: %s", job_id)
                self._set(conn, job_id, status="failed", error="내보내기 처리 중 오류가 발생했습니다.", finished_at=self.clock.now_iso())


def _principal(conn: sqlite3.Connection, client_id: str, user_id: int) -> Principal:
    user = conn.execute(
        "SELECT u.*, r.name AS role FROM users u JOIN roles r ON r.id = u.role_id WHERE u.id = ?", (user_id,)
    ).fetchone()
    return Principal(client_id, user["id"], user["username"], user["role"], user["team_id"], user_permissions(conn, user["role_id"]))


def _render(rows: list[sqlite3.Row], fmt: str) -> str:
    records = [
        {
            "id": r["id"], "title": r["title"], "status": r["status"], "priority": r["priority"],
            "service_id": r["service_id"], "component_name": r["component_name"], "model": r["model"],
            "group_name": r["group_name"], "assignee_username": r["assignee_username"],
            "requester_username": r["requester_username"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "resolved_at": r["resolved_at"],
        }  # fmt: skip
        for r in rows
    ]
    if fmt == "jsonl":
        return "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(records)
    return buf.getvalue()


def _job_out(row: sqlite3.Row) -> ExportJobOut:
    return ExportJobOut(
        id=row["id"],
        status=row["status"],
        progress=row["progress"],
        format=row["format"],
        row_count=row["row_count"],
        download_url=f"/api/v2/exports/{row['id']}/download" if row["status"] == "succeeded" else None,
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def _own_job(conn: sqlite3.Connection, principal: Principal, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM export_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None or row["requested_by"] != principal.user_id:
        raise ApiError(404, "export_not_found", f"내보내기 작업 {job_id} 을(를) 찾을 수 없습니다.")
    return row


@router.post("", response_model=ExportJobOut, status_code=status.HTTP_202_ACCEPTED)
def create_export(
    body: ExportIn,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user("export:create")),
    conn: sqlite3.Connection = Depends(get_db),
) -> ExportJobOut:
    job_id = f"EXP-{uuid.uuid4().hex[:12]}"
    with conn:
        conn.execute(
            "INSERT INTO export_jobs (id, requested_by, client_id, format, filters, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (job_id, principal.user_id, principal.client_id, body.format,
             body.filters.model_dump_json(exclude_none=True), request.app.state.clock.now_iso()),
        )  # fmt: skip
    request.app.state.export_runner.submit(job_id)
    response.headers["Location"] = f"/api/v2/exports/{job_id}"
    response.headers["Retry-After"] = "2"
    return _job_out(_own_job(conn, principal, job_id))


@router.get("/{job_id}", response_model=ExportJobOut)
def get_export(
    job_id: str,
    response: Response,
    principal: Principal = Depends(require_user("export:create")),
    conn: sqlite3.Connection = Depends(get_db),
) -> ExportJobOut:
    row = _own_job(conn, principal, job_id)
    if row["status"] in ("queued", "running"):
        response.headers["Retry-After"] = "2"
    return _job_out(row)


@router.get("/{job_id}/download")
def download_export(
    job_id: str, principal: Principal = Depends(require_user("export:create")), conn: sqlite3.Connection = Depends(get_db)
) -> Response:
    row = _own_job(conn, principal, job_id)
    if row["status"] != "succeeded":
        raise ApiError(409, "export_not_ready", f"내보내기 작업이 아직 완료되지 않았습니다(상태: {row['status']}).",
                       export_status=row["status"], headers={"Retry-After": "2"})  # fmt: skip
    media_type = "text/csv" if row["format"] == "csv" else "application/x-ndjson"
    return Response(row["result"], media_type=f"{media_type}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{job_id}.{row["format"]}"'})  # fmt: skip
