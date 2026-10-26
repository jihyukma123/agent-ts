"""에이전트 서비스 SQLite 스키마와 초기화.

에이전트는 서버 측 세션만 저장합니다. 업무 데이터는 ITSM 이 소유하고, 대화 이력은 SDK SQLiteSession 이 별도 파일에 저장합니다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
-- 서버 측 세션. 쿠키에는 임의의 세션 ID만 담기고, 현재 사용자는 이 테이블로 결정합니다.
-- 사용자 정보는 세션을 만들 때 ITSM API(/api/v2/me)에서 받아 저장합니다.
-- itsm_token 은 이 사용자를 대신해 ITSM API 를 호출할 수 있는 위임 토큰입니다(만료 시 다시 교환).
CREATE TABLE IF NOT EXISTS sessions (
    id                    TEXT    PRIMARY KEY,
    user_id               INTEGER NOT NULL,
    username              TEXT    NOT NULL,
    display_name          TEXT    NOT NULL,
    role                  TEXT    NOT NULL,
    role_label            TEXT    NOT NULL,
    team                  TEXT,
    permissions           TEXT    NOT NULL,
    conversation_id       TEXT    NOT NULL UNIQUE,
    itsm_token            TEXT    NOT NULL,
    itsm_token_expires_at TEXT    NOT NULL,
    created_at            TEXT    NOT NULL,
    session_expires_at    TEXT    NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    itsm_credential_generation INTEGER NOT NULL DEFAULT 0
);

-- U1 ticket_tools_state_v1 namespace.
CREATE TABLE IF NOT EXISTS pending_ticket_mutations (
    mutation_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'update')),
    user_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    normalized_payload_hash TEXT NOT NULL,
    ticket_id TEXT,
    base_version INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending','confirmed','cancelled','invalidated','expired','consumed')),
    confirmed_at TEXT,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_ticket_mutations_scope
ON pending_ticket_mutations(user_id, session_id, conversation_id, operation, status);

CREATE TABLE IF NOT EXISTS ticket_cursor_bindings (
    continuation_ref TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    query_serialization_version INTEGER NOT NULL,
    canonical_normalized_query_json TEXT NOT NULL,
    normalized_query_hash TEXT NOT NULL,
    upstream_cursor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_cursor_bindings_scope
ON ticket_cursor_bindings(user_id, session_id, conversation_id);

-- U2 ticket_tools_create_v1 namespace. This additive ledger deliberately has
-- no foreign keys to revocable session/conversation rows.
CREATE TABLE IF NOT EXISTS ticket_create_attempts (
    create_attempt_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    originating_session_id TEXT NOT NULL,
    originating_conversation_id TEXT NOT NULL,
    normalized_payload_hash TEXT NOT NULL,
    canonical_request_bytes BLOB NOT NULL,
    serialization_version INTEGER NOT NULL CHECK (serialization_version = 1),
    request_bytes_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    confirmed_mutation_id TEXT UNIQUE,
    confirmed_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 2),
    status TEXT NOT NULL CHECK (status IN ('prepared','in_flight','retryable_after_reconcile','retryable_no_apply','completed','terminal_failure','ambiguous')),
    claim_id TEXT UNIQUE,
    claim_deadline_at TEXT,
    refresh_reservation_id TEXT UNIQUE,
    refresh_used_at TEXT,
    refresh_state TEXT NOT NULL DEFAULT 'unused' CHECK (refresh_state IN ('unused','reserved','committed','failed')),
    refresh_base_credential_generation INTEGER,
    refresh_committed_credential_generation INTEGER,
    refresh_committed_at TEXT,
    last_outcome TEXT,
    result_ticket_id TEXT,
    created_at TEXT NOT NULL,
    retained_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_create_attempts_scope
ON ticket_create_attempts(user_id, originating_session_id, originating_conversation_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ticket_create_prepared_scope
ON ticket_create_attempts(user_id, originating_session_id, originating_conversation_id)
WHERE status = 'prepared';
"""

TABLES = [
    "pending_update_executions",
    "ticket_tools_update_v1_migrations",
    "ticket_create_attempts",
    "ticket_cursor_bindings",
    "pending_ticket_mutations",
    "sessions",
]

# OpenAI Agents SDK SQLiteSession 이 사용하는 기본 테이블 이름
AGENT_HISTORY_TABLES = ["ticket_message_presentations", "agent_messages", "agent_sessions"]

SESSION_LIFETIME = timedelta(hours=8)
_EXPIRED_SESSION_TIME = "1970-01-01T00:00:00Z"
_SESSION_BASE_COLUMNS = (
    ("id", "TEXT", 0, 1),
    ("user_id", "INTEGER", 1, 0),
    ("username", "TEXT", 1, 0),
    ("display_name", "TEXT", 1, 0),
    ("role", "TEXT", 1, 0),
    ("role_label", "TEXT", 1, 0),
    ("team", "TEXT", 0, 0),
    ("permissions", "TEXT", 1, 0),
    ("conversation_id", "TEXT", 1, 0),
    ("itsm_token", "TEXT", 1, 0),
    ("itsm_token_expires_at", "TEXT", 1, 0),
    ("created_at", "TEXT", 1, 0),
)


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


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _legacy_expiry(created_at: object) -> str:
    if not isinstance(created_at, str) or not created_at.strip():
        return _EXPIRED_SESSION_TIME
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return _EXPIRED_SESSION_TIME
    return _format_utc(parsed + SESSION_LIFETIME)


def _migrate_sessions(conn: sqlite3.Connection) -> None:
    """기존 physical session schema를 재작성하지 않고 expiry 열만 추가한다."""
    columns = conn.execute("PRAGMA table_info(sessions)").fetchall()
    observed_base = tuple((row[1], row[2].upper(), row[3], row[5]) for row in columns[: len(_SESSION_BASE_COLUMNS)])
    if observed_base != _SESSION_BASE_COLUMNS:
        raise RuntimeError("sessions schema does not match the supported physical session contract")
    names = [row[1] for row in columns]
    unexpected = names[len(_SESSION_BASE_COLUMNS) :]
    if unexpected not in (
        [],
        ["session_expires_at"],
        ["session_expires_at", "itsm_credential_generation"],
    ):
        raise RuntimeError("sessions schema contains unsupported columns")
    if "session_expires_at" not in names:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN session_expires_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z'"
        )
        rows = conn.execute("SELECT id, created_at FROM sessions").fetchall()
        conn.executemany(
            "UPDATE sessions SET session_expires_at = ? WHERE id = ?",
            ((_legacy_expiry(row[1]), row[0]) for row in rows),
        )
    if "itsm_credential_generation" not in names:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN itsm_credential_generation INTEGER NOT NULL DEFAULT 0"
        )
    invalid = conn.execute(
        "SELECT id, created_at FROM sessions WHERE session_expires_at IS NULL OR trim(session_expires_at) = ''"
    ).fetchall()
    conn.executemany(
        "UPDATE sessions SET session_expires_at = ? WHERE id = ?",
        ((_legacy_expiry(row[1]), row[0]) for row in invalid),
    )


def _migrate_pending_mutations(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_ticket_mutations)")}
    if "confirmed_at" not in columns:
        conn.execute("ALTER TABLE pending_ticket_mutations ADD COLUMN confirmed_at TEXT")


def migrate_ticket_create_attempts(conn: sqlite3.Connection) -> None:
    """Add the approved optional refresh evidence without rewriting ledger rows.

    Older development databases may still contain retired claim bookkeeping
    columns. They are deliberately ignored: SQLite cannot remove them without a
    table rewrite, and rollback safety forbids rewriting this durable ledger.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ticket_create_attempts)")}
    additions = (
        ("refresh_state", "TEXT NOT NULL DEFAULT 'unused'"),
        ("refresh_base_credential_generation", "INTEGER"),
        ("refresh_committed_credential_generation", "INTEGER"),
        ("refresh_committed_at", "TEXT"),
    )
    for name, declaration in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE ticket_create_attempts ADD COLUMN {name} {declaration}")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn, conn:
        conn.executescript(SCHEMA)
        _migrate_sessions(conn)
        _migrate_pending_mutations(conn)
        migrate_ticket_create_attempts(conn)
        # U3 is additive and deliberately follows the U1 common schema.
        from app.ticketing.update_migration import migrate_ticket_updates

        migrate_ticket_updates(conn)


def reset_db(db_path: Path, agent_history_db_path: Path) -> None:
    """세션과 Agent 대화 이력을 모두 지웁니다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open_db(db_path) as conn, conn:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    init_db(db_path)

    if agent_history_db_path.exists():
        with closing(sqlite3.connect(agent_history_db_path)) as conn, conn:
            for table in AGENT_HISTORY_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
