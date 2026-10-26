"""ITSM Assistant(에이전트) 설정.

비밀 값(OPENAI_API_KEY, ITSM_CLIENT_SECRET)은 프로젝트 루트의 .env.local 에서 애플리케이션 시작 시 한 번 읽습니다.
값은 Settings 객체 안에만 보관하며 로그·응답·Agent 실행 context 에 넣지 않습니다.
ITSM API 연결 정보는 환경 변수가 있으면 환경 변수를, 없으면 .env.local 을, 둘 다 없으면 개발용 기본값을 씁니다.
"""

from __future__ import annotations

import os
import base64
import json
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from app.ticketing.capability import valid_refresh_key_id

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 실험 조건으로 고정된 값입니다. 사용할 수 없을 때 다른 모델로 대체하지 않습니다.
AGENT_MODEL = "gpt-6-luna"
AGENT_REASONING_EFFORT = "high"

DEFAULT_ITSM_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_ITSM_CLIENT_ID = "itsm-agent"
DEFAULT_ITSM_CLIENT_SECRET = "itsm-agent-dev-secret"  # ITSM 개발 환경에 등록된 값
MIN_REFRESH_KEY_VERIFICATION_SECONDS = 24 * 60 * 60
MAX_REFRESH_KEY_VERIFICATION_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class RefreshSigningKeyRingConfig:
    current_kid: str
    keys: dict[str, bytes] = field(repr=False)
    verification_seconds: int


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
    refresh_signing_key_ring: RefreshSigningKeyRingConfig | None = field(
        default=None, repr=False
    )
    refresh_signing_config_error: str | None = None

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


def _load_refresh_key_ring(values: dict[str, str]) -> tuple[RefreshSigningKeyRingConfig | None, str | None]:
    raw_keys = values.get("TICKET_REFRESH_KEYS_JSON", "")
    current_kid = values.get("TICKET_REFRESH_CURRENT_KID", "")
    raw_lifetime = values.get("TICKET_REFRESH_VERIFICATION_SECONDS", "")
    if not any((raw_keys, current_kid, raw_lifetime)):
        return None, None
    if not raw_keys or not current_kid or not raw_lifetime:
        return None, "refresh signing key configuration is incomplete"
    try:
        encoded_keys = json.loads(raw_keys)
        lifetime = int(raw_lifetime)
        if not isinstance(encoded_keys, dict) or not encoded_keys:
            raise ValueError
        keys = {
            str(kid): base64.b64decode(str(encoded), altchars=b"-_", validate=True)
            for kid, encoded in encoded_keys.items()
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "refresh signing key configuration is malformed"
    if (
        not valid_refresh_key_id(current_kid)
        or current_kid not in keys
        or any(not valid_refresh_key_id(kid) or len(secret) < 32 for kid, secret in keys.items())
    ):
        return None, "refresh signing key configuration contains an unknown or weak key"
    if not MIN_REFRESH_KEY_VERIFICATION_SECONDS <= lifetime <= MAX_REFRESH_KEY_VERIFICATION_SECONDS:
        return None, "refresh signing key verification lifetime is outside the approved bounds"
    return RefreshSigningKeyRingConfig(current_kid, keys, lifetime), None


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("AGENT_DATA_DIR", PROJECT_ROOT / "data"))
    env = _env_values(Path(os.environ.get("AGENT_ENV_FILE", PROJECT_ROOT / ".env.local")))

    def pick(name: str, default: str) -> str:
        return os.environ.get(name) or env.get(name) or default

    refresh_values = {
        name: os.environ.get(name) or env.get(name) or ""
        for name in (
            "TICKET_REFRESH_KEYS_JSON",
            "TICKET_REFRESH_CURRENT_KID",
            "TICKET_REFRESH_VERIFICATION_SECONDS",
        )
    }
    refresh_key_ring, refresh_config_error = _load_refresh_key_ring(refresh_values)

    return Settings(
        db_path=data_dir / "agent.db",
        agent_history_db_path=data_dir / "agent_history.db",
        static_dir=PROJECT_ROOT / "static",
        openai_api_key=env.get("OPENAI_API_KEY") or None,
        itsm_base_url=pick("ITSM_BASE_URL", DEFAULT_ITSM_BASE_URL).rstrip("/"),
        itsm_client_id=pick("ITSM_CLIENT_ID", DEFAULT_ITSM_CLIENT_ID),
        itsm_client_secret=pick("ITSM_CLIENT_SECRET", DEFAULT_ITSM_CLIENT_SECRET),
        itsm_timeout_seconds=float(pick("ITSM_TIMEOUT_SECONDS", "10")),
        refresh_signing_key_ring=refresh_key_ring,
        refresh_signing_config_error=refresh_config_error,
    )
