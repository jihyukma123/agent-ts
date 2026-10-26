"""Agent 실행 서비스.

- Agent 실행은 SDK 의 Runner.run 을 그대로 사용합니다(자체 Agent loop 없음).
- 대화 이력은 SDK 의 SQLiteSession 에 저장하며, 세션 키는 서버 측 세션의 conversation_id 입니다.
  conversation_id 는 사용자 전환 시 새로 만들어지므로 전환하면 대화가 초기화됩니다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import openai
from agents import AgentsException, MaxTurnsExceeded, Model, OpenAIResponsesModel, RunConfig, Runner, SQLiteSession
from fastapi import status

from app.agent.assistant import build_agent
from app.agent.context import AgentUserContext
from app.config import Settings
from app.itsm_tickets import TicketGateway
from app.schemas import AgentMessageOut
from app.session import CurrentUser, api_error

logger = logging.getLogger("itsm.agent")

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-*]{4,}")


def redact(text: str) -> str:
    """오류 메시지에 API 키 일부가 섞여 있어도 화면·로그에 노출되지 않게 가립니다."""
    return _SECRET_PATTERN.sub("sk-***", text)


def _api_message(exc: openai.APIStatusError) -> str:
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("message"):
            return redact(str(error["message"]))
    return redact(exc.message)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        return "".join(p for p in parts if p)
    return ""


class AgentService:
    def __init__(self, settings: Settings, model: Model | None = None):
        self.history_db_path: Path = settings.agent_history_db_path
        self.model_name = settings.agent_model
        if model is None and settings.openai_api_key:
            client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            model = OpenAIResponsesModel(model=settings.agent_model, openai_client=client)
        self.agent = build_agent(model) if model is not None else None

    @property
    def configured(self) -> bool:
        return self.agent is not None

    def _session(self, conversation_id: str) -> SQLiteSession:
        self.history_db_path.parent.mkdir(parents=True, exist_ok=True)
        return SQLiteSession(session_id=conversation_id, db_path=self.history_db_path)

    async def chat(self, user: CurrentUser, message: str, *, ticket_gateway: TicketGateway | None = None) -> str:
        if self.agent is None:
            raise api_error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "agent_not_configured",
                "Agent 설정이 누락되었습니다: 프로젝트 루트의 .env.local 에 OPENAI_API_KEY 를 설정한 뒤 서버를 다시 시작하세요.",
            )

        context = AgentUserContext.from_current_user(user, ticket_gateway)
        session = self._session(user.conversation_id)
        try:
            result = await Runner.run(
                self.agent,
                message,
                context=context,
                session=session,
                max_turns=8,
                # 추적 데이터는 외부로 보내지 않습니다. 필요하면 실험 조건에서 켜세요.
                run_config=RunConfig(tracing_disabled=True),
            )
        except openai.AuthenticationError as exc:
            raise self._failure(status.HTTP_502_BAD_GATEWAY, "agent_auth_failed", "OpenAI API 키 인증에 실패했습니다", exc)
        except openai.PermissionDeniedError as exc:
            raise self._failure(
                status.HTTP_502_BAD_GATEWAY, "model_access_denied", f"모델 '{self.model_name}' 접근 권한이 없습니다", exc
            )
        except openai.NotFoundError as exc:
            raise self._failure(
                status.HTTP_502_BAD_GATEWAY, "model_unavailable", f"요청한 모델 '{self.model_name}'을(를) 사용할 수 없습니다", exc
            )
        except openai.BadRequestError as exc:
            raise self._failure(
                status.HTTP_502_BAD_GATEWAY,
                "model_request_rejected",
                f"OpenAI API가 요청을 거부했습니다(모델 '{self.model_name}' 또는 reasoning 설정 확인 필요)",
                exc,
            )
        except openai.RateLimitError as exc:
            raise self._failure(status.HTTP_503_SERVICE_UNAVAILABLE, "model_rate_limited", "OpenAI API 사용 한도에 걸렸습니다", exc)
        except openai.APIStatusError as exc:
            raise self._failure(status.HTTP_502_BAD_GATEWAY, "model_api_error", "OpenAI API 오류가 발생했습니다", exc)
        except openai.APIConnectionError as exc:
            logger.warning("OpenAI API 연결 실패: %s", type(exc).__name__)
            raise api_error(
                status.HTTP_504_GATEWAY_TIMEOUT, "model_connection_failed", "OpenAI API에 연결하지 못했습니다(네트워크 또는 시간 초과)."
            )
        except MaxTurnsExceeded:
            logger.warning("Agent 실행이 8 turn 상한에 도달해 중지됨")
            return "요청 처리 단계가 안전 상한(8회)에 도달해 중지했습니다. 동일한 작업을 자동으로 더 시도하지 않았습니다."
        except AgentsException as exc:
            logger.warning("Agent 실행 실패: %s: %s", type(exc).__name__, redact(str(exc)))
            raise api_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "agent_run_failed", f"Agent 실행에 실패했습니다: {type(exc).__name__}")
        finally:
            session.close()
        return str(result.final_output)

    def _failure(self, status_code: int, code: str, summary: str, exc: openai.APIStatusError):
        detail = _api_message(exc)
        logger.warning("%s (HTTP %s): %s", summary, exc.status_code, detail)
        return api_error(
            status_code,
            code,
            f"{summary}. 다른 모델로 대체하지 않았습니다. (OpenAI 응답 HTTP {exc.status_code}: {detail})",
        )

    async def history(self, conversation_id: str) -> list[AgentMessageOut]:
        session = self._session(conversation_id)
        try:
            items = await session.get_items()
        finally:
            session.close()
        messages: list[AgentMessageOut] = []
        for item in items:
            role = item.get("role") if isinstance(item, dict) else None
            if role in ("user", "assistant") and item.get("type", "message") == "message":
                text = _content_text(item.get("content"))
                if text:
                    messages.append(AgentMessageOut(role=role, text=text))
        return messages

    async def clear_history(self, conversation_id: str) -> None:
        session = self._session(conversation_id)
        try:
            await session.clear_session()
        finally:
            session.close()
