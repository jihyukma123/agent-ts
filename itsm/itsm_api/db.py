"""ITSM API SQLite 스키마, 초기화, 고정 데이터 시드."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from itsm_api import seed_data
from itsm_api.codes import CLASSIFICATION_LABELS, STATUS_LABELS
from itsm_api.sla import compute_priority


def _check(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS permissions (
    code        TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roles (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id         INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_code TEXT    NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_code)
);

CREATE TABLE IF NOT EXISTS teams (
    id   INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    username     TEXT    NOT NULL UNIQUE,
    display_name TEXT    NOT NULL,
    email        TEXT    NOT NULL UNIQUE,
    role_id      INTEGER NOT NULL REFERENCES roles(id),
    team_id      INTEGER REFERENCES teams(id),
    active       INTEGER NOT NULL DEFAULT 1
);

-- 연동 클라이언트(ITSM 웹, 연동 시스템 등). can_delegate=1 이면 사용자 위임 토큰을 발급받을 수 있습니다.
CREATE TABLE IF NOT EXISTS api_clients (
    client_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    secret_hash  TEXT NOT NULL,
    can_delegate INTEGER NOT NULL DEFAULT 0
);

-- 토큰 원문은 저장하지 않고 SHA-256 해시만 저장합니다. user_id 가 NULL 이면 서비스 토큰입니다.
CREATE TABLE IF NOT EXISTS access_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL REFERENCES api_clients(client_id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    issued_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id                     TEXT PRIMARY KEY,
    name                   TEXT NOT NULL UNIQUE,
    description            TEXT NOT NULL,
    owner_team_id          INTEGER NOT NULL REFERENCES teams(id),
    status                 TEXT NOT NULL CHECK (status IN ('active', 'deprecated')),
    replacement_service_id TEXT REFERENCES services(id)
);

-- owner_team_id 가 있으면 서비스 담당 팀 대신 이 팀으로 자동 배정합니다.
CREATE TABLE IF NOT EXISTS components (
    id            INTEGER PRIMARY KEY,
    service_id    TEXT    NOT NULL REFERENCES services(id),
    name          TEXT    NOT NULL,
    owner_team_id INTEGER REFERENCES teams(id),
    UNIQUE (service_id, name)
);

CREATE TABLE IF NOT EXISTS models (
    id     TEXT PRIMARY KEY,
    name   TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'retired'))
);

CREATE TABLE IF NOT EXISTS problems (
    id            TEXT    PRIMARY KEY,
    seq           INTEGER NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    description   TEXT    NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('open', 'known_error', 'resolved')),
    root_cause    TEXT,
    workaround    TEXT,
    owner_team_id INTEGER NOT NULL REFERENCES teams(id),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id                  TEXT    PRIMARY KEY,
    seq                 INTEGER NOT NULL UNIQUE,
    title               TEXT    NOT NULL,
    description         TEXT    NOT NULL,
    service_id          TEXT    NOT NULL REFERENCES services(id),
    component_id        INTEGER REFERENCES components(id),
    model               TEXT    REFERENCES models(id),
    status              TEXT    NOT NULL CHECK (status IN ({_check(STATUS_LABELS)})),
    impact_level        TEXT    NOT NULL,
    urgency             TEXT    NOT NULL,
    priority            TEXT    NOT NULL,
    impact_note         TEXT    NOT NULL,
    classification      TEXT    NOT NULL DEFAULT 'standard' CHECK (classification IN ({_check(CLASSIFICATION_LABELS)})),
    is_major            INTEGER NOT NULL DEFAULT 0,
    requester_id        INTEGER NOT NULL REFERENCES users(id),
    assignment_group_id INTEGER REFERENCES teams(id),
    assignee_id         INTEGER REFERENCES users(id),
    problem_id          TEXT    REFERENCES problems(id),
    merged_into         TEXT    REFERENCES tickets(id),
    pending_reason      TEXT,
    resolution_code     TEXT,
    resolution_note     TEXT,
    occurred_at         TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    responded_at        TEXT,
    resolved_at         TEXT,
    closed_at           TEXT,
    version             INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets (created_at DESC, seq DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets (status);

CREATE TABLE IF NOT EXISTS sla_pauses (
    id         INTEGER PRIMARY KEY,
    ticket_id  TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);

CREATE TABLE IF NOT EXISTS worklogs (
    id         INTEGER PRIMARY KEY,
    ticket_id  TEXT    NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL REFERENCES users(id),
    visibility TEXT    NOT NULL CHECK (visibility IN ('public', 'internal')),
    kind       TEXT    NOT NULL CHECK (kind IN ('comment', 'status_change', 'system')),
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_worklogs_ticket ON worklogs (ticket_id, id);

-- 모든 티켓 변경은 TicketService 를 거치며 같은 트랜잭션에서 감사 이벤트와 알림 outbox 를 기록합니다.
CREATE TABLE IF NOT EXISTS audit_events (
    id         INTEGER PRIMARY KEY,
    ticket_id  TEXT    NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    actor_id   INTEGER REFERENCES users(id),
    client_id  TEXT,
    action     TEXT    NOT NULL,
    changes    TEXT    NOT NULL,
    request_id TEXT,
    at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ticket ON audit_events (ticket_id, id);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id         INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    ticket_id  TEXT NOT NULL,
    recipients TEXT NOT NULL,
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    client_id    TEXT    NOT NULL,
    user_id      INTEGER NOT NULL,
    key          TEXT    NOT NULL,
    request_hash TEXT    NOT NULL,
    ticket_id    TEXT    NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL,
    PRIMARY KEY (client_id, user_id, key)
);

CREATE TABLE IF NOT EXISTS export_jobs (
    id           TEXT    PRIMARY KEY,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    client_id    TEXT    NOT NULL,
    format       TEXT    NOT NULL CHECK (format IN ('csv', 'jsonl')),
    filters      TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    progress     INTEGER NOT NULL DEFAULT 0,
    row_count    INTEGER,
    result       TEXT,
    error        TEXT,
    created_at   TEXT    NOT NULL,
    finished_at  TEXT
);
"""

# 삭제 순서는 외래 키 의존성의 역순입니다.
TABLES = [
    "export_jobs",
    "idempotency_keys",
    "notification_outbox",
    "audit_events",
    "worklogs",
    "sla_pauses",
    "tickets",
    "problems",
    "models",
    "components",
    "services",
    "access_tokens",
    "api_clients",
    "users",
    "teams",
    "role_permissions",
    "roles",
    "permissions",
]


def connect(db_path: Path) -> sqlite3.Connection:
    # 요청마다 새 연결을 쓰지만 FastAPI 가 의존성과 엔드포인트를 다른 스레드에서 실행할 수 있어
    # 스레드 검사를 끕니다. 하나의 연결을 동시에 공유하지는 않습니다.
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(db_path)) as conn:
        yield conn


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _seed(conn: sqlite3.Connection) -> None:
    conn.executemany("INSERT INTO permissions (code, description) VALUES (?, ?)", seed_data.PERMISSIONS)
    for role_id, name, label, description, permission_codes in seed_data.ROLES:
        conn.execute(
            "INSERT INTO roles (id, name, label, description) VALUES (?, ?, ?, ?)", (role_id, name, label, description)
        )
        conn.executemany(
            "INSERT INTO role_permissions (role_id, permission_code) VALUES (?, ?)",
            [(role_id, code) for code in permission_codes],
        )
    conn.executemany("INSERT INTO teams (id, slug, name) VALUES (?, ?, ?)", seed_data.TEAMS)
    conn.executemany(
        "INSERT INTO users (id, username, display_name, email, role_id, team_id, active) VALUES (?, ?, ?, ?, ?, ?, ?)",
        seed_data.USERS,
    )
    conn.executemany(
        "INSERT INTO api_clients (client_id, name, secret_hash, can_delegate) VALUES (?, ?, ?, ?)",
        [(cid, name, hash_secret(secret), delegate) for cid, name, secret, delegate in seed_data.API_CLIENTS],
    )
    # 대체 서비스를 참조하므로 대체 대상이 먼저 들어가도록 폐기 서비스를 나중에 넣습니다.
    for service in sorted(seed_data.SERVICES, key=lambda s: s["status"] == "deprecated"):
        conn.execute(
            """
            INSERT INTO services (id, name, description, owner_team_id, status, replacement_service_id)
            VALUES (:id, :name, :description, :owner_team_id, :status, :replacement_service_id)
            """,
            service,
        )
    conn.executemany(
        "INSERT INTO components (id, service_id, name, owner_team_id) VALUES (?, ?, ?, ?)", seed_data.COMPONENTS
    )
    conn.executemany("INSERT INTO models (id, name, status) VALUES (?, ?, ?)", seed_data.MODELS)
    conn.executemany(
        """
        INSERT INTO problems (id, seq, title, description, status, root_cause, workaround, owner_team_id, created_at, updated_at)
        VALUES (:id, :seq, :title, :description, :status, :root_cause, :workaround, :owner_team_id, :created_at, :updated_at)
        """,
        seed_data.PROBLEMS,
    )
    for ticket in seed_data.TICKETS:
        row = {**ticket, "priority": compute_priority(ticket["impact_level"], ticket["urgency"])}
        conn.execute(
            """
            INSERT INTO tickets (id, seq, title, description, service_id, component_id, model, status, impact_level,
                                 urgency, priority, impact_note, classification, is_major, requester_id,
                                 assignment_group_id, assignee_id, problem_id, merged_into, pending_reason,
                                 resolution_code, resolution_note, occurred_at, created_at, updated_at,
                                 responded_at, resolved_at, closed_at, version)
            VALUES (:id, :seq, :title, :description, :service_id, :component_id, :model, :status, :impact_level,
                    :urgency, :priority, :impact_note, :classification, :is_major, :requester_id,
                    :assignment_group_id, :assignee_id, :problem_id, :merged_into, :pending_reason,
                    :resolution_code, :resolution_note, :occurred_at, :created_at, :updated_at,
                    :responded_at, :resolved_at, :closed_at, 1)
            """,
            row,
        )
        conn.execute(
            """
            INSERT INTO audit_events (ticket_id, actor_id, client_id, action, changes, request_id, at)
            VALUES (?, ?, 'seed', 'ticket.created', ?, NULL, ?)
            """,
            (ticket["id"], ticket["requester_id"], json.dumps({"status": [None, "received"]}), ticket["created_at"]),
        )
        if ticket.get("pending_since"):
            conn.execute(
                "INSERT INTO sla_pauses (ticket_id, started_at, ended_at) VALUES (?, ?, NULL)",
                (ticket["id"], ticket["pending_since"]),
            )
    conn.executemany(
        """
        INSERT INTO worklogs (ticket_id, author_id, visibility, kind, body, created_at)
        VALUES (:ticket_id, :author_id, :visibility, :kind, :body, :created_at)
        """,
        seed_data.WORKLOGS,
    )


def init_db(db_path: Path) -> None:
    """스키마를 만들고, 비어 있는 DB에만 초기 데이터를 넣습니다. 기존 데이터는 유지합니다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn, conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            _seed(conn)


def reset_db(db_path: Path) -> None:
    """모든 ITSM 데이터를 지우고 고정 초기 데이터로 되돌립니다. 발급된 토큰도 모두 무효화됩니다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn, conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    init_db(db_path)
