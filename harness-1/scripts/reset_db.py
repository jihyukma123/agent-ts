"""에이전트 서비스의 세션과 대화 이력을 모두 지웁니다 (ITSM 데이터는 건드리지 않습니다).

사용법: .venv/bin/python scripts/reset_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.db import reset_db  # noqa: E402


def main() -> None:
    settings = load_settings()
    reset_db(settings.db_path, settings.agent_history_db_path)
    print(f"초기화 완료: {settings.db_path}, {settings.agent_history_db_path}")


if __name__ == "__main__":
    main()
