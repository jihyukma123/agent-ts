"""연동 시스템에 전달할 ITSM 연동 문서를 만듭니다.

사용법: .venv/bin/python scripts/export_spec.py [--client itsm-agent] [--out itsm_agent/docs/itsm]

- openapi.json         : 현재 ITSM API 코드에서 생성한 OpenAPI 스키마
- integration-guide.md : docs/itsm-api-guide.md + 해당 클라이언트에 발급된 자격증명(개발 환경)
ITSM 소스 코드와 다른 클라이언트의 자격증명은 내보내지 않습니다. API 를 바꾸면 다시 실행하세요.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from itsm_api import seed_data  # noqa: E402
from itsm_api.config import ItsmApiSettings  # noqa: E402
from itsm_api.main import create_app  # noqa: E402

DEV_BASE_URL = "http://127.0.0.1:8001"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", default="itsm-agent")
    parser.add_argument("--out", type=Path, default=ROOT / "itsm_agent" / "docs" / "itsm")
    args = parser.parse_args()

    client = next((c for c in seed_data.API_CLIENTS if c[0] == args.client), None)
    if client is None:
        sys.exit(f"등록되지 않은 클라이언트: {args.client}")
    client_id, name, secret, can_delegate = client
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        schema = create_app(ItsmApiSettings(db_path=Path(tmp) / "itsm.db")).openapi()
    schema["servers"] = [{"url": DEV_BASE_URL, "description": "개발 환경"}]
    (args.out / "openapi.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")

    guide = (ROOT / "docs" / "itsm-api-guide.md").read_text()
    guide += f"""
---

## 부록. 발급된 연동 자격증명 (개발 환경)

| 항목 | 값 |
|---|---|
| 연동 시스템 | {name} |
| `client_id` | `{client_id}` |
| `client_secret` | `{secret}` |
| 사용자 위임 | {"허용" if can_delegate else "불가"} |
| ITSM API 주소 | `{DEV_BASE_URL}` |

운영 환경 자격증명은 별도로 발급합니다.
"""
    (args.out / "integration-guide.md").write_text(guide)
    print(f"내보내기 완료: {args.out} (경로 {len(schema['paths'])}개, client={client_id})")


if __name__ == "__main__":
    main()
