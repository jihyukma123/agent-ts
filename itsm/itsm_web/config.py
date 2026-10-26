"""ITSM 웹(화면 + BFF) 설정.

ITSM API 연결 정보는 환경 변수가 있으면 환경 변수를, 없으면 프로젝트 루트의 .env.local 을,
둘 다 없으면 개발용 기본값을 씁니다. 비밀 값은 Settings 객체 안에만 보관하며 로그·응답에 넣지 않습니다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ITSM_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_ITSM_CLIENT_ID = "portal"
DEFAULT_ITSM_CLIENT_SECRET = "portal-dev-secret"  # ITSM API 시드의 개발용 값
DEFAULT_AGENT_BASE_URL = "http://127.0.0.1:8100"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    static_dir: Path
    itsm_base_url: str = DEFAULT_ITSM_BASE_URL
    itsm_client_id: str = DEFAULT_ITSM_CLIENT_ID
    # repr=False: 디버그 출력이나 예외 메시지에 비밀 값이 섞이지 않도록 합니다.
    itsm_client_secret: str = field(default=DEFAULT_ITSM_CLIENT_SECRET, repr=False)
    itsm_timeout_seconds: float = 10.0
    agent_base_url: str = DEFAULT_AGENT_BASE_URL
    agent_timeout_seconds: float = 120.0
    harness_agent_base_url: str = "http://127.0.0.1:8101"


def _env_values(env_file: Path) -> dict[str, str]:
    if not env_file.is_file():
        return {}
    return {k: (v or "").strip() for k, v in dotenv_values(env_file).items()}


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("ITSM_WEB_DATA_DIR", PROJECT_ROOT / "data"))
    env = _env_values(Path(os.environ.get("ITSM_ENV_FILE", PROJECT_ROOT / ".env.local")))

    def pick(name: str, default: str) -> str:
        return os.environ.get(name) or env.get(name) or default

    return Settings(
        db_path=data_dir / "web.db",
        static_dir=PROJECT_ROOT / "static",
        itsm_base_url=pick("ITSM_BASE_URL", DEFAULT_ITSM_BASE_URL).rstrip("/"),
        itsm_client_id=pick("ITSM_CLIENT_ID", DEFAULT_ITSM_CLIENT_ID),
        itsm_client_secret=pick("ITSM_CLIENT_SECRET", DEFAULT_ITSM_CLIENT_SECRET),
        itsm_timeout_seconds=float(pick("ITSM_TIMEOUT_SECONDS", "10")),
        agent_base_url=pick("ITSM_AGENT_BASE_URL", DEFAULT_AGENT_BASE_URL).rstrip("/"),
        agent_timeout_seconds=float(pick("ITSM_AGENT_TIMEOUT_SECONDS", "120")),
        harness_agent_base_url=pick("ITSM_HARNESS_AGENT_BASE_URL", "http://127.0.0.1:8101").rstrip("/"),
    )
