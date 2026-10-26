"""ITSM Assistant(에이전트) 설정.

비밀 값(OPENAI_API_KEY, ITSM_CLIENT_SECRET)은 프로젝트 루트의 .env.local 에서 애플리케이션 시작 시 한 번 읽습니다.
값은 Settings 객체 안에만 보관하며 로그·응답·Agent 실행 context 에 넣지 않습니다.
ITSM API 연결 정보는 환경 변수가 있으면 환경 변수를, 없으면 .env.local 을, 둘 다 없으면 개발용 기본값을 씁니다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 실험 조건으로 고정된 값입니다. 사용할 수 없을 때 다른 모델로 대체하지 않습니다.
AGENT_MODEL = "gpt-6-luna"
AGENT_REASONING_EFFORT = "high"

DEFAULT_ITSM_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_ITSM_CLIENT_ID = "itsm-agent"
DEFAULT_ITSM_CLIENT_SECRET = "itsm-agent-dev-secret"  # ITSM 개발 환경에 등록된 값


@dataclass(frozen=True)
class Settings:
    db_path: Path
    agent_history_db_path: Path
    static_dir: Path
    # repr=False: 디버그 출력이나 예외 메시지에 비밀 값이 섞이지 않도록 합니다.
    openai_api_key: str | None = field(default=None, repr=False)
    agent_model: str = AGENT_MODEL
    agent_reasoning_effort: str = AGENT_REASONING_EFFORT
    itsm_base_url: str = DEFAULT_ITSM_BASE_URL
    itsm_client_id: str = DEFAULT_ITSM_CLIENT_ID
    itsm_client_secret: str = field(default=DEFAULT_ITSM_CLIENT_SECRET, repr=False)
    itsm_timeout_seconds: float = 10.0

    @property
    def agent_configured(self) -> bool:
        return bool(self.openai_api_key)


def _env_values(env_file: Path) -> dict[str, str]:
    if not env_file.is_file():
        return {}
    return {k: (v or "").strip() for k, v in dotenv_values(env_file).items()}


def load_api_key(env_file: Path) -> str | None:
    """.env.local 에서 OPENAI_API_KEY 를 읽습니다. 파일을 수정하거나 os.environ 에 쓰지 않습니다."""
    return _env_values(env_file).get("OPENAI_API_KEY") or None


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("AGENT_DATA_DIR", PROJECT_ROOT / "data"))
    env = _env_values(Path(os.environ.get("AGENT_ENV_FILE", PROJECT_ROOT / ".env.local")))

    def pick(name: str, default: str) -> str:
        return os.environ.get(name) or env.get(name) or default

    return Settings(
        db_path=data_dir / "agent.db",
        agent_history_db_path=data_dir / "agent_history.db",
        static_dir=PROJECT_ROOT / "static",
        openai_api_key=env.get("OPENAI_API_KEY") or None,
        itsm_base_url=pick("ITSM_BASE_URL", DEFAULT_ITSM_BASE_URL).rstrip("/"),
        itsm_client_id=pick("ITSM_CLIENT_ID", DEFAULT_ITSM_CLIENT_ID),
        itsm_client_secret=pick("ITSM_CLIENT_SECRET", DEFAULT_ITSM_CLIENT_SECRET),
        itsm_timeout_seconds=float(pick("ITSM_TIMEOUT_SECONDS", "10")),
    )
