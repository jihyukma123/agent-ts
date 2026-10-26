"""서비스·컴포넌트·모델 카탈로그."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from itsm_api.auth import Principal, get_db, require
from itsm_api.errors import ApiError
from itsm_api.schemas import ComponentOut, ModelOut, ServiceOut, TeamRef

router = APIRouter(prefix="/api/v2/catalog", tags=["catalog"])


def _teams(conn: sqlite3.Connection) -> dict[int, TeamRef]:
    return {r["id"]: TeamRef(**dict(r)) for r in conn.execute("SELECT id, slug, name FROM teams")}


def _services(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[ServiceOut]:
    teams = _teams(conn)
    components: dict[str, list[sqlite3.Row]] = {}
    for c in conn.execute("SELECT * FROM components ORDER BY id"):
        components.setdefault(c["service_id"], []).append(c)
    result = []
    for s in conn.execute(f"SELECT * FROM services {where} ORDER BY status, name", params):
        owner = teams[s["owner_team_id"]]
        # 컴포넌트에 담당 팀이 따로 없으면 서비스 담당 팀이 맡습니다.
        comps = [
            ComponentOut(id=c["id"], name=c["name"], owner_team=teams[c["owner_team_id"]] if c["owner_team_id"] else owner)
            for c in components.get(s["id"], [])
        ]
        result.append(
            ServiceOut(
                id=s["id"],
                name=s["name"],
                description=s["description"],
                status=s["status"],
                owner_team=owner,
                replacement_service_id=s["replacement_service_id"],
                components=comps,
            )
        )
    return result


@router.get("/services", response_model=list[ServiceOut])
def list_services(
    include_deprecated: bool = False,
    _p: Principal = Depends(require("catalog:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[ServiceOut]:
    return _services(conn, "" if include_deprecated else "WHERE status = 'active'")


@router.get("/services/{service_id}", response_model=ServiceOut)
def get_service(service_id: str, _p: Principal = Depends(require("catalog:read")), conn: sqlite3.Connection = Depends(get_db)) -> ServiceOut:
    found = _services(conn, "WHERE id = ?", (service_id,))
    if not found:
        raise ApiError(404, "service_not_found", f"서비스 '{service_id}' 이(가) 카탈로그에 없습니다.")
    return found[0]


@router.get("/models", response_model=list[ModelOut])
def list_models(
    include_retired: bool = False,
    _p: Principal = Depends(require("catalog:read")),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[ModelOut]:
    where = "" if include_retired else "WHERE status = 'active'"
    return [ModelOut(**dict(r)) for r in conn.execute(f"SELECT id, name, status FROM models {where} ORDER BY id")]
