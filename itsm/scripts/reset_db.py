"""데이터를 고정 초기 상태로 되돌립니다.

사용법: .venv/bin/python scripts/reset_db.py

- ITSM API DB(itsm.db): 티켓·사용자·역할·카탈로그 등을 다시 만들고, 발급된 ITSM 토큰을 모두 무효화합니다.
- ITSM 웹 DB(web.db): 서버 측 세션을 지웁니다.
서버가 실행 중이어도 동작하지만, 기존 브라우저 세션은 무효화되므로 사용자를 다시 선택해야 합니다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itsm_api.config import load_settings as load_itsm_settings  # noqa: E402
from itsm_api.db import reset_db as reset_itsm_db  # noqa: E402
from itsm_web.config import load_settings  # noqa: E402
from itsm_web.db import reset_db  # noqa: E402


def main() -> None:
    itsm_settings = load_itsm_settings()
    reset_itsm_db(itsm_settings.db_path)
    print(f"ITSM API 초기화 완료: {itsm_settings.db_path}")
    settings = load_settings()
    reset_db(settings.db_path)
    print(f"ITSM 웹 초기화 완료: {settings.db_path}")


if __name__ == "__main__":
    main()
