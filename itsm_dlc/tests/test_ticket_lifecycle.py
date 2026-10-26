from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.agent.service import AgentService
from app.db import open_db
from app.main import create_app
from app.session import SESSION_COOKIE, load_current_user
from app.ticketing.capability import DelegatedCredential, TicketRequestCapability
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import CanonicalTicketQueryV1, PendingTicketMutationV1
from tests.conftest import FakeItsm, FakeModel, login
from tests.test_ticket_read_integration import TicketReadModel


class MutableUtcClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ObservedUtcClock(MutableUtcClock):
    def __init__(self, value: datetime):
        super().__init__(value)
        self.calls = 0
        self.third_call = threading.Event()

    def __call__(self) -> datetime:
        self.calls += 1
        if self.calls >= 3:
            self.third_call.set()
        return self.value

    def reset(self) -> None:
        self.calls = 0
        self.third_call.clear()


def _current(settings, client):
    with open_db(settings.db_path) as conn:
        return load_current_user(conn, client.cookies.get(SESSION_COOKIE))


def _seed_revocable_state(service: AgentService, user) -> str:
    service.pending_repository.save_review(PendingTicketMutationV1(
        mutation_id="pending-generated",
        operation="create",
        user_id=user.id,
        session_id=user.session_id,
        conversation_id=user.conversation_id,
        normalized_payload_hash="a" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))

    async def token():
        return DelegatedCredential("not-persisted-by-cursor")

    capability = TicketRequestCapability(user.id, user.session_id, user.conversation_id, token, token)
    return service.cursor_repository.issue(capability, CanonicalTicketQueryV1(), "upstream-secret")


def _history_counts(settings, conversation_id: str) -> tuple[int, int, int]:
    with sqlite3.connect(settings.agent_history_db_path) as conn:
        presentation_count = conn.execute(
            "SELECT COUNT(*) FROM ticket_message_presentations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        message_count = conn.execute(
            "SELECT COUNT(*) FROM agent_messages WHERE session_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        session_count = conn.execute(
            "SELECT COUNT(*) FROM agent_sessions WHERE session_id = ?",
            (conversation_id,),
        ).fetchone()[0]
    return presentation_count, message_count, session_count


def _seed_u2_attempt(settings, suffix: str) -> tuple:
    body = f'{{"stable":"{suffix}"}}'.encode()
    digest = hashlib.sha256(body).hexdigest()
    with open_db(settings.db_path) as conn, conn:
        conn.execute(
            """INSERT INTO ticket_create_attempts
               (create_attempt_id,user_id,originating_session_id,originating_conversation_id,
                normalized_payload_hash,canonical_request_bytes,serialization_version,
                request_bytes_hash,idempotency_key,attempt_count,status,created_at,retained_until)
               VALUES (?,7,'historical-session','historical-conversation',?,?,1,?,?,0,'terminal_failure',?,?)""",
            (
                f"attempt-{suffix}", digest, body, digest, f"key-{suffix}",
                "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z",
            ),
        )
        return tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT create_attempt_id, idempotency_key, canonical_request_bytes "
                "FROM ticket_create_attempts ORDER BY create_attempt_id"
            )
        )


def _u2_attempts(settings) -> tuple:
    with open_db(settings.db_path) as conn:
        return tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT create_attempt_id, idempotency_key, canonical_request_bytes "
                "FROM ticket_create_attempts ORDER BY create_attempt_id"
            )
        )


def test_current_turn_evidence_normalizes_only_independent_commands():
    values = {
        "  CONFIRM  ": "explicit_confirm",
        "확인": "explicit_confirm",
        " 취소 ": "explicit_cancel",
        "확인하고 제목도 바꿔": "revise_or_ambiguous",
    }
    for raw, expected in values.items():
        evidence = CurrentTurnEvidenceV1.capture(
            user_id=7, session_id="session", conversation_id="conversation", raw_message=raw
        )
        assert evidence.decision == expected
        assert raw not in repr(evidence.raw_message_sha256)


def test_conversation_clear_invalidates_state_then_presentation_and_history(settings):
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=TicketReadModel())
    with TestClient(create_app(settings, agent_service=service, itsm_client=fake_itsm.client())) as client:
        login(client, "admin01")
        assert client.post("/api/agent/chat", json={"message": "티켓 목록"}).status_code == 200
        user = _current(settings, client)
        ref = _seed_revocable_state(service, user)
        ledger_before = _seed_u2_attempt(settings, "clear")
        assert client.delete("/api/agent/messages").status_code == 204
        assert client.get("/api/agent/messages").json() == {"messages": []}

    assert service.pending_repository.get("pending-generated").status == "invalidated"
    with open_db(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?", (ref,)).fetchone()[0] == 0
    assert service.presentation_repository.message_rows(user.conversation_id) == []
    assert _u2_attempts(settings) == ledger_before


def test_logout_cleans_old_authority_before_session_delete(settings):
    service = AgentService(settings, model=TicketReadModel())
    fake_itsm = FakeItsm()
    with TestClient(create_app(settings, agent_service=service, itsm_client=fake_itsm.client())) as client:
        login(client, "admin01")
        user = _current(settings, client)
        _seed_revocable_state(service, user)
        ledger_before = _seed_u2_attempt(settings, "logout")
        original = service.lifecycle_transition
        observed = []

        async def checked_transition(request, expected, terminal_commit):
            with open_db(settings.db_path) as conn:
                observed.append(conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)).fetchone()[0])
            await original(request, expected, terminal_commit)

        service.lifecycle_transition = checked_transition
        assert client.delete("/api/session").status_code == 204

    assert observed == [1]
    with open_db(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)).fetchone()[0] == 0
    assert _u2_attempts(settings) == ledger_before


def test_successful_user_switch_preserves_u2_ledger_bytes_and_key(settings):
    service = AgentService(settings, model=TicketReadModel())
    fake_itsm = FakeItsm()
    with TestClient(create_app(settings, agent_service=service, itsm_client=fake_itsm.client())) as client:
        login(client, "admin01")
        old = _current(settings, client)
        _seed_revocable_state(service, old)
        ledger_before = _seed_u2_attempt(settings, "switch")
        response = client.post("/api/session", json={"user_id": 2})
        assert response.status_code == 200
        current = _current(settings, client)
        assert current.id == 2 and current.session_id != old.session_id
    assert _u2_attempts(settings) == ledger_before


def test_switch_cleanup_failure_keeps_old_identity(settings):
    service = AgentService(settings, model=TicketReadModel())
    fake_itsm = FakeItsm()
    app = create_app(settings, agent_service=service, itsm_client=fake_itsm.client())
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client, "admin01")
        old = _current(settings, client)

        async def fail_cleanup(_request, _expected, _terminal_commit):
            raise RuntimeError("synthetic cleanup failure")

        service.lifecycle_transition = fail_cleanup
        assert client.post("/api/session", json={"user_id": 2}).status_code == 500
        current = _current(settings, client)
        assert current.id == old.id and current.session_id == old.session_id


class InterleavingTicketModel(TicketReadModel):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.max_active = 0
        self.completed_turns = 0

    async def get_response(self, *args, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        try:
            response = await super().get_response(*args, **kwargs)
            if len(self.calls) % 2 == 0:
                self.completed_turns += 1
                response.output[0].content[0].text = f"동시 조회 응답 {self.completed_turns}"
            return response
        finally:
            self.active -= 1


def test_same_conversation_runner_and_presentation_commit_are_serialized(settings):
    model = InterleavingTicketModel()
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=model)
    lock_observations = []
    original_put = service.presentation_repository.put_and_readback

    def checked_put(conversation_id, message, presentation):
        lock_observations.append(service._conversation_locks[conversation_id].locked())
        return original_put(conversation_id, message, presentation)

    service.presentation_repository.put_and_readback = checked_put
    with TestClient(create_app(settings, agent_service=service, itsm_client=fake_itsm.client())) as client:
        login(client, "admin01")

        def send(number):
            return client.post("/api/agent/chat", json={"message": f"동시 티켓 조회 {number}"})

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(send, (1, 2)))
        assert [response.status_code for response in responses] == [200, 200]
        assert model.max_active == 1
        bodies = [response.json() for response in responses]
        assert {body["reply"] for body in bodies} == {"동시 조회 응답 1", "동시 조회 응답 2"}
        assert all(body["presentation"]["kind"] == "ticket_list" for body in bodies)
        history = client.get("/api/agent/messages").json()["messages"]
        assistant_rows = [row for row in history if row["role"] == "assistant"]
        assert {row["text"] for row in assistant_rows} == {"동시 조회 응답 1", "동시 조회 응답 2"}
        assert all(row["presentation"]["kind"] == "ticket_list" for row in assistant_rows)
        assert lock_observations == [True, True]

    raw = settings.agent_history_db_path.read_text(errors="ignore")
    assert "tok-admin01" not in raw and "upstream-secret" not in raw


def test_absolute_session_expiry_exact_boundary_cleans_authority_before_runner_or_itsm(settings):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = MutableUtcClock(created)
    model = TicketReadModel()
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=model)
    with TestClient(
        create_app(settings, agent_service=service, itsm_client=fake_itsm.client(), utc_now=clock)
    ) as client:
        login(client, "admin01")
        user = _current(settings, client)
        ref = _seed_revocable_state(service, user)
        ledger_before = _seed_u2_attempt(settings, "expiry")
        with open_db(settings.db_path) as conn:
            created_at, expires_at = conn.execute(
                "SELECT created_at, session_expires_at FROM sessions WHERE id = ?", (user.session_id,)
            ).fetchone()
        assert (created_at, expires_at) == ("2026-01-01T00:00:00Z", "2026-01-01T08:00:00Z")

        clock.value = created + timedelta(hours=8) - timedelta(microseconds=1)
        assert client.get("/api/session").status_code == 200
        ticket_calls_before = len([p for p in fake_itsm.paths() if p.startswith("/api/v2/tickets")])
        model_calls_before = len(model.calls)

        clock.value = created + timedelta(hours=8)
        response = client.post("/api/agent/chat", json={"message": "티켓 조회"})
        assert response.status_code == 401 and response.json()["detail"]["code"] == "authentication_required"
        assert len(model.calls) == model_calls_before
        assert len([p for p in fake_itsm.paths() if p.startswith("/api/v2/tickets")]) == ticket_calls_before

    with open_db(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_ticket_mutations WHERE session_id = ?", (user.session_id,)
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?", (ref,)).fetchone()[0] == 0
    assert _u2_attempts(settings) == ledger_before


def test_forward_redeploy_of_prior_release_session_fails_closed_and_preserves_u2_ledger(settings):
    service = AgentService(settings, model=TicketReadModel())
    fake_itsm = FakeItsm()
    with TestClient(create_app(settings, agent_service=service, itsm_client=fake_itsm.client())) as client:
        ledger_before = _seed_u2_attempt(settings, "forward-redeploy")
        with open_db(settings.db_path) as conn, conn:
            conn.execute(
                """INSERT INTO sessions
                   (id, user_id, username, display_name, role, role_label, team, permissions,
                    conversation_id, itsm_token, itsm_token_expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "prior-session", 7, "prior", "Prior", "requester", "Requester", None,
                    '["ticket:read"]', "prior-conversation", "prior-secret",
                    "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z",
                ),
            )
        client.cookies.set(SESSION_COOKIE, "prior-session")
        response = client.get("/api/session")
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "authentication_required"
    assert _u2_attempts(settings) == ledger_before


def test_session_expiry_survives_restart_and_after_boundary_fails_closed(settings):
    created = datetime(2026, 2, 1, tzinfo=timezone.utc)
    clock = MutableUtcClock(created)
    first_itsm = FakeItsm()
    first_model = TicketReadModel()
    first_service = AgentService(settings, model=first_model)
    with TestClient(
        create_app(settings, agent_service=first_service, itsm_client=first_itsm.client(), utc_now=clock)
    ) as client:
        login(client, "member01")
        assert client.post("/api/agent/chat", json={"message": "재시작 전 티켓 조회"}).status_code == 200
        user = _current(settings, client)
        cookie = client.cookies.get(SESSION_COOKIE)
        assert all(count > 0 for count in _history_counts(settings, user.conversation_id))

    clock.value = created + timedelta(hours=8, microseconds=1)
    restarted_model = TicketReadModel()
    restarted_itsm = FakeItsm()
    service = AgentService(settings, model=restarted_model)
    with TestClient(
        create_app(settings, agent_service=service, itsm_client=restarted_itsm.client(), utc_now=clock)
    ) as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        response = client.get("/api/agent/messages")
    assert response.status_code == 401 and response.json()["detail"]["code"] == "authentication_required"
    assert restarted_model.calls == []
    assert not any(path.startswith("/api/v2/tickets") for path in restarted_itsm.paths())
    assert _history_counts(settings, user.conversation_id) == (0, 0, 0)


def test_concurrent_expired_cookie_cleanup_and_restart_retry_are_terminal(settings):
    created = datetime(2026, 2, 15, tzinfo=timezone.utc)
    clock = MutableUtcClock(created)
    model = TicketReadModel()
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=model)
    app = create_app(settings, agent_service=service, itsm_client=fake_itsm.client(), utc_now=clock)
    with TestClient(app) as client:
        login(client, "admin01")
        user = _current(settings, client)
        ref = _seed_revocable_state(service, user)
        cookie = client.cookies.get(SESSION_COOKIE)
        clock.value = created + timedelta(hours=8)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _value: client.get("/api/agent/messages"), (1, 2)))

    assert [response.status_code for response in responses] == [401, 401]
    assert [response.json()["detail"]["code"] for response in responses] == [
        "authentication_required",
        "authentication_required",
    ]
    assert model.calls == []
    assert not any(path.startswith("/api/v2/tickets") for path in fake_itsm.paths())
    with open_db(settings.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            """SELECT COUNT(*) FROM pending_ticket_mutations
               WHERE session_id = ? AND status IN ('pending','confirmed')""",
            (user.session_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?", (ref,)
        ).fetchone()[0] == 0

    restarted_model = TicketReadModel()
    restarted_itsm = FakeItsm()
    restarted_service = AgentService(settings, model=restarted_model)
    with TestClient(
        create_app(
            settings,
            agent_service=restarted_service,
            itsm_client=restarted_itsm.client(),
            utc_now=clock,
        )
    ) as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        retried = client.get("/api/agent/messages")

    assert retried.status_code == 401 and retried.json()["detail"]["code"] == "authentication_required"
    assert restarted_model.calls == []
    assert not any(path.startswith("/api/v2/tickets") for path in restarted_itsm.paths())


def test_expiry_cleanup_failure_rolls_back_and_never_reaches_runner_or_itsm(settings):
    created = datetime(2026, 3, 1, tzinfo=timezone.utc)
    clock = MutableUtcClock(created)
    model = TicketReadModel()
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=model)
    with TestClient(
        create_app(settings, agent_service=service, itsm_client=fake_itsm.client(), utc_now=clock),
        raise_server_exceptions=False,
    ) as client:
        login(client, "admin01")
        user = _current(settings, client)
        ref = _seed_revocable_state(service, user)
        with open_db(settings.db_path) as conn, conn:
            conn.execute(
                """CREATE TRIGGER fail_expired_session_delete BEFORE DELETE ON sessions
                   BEGIN SELECT RAISE(ABORT, 'synthetic cleanup failure'); END"""
            )
        model_calls_before = len(model.calls)
        ticket_calls_before = len([p for p in fake_itsm.paths() if p.startswith("/api/v2/tickets")])
        clock.value = created + timedelta(hours=8)
        response = client.post("/api/agent/chat", json={"message": "티켓 조회"})
        assert response.status_code == 503 and response.json()["detail"]["code"] == "session_cleanup_failed"
        assert len(model.calls) == model_calls_before
        assert len([p for p in fake_itsm.paths() if p.startswith("/api/v2/tickets")]) == ticket_calls_before

    with open_db(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT status FROM pending_ticket_mutations WHERE mutation_id = 'pending-generated'").fetchone()[0] == "pending"
        assert conn.execute("SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?", (ref,)).fetchone()[0] == 1


def test_session_expiring_while_waiting_for_conversation_lock_blocks_second_runner(settings):
    created = datetime(2026, 5, 1, tzinfo=timezone.utc)
    clock = ObservedUtcClock(created)
    entered = threading.Event()
    release = threading.Event()

    class HoldingModel(FakeModel):
        async def get_response(self, *args, **kwargs):
            entered.set()
            await asyncio.to_thread(release.wait)
            return await super().get_response(*args, **kwargs)

    model = HoldingModel(reply="첫 응답")
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=model)
    with TestClient(
        create_app(settings, agent_service=service, itsm_client=fake_itsm.client(), utc_now=clock)
    ) as client:
        login(client, "admin01")
        user = _current(settings, client)
        ref = _seed_revocable_state(service, user)
        clock.reset()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.post, "/api/agent/chat", json={"message": "첫 요청"})
            assert entered.wait(timeout=2)
            second = pool.submit(client.post, "/api/agent/chat", json={"message": "대기 요청"})
            assert clock.third_call.wait(timeout=2)
            clock.value = created + timedelta(hours=8)
            release.set()
            first_response = first.result(timeout=5)
            second_response = second.result(timeout=5)

        assert first_response.status_code == 401
        assert second_response.status_code == 401
        assert first_response.json()["detail"]["code"] == "authentication_required"
        assert second_response.json()["detail"]["code"] == "authentication_required"
        assert len(model.calls) == 1
        assert not any(path.startswith("/api/v2/tickets") for path in fake_itsm.paths())

    with open_db(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_ticket_mutations WHERE session_id = ?", (user.session_id,)
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?", (ref,)).fetchone()[0] == 0


def test_session_expiring_inside_runner_before_tool_access_cleans_and_sends_no_ticket_http(settings):
    created = datetime(2026, 6, 1, tzinfo=timezone.utc)
    clock = MutableUtcClock(created)

    class ExpiringToolModel(TicketReadModel):
        async def get_response(self, *args, **kwargs):
            response = await super().get_response(*args, **kwargs)
            if len(self.calls) == 1:
                clock.value = created + timedelta(hours=8)
            return response

    model = ExpiringToolModel()
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=model)
    with TestClient(
        create_app(settings, agent_service=service, itsm_client=fake_itsm.client(), utc_now=clock)
    ) as client:
        login(client, "admin01")
        user = _current(settings, client)
        ref = _seed_revocable_state(service, user)
        response = client.post("/api/agent/chat", json={"message": "만료 직전 조회"})
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "authentication_required"
        assert client.get("/api/session").status_code == 401

    assert not any(path.startswith("/api/v2/tickets") for path in fake_itsm.paths())
    with open_db(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pending_ticket_mutations WHERE session_id = ?", (user.session_id,)
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?", (ref,)).fetchone()[0] == 0


def test_runner_expiry_and_competing_history_request_share_lock_and_publish_nothing(settings, monkeypatch):
    created = datetime(2026, 7, 1, tzinfo=timezone.utc)
    clock = MutableUtcClock(created)
    model = TicketReadModel(reply="폐기되어야 하는 성공 응답")
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=model)
    history_written = threading.Event()
    release_runner = threading.Event()
    expiry_lock_requested = threading.Event()
    correlation_calls = []
    publication_calls = []

    with TestClient(
        create_app(settings, agent_service=service, itsm_client=fake_itsm.client(), utc_now=clock)
    ) as client:
        login(client, "admin01")
        assert client.post("/api/agent/chat", json={"message": "기존 history 생성"}).status_code == 200
        user = _current(settings, client)
        ref = _seed_revocable_state(service, user)
        assert all(count > 0 for count in _history_counts(settings, user.conversation_id))

        original_session = service._session

        class HistoryWriteBarrierSession:
            def __init__(self, inner):
                self.inner = inner
                self.blocked = False

            async def add_items(self, items):
                await self.inner.add_items(items)
                if not self.blocked and any(
                    isinstance(item, dict) and item.get("role") == "assistant" for item in items
                ):
                    self.blocked = True
                    history_written.set()
                    await asyncio.to_thread(release_runner.wait)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        monkeypatch.setattr(
            service,
            "_session",
            lambda conversation_id: HistoryWriteBarrierSession(original_session(conversation_id)),
        )

        original_for_conversation = service.conversation_locks.for_conversation
        provider_calls = 0
        provider_calls_guard = threading.Lock()

        def observed_for_conversation(conversation_id):
            nonlocal provider_calls
            with provider_calls_guard:
                provider_calls += 1
                if provider_calls >= 2:
                    expiry_lock_requested.set()
            return original_for_conversation(conversation_id)

        monkeypatch.setattr(service.conversation_locks, "for_conversation", observed_for_conversation)

        original_latest = service.presentation_repository.latest_assistant
        original_publish = service.presentation_repository.put_and_readback

        def observed_latest(conversation_id):
            correlation_calls.append(conversation_id)
            return original_latest(conversation_id)

        def observed_publish(conversation_id, message, presentation):
            publication_calls.append(conversation_id)
            return original_publish(conversation_id, message, presentation)

        monkeypatch.setattr(service.presentation_repository, "latest_assistant", observed_latest)
        monkeypatch.setattr(service.presentation_repository, "put_and_readback", observed_publish)

        with ThreadPoolExecutor(max_workers=2) as pool:
            chat_future = pool.submit(
                client.post,
                "/api/agent/chat",
                json={"message": "Runner 도중 만료되는 조회"},
            )
            assert history_written.wait(timeout=3)
            clock.value = created + timedelta(hours=8)
            expiry_future = pool.submit(client.get, "/api/agent/messages")
            assert expiry_lock_requested.wait(timeout=3)
            assert service._conversation_locks[user.conversation_id].locked()
            assert not expiry_future.done()

            release_runner.set()
            chat_response = chat_future.result(timeout=5)
            expiry_response = expiry_future.result(timeout=5)

        assert [chat_response.status_code, expiry_response.status_code] == [401, 401]
        assert [
            chat_response.json()["detail"]["code"],
            expiry_response.json()["detail"]["code"],
        ] == ["authentication_required", "authentication_required"]
        assert correlation_calls == []
        assert publication_calls == []

        with open_db(settings.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM pending_ticket_mutations WHERE session_id = ?", (user.session_id,)
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?", (ref,)
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (user.session_id,)
            ).fetchone()[0] == 0
        assert _history_counts(settings, user.conversation_id) == (0, 0, 0)

        after_cleanup = client.get("/api/agent/messages")
        assert after_cleanup.status_code == 401
        assert correlation_calls == [] and publication_calls == []
        assert _history_counts(settings, user.conversation_id) == (0, 0, 0)


@pytest.mark.parametrize("event", ["clear", "logout", "switch"])
def test_lifecycle_event_holds_lock_from_runner_history_cleanup_through_terminal_commit(
    event, settings, monkeypatch
):
    """A stale chat captured before commit cannot regenerate the cleared old scope."""
    fake_itsm = FakeItsm()
    service = AgentService(settings, model=TicketReadModel(reply="old-scope reply"))
    history_written = threading.Event()
    release_runner = threading.Event()
    cleanup_done = threading.Event()
    stale_generation_captured = threading.Event()
    release_terminal_commit = threading.Event()
    barrier_guard = threading.Lock()
    barrier_armed = True

    with TestClient(create_app(settings, agent_service=service, itsm_client=fake_itsm.client())) as client:
        login(client, "admin01")
        old = _current(settings, client)
        ref = _seed_revocable_state(service, old)
        original_session = service._session

        class HistoryWriteBarrierSession:
            def __init__(self, inner):
                self.inner = inner

            async def add_items(self, items):
                nonlocal barrier_armed
                await self.inner.add_items(items)
                should_block = False
                if any(isinstance(item, dict) and item.get("role") == "assistant" for item in items):
                    with barrier_guard:
                        if barrier_armed:
                            barrier_armed = False
                            should_block = True
                if should_block:
                    history_written.set()
                    await asyncio.to_thread(release_runner.wait)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        monkeypatch.setattr(
            service,
            "_session",
            lambda conversation_id: HistoryWriteBarrierSession(original_session(conversation_id)),
        )
        original_clear_locked = service._clear_history_locked

        async def cleanup_commit_barrier(conversation_id):
            await original_clear_locked(conversation_id)
            cleanup_done.set()
            await asyncio.to_thread(release_terminal_commit.wait)

        monkeypatch.setattr(service, "_clear_history_locked", cleanup_commit_barrier)
        original_generation = service.conversation_locks.generation

        def observed_generation(conversation_id):
            value = original_generation(conversation_id)
            if cleanup_done.is_set() and not release_terminal_commit.is_set():
                stale_generation_captured.set()
            return value

        monkeypatch.setattr(service.conversation_locks, "generation", observed_generation)

        def lifecycle_request():
            if event == "clear":
                return client.delete("/api/agent/messages")
            if event == "logout":
                return client.delete("/api/session")
            return client.post("/api/session", json={"user_id": 2})

        with ThreadPoolExecutor(max_workers=3) as pool:
            runner_future = pool.submit(
                client.post,
                "/api/agent/chat",
                json={"message": "terminal event보다 먼저 시작한 Runner"},
            )
            assert history_written.wait(timeout=3)
            lifecycle_future = pool.submit(lifecycle_request)
            release_runner.set()
            assert cleanup_done.wait(timeout=3)
            assert service._conversation_locks[old.conversation_id].locked()
            assert _history_counts(settings, old.conversation_id) == (0, 0, 0)

            stale_future = pool.submit(
                client.post,
                "/api/agent/chat",
                json={"message": "cleanup 뒤 terminal commit 전에 캡처한 old-scope chat"},
            )
            assert stale_generation_captured.wait(timeout=3)
            assert not stale_future.done()
            assert _history_counts(settings, old.conversation_id) == (0, 0, 0)

            release_terminal_commit.set()
            runner_response = runner_future.result(timeout=5)
            lifecycle_response = lifecycle_future.result(timeout=5)
            stale_response = stale_future.result(timeout=5)

        assert runner_response.status_code == 200
        assert lifecycle_response.status_code == (200 if event == "switch" else 204)
        assert stale_response.status_code == 401
        assert stale_response.json()["detail"]["code"] == "authentication_required"
        assert _history_counts(settings, old.conversation_id) == (0, 0, 0)
        with open_db(settings.db_path) as conn:
            assert conn.execute(
                """SELECT COUNT(*) FROM pending_ticket_mutations
                   WHERE session_id = ? AND status IN ('pending', 'confirmed')""",
                (old.session_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM ticket_cursor_bindings WHERE continuation_ref = ?",
                (ref,),
            ).fetchone()[0] == 0
            old_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (old.session_id,)
            ).fetchone()[0]
        assert old_sessions == (1 if event == "clear" else 0)
