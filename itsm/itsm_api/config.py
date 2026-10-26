"""ITSM API 서비스 설정.

ITSM API 는 ITSM 웹(itsm_web/)과 별도 프로세스로 실행되는 시스템 오브 레코드입니다.
실행: uvicorn itsm_api.main:create_app --factory --port 8001
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ItsmApiSettings:
    db_path: Path
    # 위임 토큰·서비스 토큰 유효 시간(초)
    token_ttl_seconds: int = 900
    # 토큰(클라이언트+사용자)별 분당 요청 한도
    rate_limit_per_minute: int = 300
    # export 작업의 단계별 처리 지연(초). 대량 데이터 처리를 흉내 냅니다.
    export_step_seconds: float = 1.0
    # 운영 환경처럼 API 문서(/docs, /openapi.json)는 기본으로 공개하지 않습니다.
    enable_docs: bool = False


def load_settings() -> ItsmApiSettings:
    data_dir = Path(os.environ.get("ITSM_API_DATA_DIR", PROJECT_ROOT / "data"))
    return ItsmApiSettings(
        db_path=data_dir / "itsm.db",
        token_ttl_seconds=int(os.environ.get("ITSM_API_TOKEN_TTL_SECONDS", "900")),
        rate_limit_per_minute=int(os.environ.get("ITSM_API_RATE_LIMIT_PER_MINUTE", "300")),
        export_step_seconds=float(os.environ.get("ITSM_API_EXPORT_STEP_SECONDS", "1.0")),
        enable_docs=os.environ.get("ITSM_API_ENABLE_DOCS", "").lower() in ("1", "true", "yes"),
    )
