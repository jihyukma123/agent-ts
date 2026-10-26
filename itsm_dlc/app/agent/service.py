"""Agent 실행 서비스.

- Agent 실행은 SDK 의 Runner.run 을 그대로 사용합니다(자체 Agent loop 없음).
- 대화 이력은 SDK 의 SQLiteSession 에 저장하며, 세션 키는 서버 측 세션의 conversation_id 입니다.
  conversation_id 는 사용자 전환 시 새로 만들어지므로 전환하면 대화가 초기화됩니다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import openai
from agents import AgentsException, Model, OpenAIResponsesModel, RunConfig, Runner, SQLiteSession
from fastapi import HTTPException, Request, status

from app.agent.assistant import REGISTRY, build_agent
from app.agent.context import AgentUserContext
from app.agent.trace import AgentTraceHooks, resolve_trace_writer
from app.config import Settings
from app.db import open_db
from app.itsm_auth import ItsmAuthClient
from app.schemas import AgentMessageOut
from app.session import CurrentUser, api_error, refresh_itsm_credential_for, revalidate_current_user
from app.ticketing.capability import (
    ActiveCredentialRefreshReservations,
    CredentialRefreshPortV1,
    CredentialRefreshReservationSigner,
    CredentialRefreshReservationVerifier,
    DelegatedCredential,
    TicketRequestCapability,
    UpdateCredentialRefreshPortV1,
)
from app.ticketing.create_operations import TicketCreateOperations
from app.ticketing.create_presentation import ticket_create_presentation
from app.ticketing.create_repository import TicketCreateAttemptRepository
from app.ticketing.gateway import ItsmGateway
from app.ticketing.lifecycle import CurrentTurnEvidenceV1
from app.ticketing.models import TicketPresentationV1
from app.ticketing.operations import TicketOperations
from app.ticketing.presentation import ticket_read_presentation
from app.ticketing.repositories import CursorRepository, PendingMutationRepository, PresentationRepository
from app.ticketing.tools import TicketRuntime
from app.ticketing.update_operations import TicketUpdateOperations
from app.ticketing.update_presentation import ticket_update_presentation
from app.ticketing.update_repository import PendingUpdateExecutionRepository

logger = logging.getLogger("itsm.agent")

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-*]{4,}")
_SAFE_REQUEST_ID = re.compile(r"^req_[A-Za-z0-9]{8,96}$")


def redact(text: str) -> str:
    """오류 메시지에 API 키 일부가 섞여 있어도 화면·로그에 노출되지 않게 가립니다."""
    return _SECRET_PATTERN.sub("sk-***", text)


def _safe_request_id(exc: openai.APIStatusError) -> str | None:
    value = exc.response.headers.get("x-request-id")
    return value if value and _SAFE_REQUEST_ID.fullmatch(value) else None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        return "".join(p for p in parts if p)
    return ""


@dataclass(frozen=True)
class ChatResult:
    reply: str
    presentation: TicketPresentationV1 | None = None


class ConversationLockProvider:
    """Application-lifetime lock registry shared by Runner, history and expiry cleanup."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._generations: dict[str, int] = {}

    def for_conversation(self, conversation_id: str) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    def generation(self, conversation_id: str) -> int:
        return self._generations.get(conversation_id, 0)

    def advance_generation(self, conversation_id: str) -> int:
        generation = self.generation(conversation_id) + 1
        self._generations[conversation_id] = generation
        return generation


class AgentService:
    def __init__(self, settings: Settings, model: Model | None = None):
        self.settings = settings
        self.history_db_path: Path = settings.agent_history_db_path
        self.model_name = settings.agent_model
        self.pending_repository = PendingMutationRepository(settings.db_path)
        self.create_repository = TicketCreateAttemptRepository(settings.db_path)
        # U3 schema is installed by create_app/init_db before configure_ticketing.
        # Keep construction side-effect free so established test/service assembly
        # that creates AgentService first remains valid.
        self.update_execution_repository: PendingUpdateExecutionRepository | None = None
        self.cursor_repository = CursorRepository(settings.db_path)
        self.presentation_repository = PresentationRepository(settings.agent_history_db_path)
        self.ticket_operations: TicketOperations | None = None
        self.ticket_create_operations: TicketCreateOperations | None = None
        self.ticket_update_operations: TicketUpdateOperations | None = None
        self._refresh_signer: CredentialRefreshReservationSigner | None = None
        self._refresh_verifier: CredentialRefreshReservationVerifier | None = None
        self._refresh_port: CredentialRefreshPortV1 | None = None
        self._active_refresh_reservations = ActiveCredentialRefreshReservations()
        self._update_refresh_port: UpdateCredentialRefreshPortV1 | None = None
        key_ring = settings.refresh_signing_key_ring
        if key_ring is not None:
            boot_id = secrets.token_urlsafe(24)
            self._refresh_signer = CredentialRefreshReservationSigner(
                kid=key_ring.current_kid,
                secret=key_ring.keys[key_ring.current_kid],
                creator_boot_id=boot_id,
            )
            verifier = CredentialRefreshReservationVerifier(
                key_ring.keys,
                current_kid=key_ring.current_kid,
                current_boot_id=boot_id,
                max_age=timedelta(seconds=key_ring.verification_seconds),
            )
            self._refresh_verifier = verifier
            self._refresh_port = CredentialRefreshPortV1(verifier)
        self.conversation_locks = ConversationLockProvider()
        # 기존 lifecycle 관찰 지점과의 호환을 유지한다.
        self._conversation_locks = self.conversation_locks._locks
        self.trace_writer = resolve_trace_writer(settings.db_path.parent)
        if model is None and settings.openai_api_key:
            client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            model = OpenAIResponsesModel(model=settings.agent_model, openai_client=client)
        self.agent = build_agent(model) if model is not None else None

    def configure_ticketing(self, itsm: ItsmAuthClient) -> None:
        self.update_execution_repository = PendingUpdateExecutionRepository(self.settings.db_path)
        self._update_refresh_port = UpdateCredentialRefreshPortV1(
            self.pending_repository
        )
        gateway = ItsmGateway(itsm)
        self.ticket_operations = TicketOperations(
            gateway,
            self.cursor_repository,
            timeout_seconds=self.settings.itsm_timeout_seconds,
        )
        self.ticket_create_operations = TicketCreateOperations(
            gateway,
            self.create_repository,
            self.pending_repository,
            timeout_seconds=self.settings.itsm_timeout_seconds,
            refresh_reservation_signer=self._refresh_signer,
            refresh_reservation_verifier=self._refresh_verifier,
            credential_refresh_port=self._refresh_port,
            active_refresh_reservations=self._active_refresh_reservations,
        )
        self.ticket_update_operations = TicketUpdateOperations(
            self.ticket_operations,
            gateway,
            self.pending_repository,
            self.update_execution_repository,
            credential_refresh_port=self._update_refresh_port,
            timeout_seconds=self.settings.itsm_timeout_seconds,
        )

    def startup_cleanup(self) -> None:
        self.trace_writer.prepare_file()
        self.cursor_repository.cleanup_expired()
        self.create_repository.cleanup_expired()
        self.presentation_repository.cleanup_orphans()
        now = datetime.now(timezone.utc)
        if self.update_execution_repository is None:
            raise RuntimeError("ticket update repository is unavailable")
        with open_db(self.settings.db_path) as conn:
            credential_generations = {
                row["id"]: int(row["itsm_credential_generation"])
                for row in conn.execute(
                    "SELECT id, itsm_credential_generation FROM sessions"
                ).fetchall()
            }
        for execution in self.update_execution_repository.non_retired():
            generation = credential_generations.get(execution.originating_session_id)
            if execution.refresh_state == "reserved" and generation is not None:
                self.update_execution_repository.reconcile_refresh_after_restart(
                    execution.mutation_id,
                    current_session_credential_generation=generation,
                    now=now,
                )
        self.update_execution_repository.recover_or_retire(
            self.pending_repository, now=now
        )
        self.update_execution_repository.cleanup(
            now=now, retention_cutoff=now - timedelta(hours=8)
        )

    @property
    def configured(self) -> bool:
        return self.agent is not None

    def _session(self, conversation_id: str) -> SQLiteSession:
        self.history_db_path.parent.mkdir(parents=True, exist_ok=True)
        return SQLiteSession(session_id=conversation_id, db_path=self.history_db_path)

    async def chat(self, request: Request, user: CurrentUser, message: str) -> ChatResult:
        if self.agent is None:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "agent_not_configured",
                "Agent 설정이 누락되었습니다: 프로젝트 루트의 .env.local 에 OPENAI_API_KEY 를 설정한 뒤 서버를 다시 시작하세요.",
            )

        if self.ticket_operations is None:
            raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "ticket_runtime_unavailable", "티켓 조회 기능을 준비하지 못했습니다.")

        lock = self.conversation_locks.for_conversation(user.conversation_id)
        async with lock:
            user = revalidate_current_user(request, user)

            async def current_credential() -> DelegatedCredential:
                return DelegatedCredential(
                    user.itsm_token,
                    user.itsm_token_expires_at,
                    user.itsm_credential_generation,
                )

            async def refresh_credential() -> DelegatedCredential:
                token, expires_at, generation = await refresh_itsm_credential_for(request, user)
                return DelegatedCredential(token, expires_at, generation)

            async def expire_authority() -> None:
                try:
                    revalidate_current_user(request, user)
                except HTTPException as exc:
                    detail = exc.detail
                    code = detail.get("code") if isinstance(detail, dict) else None
                    if code in ("session_expired", "no_session"):
                        return
                    raise

            capability = TicketRequestCapability(
                user.id,
                user.session_id,
                user.conversation_id,
                current_credential,
                refresh_credential,
                session_expires_at=user.session_expires_at,
                _expiry_handler=expire_authority,
                _utc_now=request.app.state.utc_now,
                _credential_generation=user.itsm_credential_generation,
            )
            turn_evidence = CurrentTurnEvidenceV1.capture(
                user_id=user.id,
                session_id=user.session_id,
                conversation_id=user.conversation_id,
                raw_message=message,
            )
            runtime = TicketRuntime(
                capability=capability,
                operations=self.ticket_operations,
                create_operations=self.ticket_create_operations,
                update_operations=self.ticket_update_operations,
                current_turn_evidence=turn_evidence,
            )
            context = AgentUserContext.from_current_user(
                user, ticket_runtime=runtime, current_turn_evidence=turn_evidence
            )
            session = self._session(user.conversation_id)
            trace_hooks = AgentTraceHooks(
                self.trace_writer,
                user_info=f"{user.username} ({user.role}, id={user.id})",
                conversation_id=user.conversation_id,
                user_message=message,
                model_name=self.model_name,
            )
            trace_hooks.emit_turn_start()
            try:
                result = await Runner.run(
                    self.agent,
                    message,
                    context=context,
                    session=session,
                    hooks=trace_hooks,
                    # 추적 데이터는 외부로 보내지 않습니다. 필요하면 실험 조건에서 켜세요.
                    run_config=RunConfig(tracing_disabled=True),
                )
            except openai.AuthenticationError as exc:
                trace_hooks.emit_turn_error(exc)
                raise self._failure(status.HTTP_502_BAD_GATEWAY, "agent_auth_failed", "OpenAI API 키 인증에 실패했습니다", exc)
            except openai.PermissionDeniedError as exc:
                trace_hooks.emit_turn_error(exc)
                raise self._failure(
                    status.HTTP_502_BAD_GATEWAY, "model_access_denied", f"모델 '{self.model_name}' 접근 권한이 없습니다", exc
                )
            except openai.NotFoundError as exc:
                trace_hooks.emit_turn_error(exc)
                raise self._failure(
                    status.HTTP_502_BAD_GATEWAY, "model_unavailable", f"요청한 모델 '{self.model_name}'을(를) 사용할 수 없습니다", exc
                )
            except openai.BadRequestError as exc:
                trace_hooks.emit_turn_error(exc)
                raise self._failure(
                    status.HTTP_502_BAD_GATEWAY,
                    "model_request_rejected",
                    f"OpenAI API가 요청을 거부했습니다(모델 '{self.model_name}' 또는 reasoning 설정 확인 필요)",
                    exc,
                )
            except openai.RateLimitError as exc:
                trace_hooks.emit_turn_error(exc)
                raise self._failure(status.HTTP_503_SERVICE_UNAVAILABLE, "model_rate_limited", "OpenAI API 사용 한도에 걸렸습니다", exc)
            except openai.APIStatusError as exc:
                trace_hooks.emit_turn_error(exc)
                raise self._failure(status.HTTP_502_BAD_GATEWAY, "model_api_error", "OpenAI API 오류가 발생했습니다", exc)
            except openai.APIConnectionError as exc:
                trace_hooks.emit_turn_error(exc)
                logger.warning("OpenAI API 연결 실패: %s", type(exc).__name__)
                raise api_error(
                    status.HTTP_504_GATEWAY_TIMEOUT, "model_connection_failed", "OpenAI API에 연결하지 못했습니다(네트워크 또는 시간 초과)."
                )
            except AgentsException as exc:
                trace_hooks.emit_turn_error(exc)
                logger.warning("Agent 실행 실패: %s", type(exc).__name__)
                raise api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "agent_run_failed", f"Agent 실행에 실패했습니다: {type(exc).__name__}")
            finally:
                session.close()

            # Runner가 SDK history를 쓴 직후, 어떤 reply/correlation/presentation 결정도
            # 하기 전에 persisted authority를 같은 lock 아래 다시 확인한다.
            user = revalidate_current_user(request, user)
            reply = str(result.final_output)
            presentation = None
            if runtime.last_update_result is not None:
                try:
                    candidate = ticket_update_presentation(runtime.last_update_result)
                    REGISTRY.mapper_for(candidate.kind)
                    persisted = self.presentation_repository.latest_assistant(user.conversation_id)
                    if persisted is not None and persisted.text == reply:
                        presentation = self.presentation_repository.put_and_readback(
                            user.conversation_id, persisted, candidate
                        )
                except Exception as exc:
                    logger.warning("presentation 저장 생략: %s", type(exc).__name__)
            elif runtime.last_write_result is not None:
                try:
                    candidate = ticket_create_presentation(runtime.last_write_result)
                    REGISTRY.mapper_for(candidate.kind)
                    persisted = self.presentation_repository.latest_assistant(user.conversation_id)
                    if persisted is not None and persisted.text == reply:
                        presentation = self.presentation_repository.put_and_readback(
                            user.conversation_id, persisted, candidate
                        )
                except Exception as exc:
                    logger.warning("presentation 저장 생략: %s", type(exc).__name__)
            elif runtime.last_read_result is not None:
                try:
                    candidate = ticket_read_presentation(runtime.last_read_result)
                    # 등록된 allowlist kind만 현재 응답과 이력에 붙일 수 있다.
                    REGISTRY.mapper_for(candidate.kind)
                    persisted = self.presentation_repository.latest_assistant(user.conversation_id)
                    if persisted is not None and persisted.text == reply:
                        presentation = self.presentation_repository.put_and_readback(
                            user.conversation_id, persisted, candidate
                        )
                except Exception as exc:
                    logger.warning("presentation 저장 생략: %s", type(exc).__name__)
            trace_hooks.emit_turn_end(
                presentation_kind=presentation.kind if presentation is not None else None
            )
            return ChatResult(reply=reply, presentation=presentation)

    def _failure(self, status_code: int, code: str, summary: str, exc: openai.APIStatusError):
        request_id = _safe_request_id(exc)
        logger.warning(
            "%s (category=%s, HTTP %s, request_id=%s)",
            summary,
            code,
            exc.status_code,
            request_id or "none",
        )
        safe_detail = f"{summary}. 다른 모델로 대체하지 않았습니다. (OpenAI 응답 HTTP {exc.status_code})"
        if request_id is not None:
            safe_detail += f" 요청 ID: {request_id}"
        return api_error(
            status_code,
            code,
            safe_detail,
        )

    async def history(self, request: Request, user: CurrentUser) -> list[AgentMessageOut]:
        lock = self.conversation_locks.for_conversation(user.conversation_id)
        async with lock:
            user = revalidate_current_user(request, user)
            try:
                self.presentation_repository.cleanup_orphans()
            except Exception as exc:
                logger.warning("presentation orphan 정리 생략: %s", type(exc).__name__)
            items = self.presentation_repository.message_rows(user.conversation_id)
            messages: list[AgentMessageOut] = []
            for sequence, item in items:
                role = item.get("role") if isinstance(item, dict) else None
                if role in ("user", "assistant") and item.get("type", "message") == "message":
                    text = _content_text(item.get("content"))
                    if text:
                        presentation = (
                            self.presentation_repository.load(user.conversation_id, sequence, text)
                            if role == "assistant"
                            else None
                        )
                        messages.append(AgentMessageOut(role=role, text=text, presentation=presentation))
            return messages

    async def _clear_history_locked(self, conversation_id: str) -> None:
        """Clear revocable scope while the caller holds the conversation lock."""
        if self.update_execution_repository is not None:
            self.update_execution_repository.retire_scope(conversation_id=conversation_id)
        self.pending_repository.invalidate(conversation_id=conversation_id)
        self.cursor_repository.invalidate(conversation_id=conversation_id)
        self.presentation_repository.clear(conversation_id)
        session = self._session(conversation_id)
        try:
            await session.clear_session()
        finally:
            session.close()

    async def lifecycle_transition(
        self,
        request: Request,
        expected: CurrentUser,
        terminal_commit: Callable[[CurrentUser], None],
    ) -> None:
        """Serialize clear/switch/logout cleanup through terminal authority commit."""
        lock = self.conversation_locks.for_conversation(expected.conversation_id)
        async with lock:
            current = revalidate_current_user(request, expected)
            await self._clear_history_locked(current.conversation_id)
            terminal_commit(current)
            self.conversation_locks.advance_generation(current.conversation_id)

    async def clear_history(self, request: Request, expected: CurrentUser) -> None:
        def commit_clear_generation(current: CurrentUser) -> None:
            # The generation advance occurs before lock release in lifecycle_transition.
            # The persisted session remains unchanged for subsequent fresh requests.
            if current.session_id != expected.session_id:
                raise sqlite3.IntegrityError("clear actor changed")

        await self.lifecycle_transition(request, expected, commit_clear_generation)
