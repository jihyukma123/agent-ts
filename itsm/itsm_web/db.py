"""ITSM 웹 SQLite 스키마와 초기화.

ITSM 웹은 서버 측 세션만 저장합니다. 사용자·티켓 등 업무 데이터는 ITSM API 가 소유합니다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = """
-- 서버 측 세션. 쿠키에는 임의의 세션 ID만 담기고, 현재 사용자는 이 테이블로 결정합니다.
-- 사용자 정보는 세션을 만들 때 ITSM API(/api/v2/me)에서 받아 저장합니다.
-- itsm_token 은 이 사용자를 대신해 ITSM API 를 호출하는 위임 토큰입니다(만료 시 다시 교환).
CREATE TABLE IF NOT EXISTS sessions (
    id                    TEXT    PRIMARY KEY,
    user_id               INTEGER NOT NULL,
    username              TEXT    NOT NULL,
    display_name          TEXT    NOT NULL,
    role                  TEXT    NOT NULL,
    role_label            TEXT    NOT NULL,
    team                  TEXT,
    permissions           TEXT    NOT NULL,
    itsm_token            TEXT    NOT NULL,
    itsm_token_expires_at TEXT    NOT NULL,
    created_at            TEXT    NOT NULL
);
"""

TABLES = ["sessions"]


def connect(db_path: Path) -> sqlite3.Connection:
    # 요청마다 새 연결을 쓰지만 FastAPI 가 의존성과 엔드포인트를 다른 스레드에서 실행할 수 있어
    # 스레드 검사를 끕니다. 하나의 연결을 동시에 공유하지는 않습니다.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(db_path)) as conn:
        yield conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn, conn:
        conn.executescript(SCHEMA)


def reset_db(db_path: Path) -> None:
    """ITSM 웹 세션을 모두 지웁니다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn, conn:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    init_db(db_path)
